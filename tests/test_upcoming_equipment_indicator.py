from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base, get_db
from app.models_dismissal_lifecycle import DismissalDetailsSnapshot
from app.routers import dashboard_dismissals as ui
from app.security import CurrentUser
from app.services.dismissal_details import DismissalDetailsService
from app.services.dismissal_details_cache import DismissalDetailsCacheService
from app.services.upcoming_dismissals import UpcomingDismissalService
from test_account_requirement import ASGIClient


SETTINGS = SimpleNamespace(app_timezone="UTC", dry_run=False)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def candidate(key="worker", **kwargs):
    day = date.today() + timedelta(days=2)
    return {
        "worker_key": key, "fio": "Иванов Иван", "login": "ivanov", "email": "ivanov@example.ru",
        "dismissal_date": day, "effective_block_date": day, "preliminary": False,
        "organizations": [], "deferred": False, "blocking_required": True,
        "blocking_completed": False, "deferral_allowed": True, "timing_label": "Через 2 дн.",
        **kwargs,
    }


def save_snapshot(db, person, row=None, **kwargs):
    snapshot = DismissalDetailsSnapshot(
        worker_key=person["worker_key"], dismissal_date=person["dismissal_date"],
        candidate_fingerprint=DismissalDetailsCacheService.candidate_fingerprint(person),
        payload_json=json.dumps({"version": 1, "rows": [row] if row else []}, ensure_ascii=False),
        status="ready", checked_at=datetime.now(timezone.utc),
    )
    for key, value in kwargs.items():
        setattr(snapshot, key, value)
    db.add(snapshot)
    db.commit()
    return snapshot


@pytest.mark.parametrize("value,count,label", [
    ("Есть — 3 шт.", 3, "Оборудование · 3"),
    ("Отсутствует", 0, "Оборудования нет"),
])
def test_legacy_snapshot_is_shown_without_remote_queries_or_writes(db, value, count, label):
    person = candidate()
    save_snapshot(db, person, {"label": "IT Invent", "value": value, "state": "success"})
    with patch.object(DismissalDetailsService, "build", side_effect=AssertionError("No live checks")), \
            patch.object(DismissalDetailsCacheService, "enqueue", side_effect=AssertionError("No writes")):
        DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    summary = person["equipment_summary"]
    assert summary["count"] == count
    assert summary["label"] == label
    assert summary["state"] == ("present" if count else "empty")
    assert not summary["stale"]
    assert not db.new and not db.dirty


@pytest.mark.parametrize("row", [
    {"label": "IT Invent", "value": "Не настроено"},
    {"label": "IT Invent", "value": "Нет логина", "state": "warning"},
    {"label": "IT Invent", "value": "Не проверено", "state": "warning"},
    {"label": "AD", "value": "Есть — 7 шт.", "state": "success"},
    {"label": "IT Invent", "value": "Отсутствует", "state": "error", "equipment_count": 0},
    {"label": "IT Invent", "value": "???", "equipment_count": True},
    {"label": "IT Invent", "value": "???", "equipment_count": -1},
])
def test_unverified_and_invalid_values_never_mean_no_equipment(db, row):
    person = candidate()
    save_snapshot(db, person, row)
    DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    assert person["equipment_summary"]["count"] is None
    assert person["equipment_summary"]["state"] in {"unknown", "error"}
    assert "Оборудования нет" not in person["equipment_summary"]["label"]


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"version":2,"rows":[]}', "{}"])
def test_corrupt_or_missing_cache_never_means_absence(db, raw):
    person = candidate()
    cache = DismissalDetailsCacheService(SETTINGS, db)
    cache.attach_equipment_summaries([person])
    assert person["equipment_summary"]["count"] is None
    assert db.query(DismissalDetailsSnapshot).count() == 0
    save_snapshot(db, person, payload_json=raw)
    cache.attach_equipment_summaries([person])
    assert person["equipment_summary"]["state"] == "unknown"


@pytest.mark.parametrize("condition", ["old", "refreshing", "fingerprint", "inner-stale", "legacy-stale"])
def test_last_successful_result_is_explicitly_marked_stale(db, condition):
    person = candidate()
    row = {"label": "IT Invent", "value": "Есть — 2 шт.", "state": "success", "equipment_count": 2}
    changes = {}
    if condition == "old":
        changes["checked_at"] = datetime.now(timezone.utc) - timedelta(days=2)
    elif condition == "refreshing":
        changes["status"] = "refreshing"
    elif condition == "fingerprint":
        changes["candidate_fingerprint"] = "previous"
    elif condition == "inner-stale":
        row["equipment_stale"] = True
    else:
        row["note"] = "Показан последний успешный результат проверки"
    save_snapshot(db, person, row, **changes)
    DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    summary = person["equipment_summary"]
    assert summary["count"] == 2
    assert summary["stale"]
    assert "устарело" in summary["label"]


def test_batch_is_one_select_and_never_uses_another_dismissal_event(db):
    people = [candidate(str(index)) for index in range(20)]
    save_snapshot(db, people[0], {"label": "IT Invent", "value": "Есть — 9 шт.", "state": "success"},
                  dismissal_date=people[0]["dismissal_date"] - timedelta(days=30))
    queries = []
    def collect(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    event.listen(db.bind, "before_cursor_execute", collect)
    try:
        DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries(people)
    finally:
        event.remove(db.bind, "before_cursor_execute", collect)
    assert len(queries) == 1 and queries[0].lstrip().startswith("SELECT")
    assert all(person["equipment_summary"]["count"] is None for person in people)


def test_empty_batch_makes_no_queries(db):
    with patch.object(db, "scalars", side_effect=AssertionError("Empty batch")):
        DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([])


def test_stale_absence_is_not_presented_as_current_check(db):
    person = candidate()
    save_snapshot(db, person, {"label": "IT Invent", "value": "Отсутствует"}, status="stale")
    DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    assert person["equipment_summary"]["label"] == "Оборудования нет · устарело"


def test_structured_count_does_not_depend_on_presentation_text(db):
    person = candidate()
    save_snapshot(db, person, {"label": "IT Invent", "value": "Equipment found", "state": "success",
                               "equipment_count": 4})
    DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    assert person["equipment_summary"]["count"] == 4


def test_failed_snapshot_with_no_rows_shows_error_instead_of_absence(db):
    person = candidate()
    save_snapshot(db, person, status="error", last_error="IT Invent недоступен")
    DismissalDetailsCacheService(SETTINGS, db).attach_equipment_summaries([person])
    assert person["equipment_summary"]["state"] == "error"
    assert person["equipment_summary"]["count"] is None


@pytest.mark.parametrize("count,state", [(2, "found"), (0, "found"), (3, "stale"), (0, "owner_not_found")])
def test_new_snapshots_preserve_structured_count_and_inner_freshness(db, count, state):
    person = candidate()
    card = SimpleNamespace(itinvent_state=state, itinvent=SimpleNamespace(equipment=[object()] * count),
                           itinvent_checked_at="07.10.2026 10:30", itinvent_error="")
    row = DismissalDetailsService(SETTINGS, db)._itinvent(card, "")
    assert row["equipment_count"] == count
    cache = DismissalDetailsCacheService(SETTINGS, db)
    with patch.object(DismissalDetailsService, "build", return_value={"rows": [row]}):
        snapshot = cache.refresh(person)
    assert json.loads(snapshot.payload_json)["rows"][0]["equipment_count"] == count
    cache.attach_equipment_summaries([person])
    assert person["equipment_summary"]["count"] == count
    assert person["equipment_summary"]["stale"] == (state == "stale")


@pytest.mark.parametrize("url", ["/", "/dismissals/upcoming/fragment"])
@pytest.mark.parametrize("preliminary", [False, True])
def test_dashboard_and_periodic_fragment_include_indicator_and_escaped_tooltip(db, monkeypatch, url, preliminary):
    person = candidate(preliminary=preliminary)
    snapshot = save_snapshot(db, person, {"label": "IT Invent", "value": "Есть — 3 шт.",
                                        "state": "success", "note": '<script>alert("bad")</script>'})
    app = FastAPI()
    app.include_router(ui.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    monkeypatch.setattr(ui, "get_current_user", lambda request: CurrentUser("operator", "operator", "domain"))
    monkeypatch.setattr(UpcomingDismissalService, "list_upcoming", lambda self, **kwargs: [person])
    monkeypatch.setattr(ui.EmployeeArrivalService, "list_pending", lambda self, **kwargs: [])
    monkeypatch.setattr(ui, "_journal_items", lambda *args, **kwargs: [])
    client = ASGIClient(app)
    with patch.object(DismissalDetailsService, "build", side_effect=AssertionError("No remote checks")):
        response = client.get(url)
    assert response.status_code == 200
    assert 'class="upcoming-equipment present"' in response.text
    assert "Оборудование · 3" in response.text
    assert "&lt;script&gt;" in response.text
    assert '<script>alert("bad")</script>' not in response.text
    assert ('class="upcoming-preliminary"' in response.text) == preliminary
    assert "Подробности" in response.text and "Отложить" in response.text
    snapshot.payload_json = json.dumps({"version": 1, "rows": [{"label": "IT Invent", "value": "Отсутствует"}]})
    db.commit()
    refreshed = client.get("/dismissals/upcoming/fragment")
    assert "Оборудования нет" in refreshed.text
    assert "Оборудование · 3" not in refreshed.text
