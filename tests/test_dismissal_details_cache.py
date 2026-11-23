from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models_dismissal_lifecycle import DismissalDetailsSnapshot
from app.models import OneCImportRun
from app.models_preliminary_dismissals import PreliminaryDismissalItem
from app.services.dismissal_details import DismissalDetailsService
from app.services.dismissal_details_cache import (
    DismissalDetailsCacheService, DismissalDetailsSnapshotWorker, PROCESS_STARTED_AT,
)
from app.services.upcoming_dismissals import UpcomingDismissalService


class FakeSettings:
    app_timezone = "Europe/Moscow"


def _database() -> tuple[object, Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, Session(engine)


def _candidate() -> dict:
    dismissal_date = date(2026, 8, 21)
    return {
        "worker_key": "worker",
        "fio": "Иванов Иван",
        "login": "ivanov",
        "email": "ivanov@example.ru",
        "dismissal_date": dismissal_date,
        "effective_block_date": dismissal_date,
        "deferred": False,
        "organizations": [
            {
                "source_id": "example.ru",
                "source_name": "Организация",
                "dismissal_date": dismissal_date,
                "status": "scheduled",
                "is_present": True,
                "placements": ["ИТ – Инженер"],
            }
        ],
    }


def _live_details(candidate: dict) -> dict:
    return {
        "fio": candidate["fio"],
        "dismissal_date": candidate["dismissal_date"],
        "organizations": candidate["organizations"],
        "rows": [
            {
                "label": "AD",
                "value": "ivanov",
                "state": "success",
                "note": "Активен",
            }
        ],
    }


def test_view_without_snapshot_never_runs_live_checks():
    engine, db = _database()
    candidate = _candidate()

    with patch(
        "app.services.dismissal_details_cache.DismissalDetailsService.build",
        side_effect=AssertionError("Открытие окна не должно проверять AD"),
    ):
        details = DismissalDetailsCacheService(FakeSettings(), db).view(candidate)

    assert details["snapshot_state"] == "pending"
    assert details["rows"] == []
    assert details["snapshot_checked_at"] == ""

    db.close()
    engine.dispose()


def test_refresh_saves_snapshot_and_view_reads_it_from_database():
    engine, db = _database()
    candidate = _candidate()
    service = DismissalDetailsCacheService(FakeSettings(), db)

    with patch(
        "app.services.dismissal_details_cache.DismissalDetailsService.build",
        return_value=_live_details(candidate),
    ) as live_build:
        snapshot = service.refresh(candidate)

    assert snapshot.status == "ready"
    assert snapshot.checked_at is not None
    live_build.assert_called_once_with(candidate)

    with patch(
        "app.services.dismissal_details_cache.DismissalDetailsService.build",
        side_effect=AssertionError("Чтение снимка не должно проверять системы"),
    ):
        details = service.view(candidate)

    assert details["snapshot_state"] == "ready"
    assert details["snapshot_checked_at"]
    assert details["rows"][0]["label"] == "AD"
    assert details["rows"][0]["value"] == "ivanov"
    assert not service.needs_refresh(
        candidate,
        now=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert service.needs_refresh(
        candidate,
        now=datetime.now(timezone.utc) + timedelta(minutes=6),
    )

    changed = {**candidate, "effective_block_date": date(2026, 8, 28)}
    assert service.needs_refresh(
        changed,
        now=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    changed_view = service.view(changed)
    assert changed_view["snapshot_state"] == "stale"
    assert "Кадровые данные изменились" in changed_view["snapshot_error"]
    assert changed_view["rows"][0]["value"] == "ivanov"

    db.close()
    engine.dispose()


def test_failed_refresh_keeps_last_successful_snapshot():
    engine, db = _database()
    candidate = _candidate()
    service = DismissalDetailsCacheService(FakeSettings(), db)

    with patch(
        "app.services.dismissal_details_cache.DismissalDetailsService.build",
        return_value=_live_details(candidate),
    ):
        service.refresh(candidate)

    with patch(
        "app.services.dismissal_details_cache.DismissalDetailsService.build",
        side_effect=RuntimeError("AD временно недоступен"),
    ):
        snapshot = service.refresh(candidate)

    assert snapshot.status == "stale"
    details = service.view(candidate)
    assert details["snapshot_state"] == "stale"
    assert details["rows"][0]["value"] == "ivanov"
    assert details["snapshot_error"] == "AD временно недоступен"
    assert db.query(DismissalDetailsSnapshot).count() == 1

    db.close()
    engine.dispose()


@pytest.mark.parametrize("previous_process", [True, False])
def test_old_import_does_not_block_but_current_import_still_pauses(previous_process):
    engine, db = _database()
    run = OneCImportRun(status="running", started_at=PROCESS_STARTED_AT + (
        -timedelta(days=3) if previous_process else timedelta(seconds=1)
    ))
    db.add(run)
    db.commit()
    worker = DismissalDetailsSnapshotWorker(FakeSettings(), sessionmaker(engine))
    with patch.object(UpcomingDismissalService, "list_upcoming", return_value=[_candidate()]), patch.object(
        DismissalDetailsService, "build", side_effect=_live_details,
    ) as build:
        worker._run_once()
    assert build.call_count == int(previous_process)
    db.refresh(run)
    assert run.status == "running"  # Snapshot worker must not rewrite HR import history.
    state = DismissalDetailsCacheService(FakeSettings(), db).view(_candidate())
    assert state["snapshot_state"] == ("ready" if previous_process else "waiting_import")
    if not previous_process:
        assert f"#{run.id}" in state["snapshot_error"]
    db.close()
    engine.dispose()


def test_exception_in_one_candidate_does_not_block_the_next_and_is_retried():
    engine, db = _database()
    bad = {**_candidate(), "effective_block_date": None}
    good = {**_candidate(), "worker_key": "second"}
    worker = DismissalDetailsSnapshotWorker(FakeSettings(), sessionmaker(engine))
    with patch.object(UpcomingDismissalService, "list_upcoming", return_value=[bad, good]), patch.object(
        DismissalDetailsService, "build", side_effect=_live_details,
    ):
        worker._run_once()
    snapshots = {row.worker_key: row for row in db.query(DismissalDetailsSnapshot)}
    assert snapshots["worker"].status == "error"
    assert snapshots["second"].status == "ready"
    snapshots["worker"].last_attempt_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    db.commit()
    with patch.object(UpcomingDismissalService, "list_upcoming", return_value=[_candidate(), good]), patch.object(
        DismissalDetailsService, "build", side_effect=_live_details,
    ):
        worker._run_once()
    db.expire_all()
    assert snapshots["worker"].status == "ready"
    db.close()
    engine.dispose()


def test_refreshing_state_is_saved_before_external_checks_and_can_recover_after_restart():
    engine, db = _database()
    cache = DismissalDetailsCacheService(FakeSettings(), db)
    candidate = _candidate()

    def build(person):
        with Session(engine) as reader:
            snapshot = reader.query(DismissalDetailsSnapshot).one()
            assert snapshot.status == "refreshing"
            assert snapshot.last_attempt_at is not None
        return _live_details(person)

    with patch.object(DismissalDetailsService, "build", side_effect=build):
        snapshot = cache.refresh(candidate)
    assert snapshot.status == "ready"
    snapshot.status = "refreshing"
    snapshot.last_attempt_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    assert cache.needs_refresh(candidate)
    assert cache.view(candidate)["snapshot_state"] == "stale"
    with patch.object(DismissalDetailsService, "build", side_effect=_live_details):
        cache.refresh(candidate)
    assert cache.view(candidate)["snapshot_state"] == "ready"
    db.close()
    engine.dispose()


def test_pending_for_days_is_displayed_as_delayed_not_scheduled():
    engine, db = _database()
    cache = DismissalDetailsCacheService(FakeSettings(), db)
    snapshot = cache.enqueue(_candidate())
    snapshot.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    db.commit()
    view = cache.view(_candidate())
    assert view["snapshot_state"] == "delayed"
    assert "10 минут" in view["snapshot_error"]
    db.close()
    engine.dispose()


def test_new_snapshots_take_priority_over_periodic_refreshes():
    engine, db = _database()
    old = _candidate()
    new = {**old, "worker_key": "new-worker", "preliminary": True}
    cache = DismissalDetailsCacheService(FakeSettings(), db)
    with patch.object(DismissalDetailsService, "build", side_effect=_live_details):
        snapshot = cache.refresh(old)
    snapshot.checked_at = snapshot.last_attempt_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    order = []

    def build(person):
        order.append(person["worker_key"])
        return _live_details(person)

    worker = DismissalDetailsSnapshotWorker(FakeSettings(), sessionmaker(engine))
    with patch.object(UpcomingDismissalService, "list_upcoming", return_value=[old, new]) as candidates, patch.object(
        DismissalDetailsService, "build", side_effect=build,
    ):
        worker._run_once()
    candidates.assert_called_once_with(limit=None)
    assert order == ["new-worker", "worker"]
    db.close()
    engine.dispose()


def test_preliminary_notification_is_discovered_without_opening_details():
    engine, db = _database()
    day = date.today() + timedelta(days=3)
    item = PreliminaryDismissalItem(
        worker_key="notification-worker", source_id="org.ru", source_name="Организация",
        fio="Иванов Иван", normalized_fio="иванов иван", dismissal_date=day, status="active",
    )
    db.add(item)
    db.commit()
    worker = DismissalDetailsSnapshotWorker(FakeSettings(), sessionmaker(engine))
    # Only remote lookups are mocked. Discovery reads the real notification
    # table; no HTTP request or call to view/enqueue is made by an operator.
    with patch.object(DismissalDetailsService, "build", side_effect=_live_details) as build:
        worker._run_once()
    assert build.call_count == 1
    assert build.call_args.args[0]["preliminary"] is True
    snapshot = db.query(DismissalDetailsSnapshot).one()
    assert snapshot.worker_key == "notification-worker"
    assert snapshot.dismissal_date == day
    assert snapshot.status == "ready"
    db.close()
    engine.dispose()


def test_all_candidates_can_be_discovered_without_changing_ui_limit():
    engine, db = _database()
    service = UpcomingDismissalService(FakeSettings(), db)
    candidates = list(range(1005))
    with patch.object(service, "ensure_primary_employment_state"), patch.object(service, "_all_candidates", return_value=candidates):
        assert len(service.list_upcoming()) == 20
        assert len(service.list_upcoming(limit=None)) == 1005
    db.close()
    engine.dispose()


@pytest.mark.parametrize("state", ["pending", "refreshing", "delayed", "waiting_import", "error", "stale", "ready"])
def test_details_template_renders_all_cache_states(state):
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app/templates"),
                      undefined=StrictUndefined, autoescape=True)
    details = {**_candidate(), "preliminary": False, "rows": [], "snapshot_state": state,
               "snapshot_checked_at": "", "snapshot_attempt_at": "11.09.2026 12:00",
               "snapshot_error": "<network error>"}
    html = env.get_template("upcoming_dismissal_details.html").render(details=details, details_error="")
    assert "Результата проверки пока нет" in html
    if state == "delayed":
        assert "Фоновая проверка задерживается" in html
        assert "Фоновая проверка запланирована" not in html
        assert "&lt;network error&gt;" in html


def test_preliminary_details_never_claim_blocking_before_hr_confirmation():
    engine, db = _database()
    candidate = {
        **_candidate(),
        "preliminary": True,
        "blocking_required": False,
        "deferred": True,
        "effective_block_date": date(2026, 8, 28),
    }

    row = DismissalDetailsService(FakeSettings(), db)._automatic_blocking(
        candidate
    )

    assert row["value"] == "Ожидает кадрового подтверждения"
    assert "не ранее 28.08.2026 19:10" in row["note"]
    assert "не блокируются" in row["note"]

    db.close()
    engine.dispose()
