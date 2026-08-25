from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, HRSourceRecord
from app.models_dismissals import DismissalDeferral
from app.models_dismissal_lifecycle import (
    FinalDismissalAutomationState,
    FinalDismissalBlockRun,
)
from app.models_onec_sources import HREmploymentState, OneCAdditionalSource
from app.models_preliminary_dismissals import PreliminaryDismissalItem
from app.services.hr_employment import sync_workbook_employment
from app.services.onec_xlsx import parse_onec_xlsx


DEFERRAL_ACTION = "final_dismissal_deferred"
DEFERRAL_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


class UpcomingDismissalService:
    """Окончательные увольнения с учетом всех должностей и организаций."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    @property
    def today(self) -> date:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).date()

    def ensure_primary_employment_state(self) -> None:
        """Однократный backfill для основного источника после установки патча.

        Новые импорты обновляют HREmploymentState через HRRegistryService.
        Этот путь нужен только если current.xlsx был импортирован до появления
        общей модели дат увольнения.
        """
        primary = self.db.scalar(
            select(OneCAdditionalSource)
            .where(OneCAdditionalSource.is_primary.is_(True))
            .order_by(OneCAdditionalSource.id)
        )
        if primary is None or not primary.source_id:
            return

        source_id = primary.source_id
        record_count = int(
            self.db.scalar(
                select(func.count(HRSourceRecord.id)).where(
                    HRSourceRecord.source_id == source_id,
                    HRSourceRecord.is_present.is_(True),
                )
            )
            or 0
        )
        if record_count == 0:
            return

        employment_count = int(
            self.db.scalar(
                select(func.count(HREmploymentState.id)).where(
                    HREmploymentState.source_id == source_id,
                    HREmploymentState.is_present.is_(True),
                )
            )
            or 0
        )
        if employment_count >= record_count:
            return

        current_file = Path(self.settings.onec_data_dir) / "current.xlsx"
        if not current_file.is_file():
            return

        workbook = parse_onec_xlsx(
            current_file,
            hash_secret=(
                self.settings.onec_worker_hash_secret.strip()
                or self.settings.app_secret_key
            ),
            header_search_rows=self.settings.onec_header_search_rows,
        )
        sync_workbook_employment(
            self.db,
            workbook=workbook,
            source_id=source_id,
            source_name=primary.name,
            timezone_name=self.settings.app_timezone,
        )
        self.db.commit()

    @staticmethod
    def _placements(record: HRSourceRecord | None) -> list[str]:
        if record is None:
            return []
        try:
            raw = json.loads(record.placements_json or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []

        result: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            department = " ".join(str(item.get("department") or "").split())
            position = " ".join(str(item.get("position") or "").split())
            text = " – ".join(part for part in (department, position) if part)
            if text and text not in result:
                result.append(text)
        return result

    def _source_names(self) -> dict[str, str]:
        rows = list(
            self.db.scalars(
                select(OneCAdditionalSource).order_by(
                    OneCAdditionalSource.is_primary.desc(),
                    OneCAdditionalSource.name,
                )
            ).all()
        )
        return {
            row.source_id: str(row.name or row.source_id).strip() or row.source_id
            for row in rows
            if row.source_id
        }

    def _all_candidates(self, *, include_expired: bool = False) -> list[dict]:
        employment_rows = list(
            self.db.scalars(
                select(HREmploymentState).order_by(
                    HREmploymentState.worker_key,
                    HREmploymentState.source_id,
                )
            ).all()
        )

        by_worker: dict[str, list[HREmploymentState]] = defaultdict(list)
        for row in employment_rows:
            by_worker[row.worker_key].append(row)

        worker_keys = list(by_worker)
        source_records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key.in_(worker_keys)
                )
            ).all()
        )
        records_by_pair = {
            (record.worker_key, normalize(record.source_id)): record
            for record in source_records
        }
        records_by_worker: dict[str, list[HRSourceRecord]] = defaultdict(list)
        for record in source_records:
            records_by_worker[record.worker_key].append(record)

        deferrals = list(
            self.db.scalars(select(DismissalDeferral)).all()
        )
        deferral_by_pair = {
            (item.worker_key, item.dismissal_date): item
            for item in deferrals
        }
        block_runs = list(
            self.db.scalars(
                select(FinalDismissalBlockRun).where(
                    FinalDismissalBlockRun.worker_key.in_(worker_keys)
                )
            ).all()
        )
        block_run_by_pair = {
            (item.worker_key, item.dismissal_date): item
            for item in block_runs
        }
        automation_state = self.db.scalar(
            select(FinalDismissalAutomationState)
            .order_by(FinalDismissalAutomationState.id)
            .limit(1)
        )
        source_names = self._source_names()
        today = self.today
        candidates: list[dict] = []

        for worker_key, states in by_worker.items():
            # Любая продолжающаяся занятость в любой организации защищает
            # общие учетные записи от окончательного увольнения.
            final_dismissal = not any(
                state.status == "active" for state in states
            )

            explicit_dates = [
                state.dismissal_date
                for state in states
                if state.dismissal_date is not None
            ]
            if not explicit_dates:
                continue

            final_date = max(explicit_dates)
            deferral = deferral_by_pair.get((worker_key, final_date))
            deferred_until = deferral.deferred_until if deferral else None
            if not final_dismissal:
                deferral = None
                deferred_until = None

            # Объект остается в «Ближайших увольнениях», пока не завершилась
            # автоблокировка. После успешного завершения он сразу исчезает,
            # а в журнале появится по прежнему правилу с начала следующего дня.
            block_run = (
                block_run_by_pair.get((worker_key, final_date))
                if final_dismissal
                else None
            )
            if (
                block_run is not None
                and block_run.status == "success"
                and block_run.completed_at is not None
            ):
                continue
            if (
                not include_expired
                and final_date < today
                and (
                    deferred_until is None
                    or deferred_until < today
                )
            ):
                if not final_dismissal:
                    continue
                else:
                    historical_before_automation = bool(
                        block_run is None
                        and (
                            automation_state is None
                            or final_date < automation_state.activated_on
                        )
                    )
                    if historical_before_automation:
                        continue

            worker_records = records_by_worker.get(worker_key, [])
            preferred_records = sorted(
                worker_records,
                key=lambda item: (
                    not item.is_present,
                    not bool(item.login.strip()),
                    not bool(item.corporate_email.strip()),
                ),
            )
            preferred = preferred_records[0] if preferred_records else None

            fio = next(
                (
                    str(state.fio or "").strip()
                    for state in states
                    if str(state.fio or "").strip()
                ),
                preferred.fio if preferred is not None else "",
            )
            login = next(
                (
                    record.login.strip().lower()
                    for record in preferred_records
                    if record.login.strip()
                ),
                "",
            )
            email = next(
                (
                    record.corporate_email.strip().lower()
                    for record in preferred_records
                    if record.corporate_email.strip()
                ),
                "",
            )

            organizations: list[dict] = []
            visible_states = [
                state
                for state in states
                if state.is_present
                or state.status == "scheduled"
                or state.dismissal_date == final_date
            ]
            for state in sorted(
                visible_states,
                key=lambda item: (
                    item.dismissal_date or date.min,
                    item.source_id,
                ),
            ):
                source_id = normalize(state.source_id)
                record = records_by_pair.get((worker_key, source_id))
                organizations.append(
                    {
                        "source_id": source_id,
                        "source_name": source_names.get(
                            source_id,
                            str(state.source_name or source_id),
                        ),
                        "dismissal_date": state.dismissal_date,
                        "status": state.status,
                        "is_present": state.is_present,
                        "placements": self._placements(record),
                    }
                )

            effective_block_date = (
                max(final_date, deferred_until or final_date)
                if final_dismissal
                else final_date
            )
            days_until = (final_date - today).days
            if days_until == 0:
                timing_label = "Сегодня"
            elif days_until == 1:
                timing_label = "Завтра"
            elif days_until > 1:
                timing_label = f"Через {days_until} дн."
            else:
                timing_label = "Дата увольнения прошла"

            candidates.append(
                {
                    "worker_key": worker_key,
                    "fio": fio,
                    "login": login,
                    "email": email,
                    "dismissal_date": final_date,
                    "timing_label": timing_label,
                    "organizations": organizations,
                    "preliminary": False,
                    "final_dismissal": final_dismissal,
                    "blocking_required": final_dismissal,
                    "deferral_allowed": bool(
                        final_dismissal
                        and not (
                            block_run is not None
                            and block_run.status == "success"
                        )
                    ),
                    "deferred": bool(
                        final_dismissal
                        and deferred_until is not None
                        and deferred_until > final_date
                    ),
                    "deferred_until": deferred_until,
                    "effective_block_date": effective_block_date,
                    "blocking_completed": bool(
                        block_run is not None
                        and block_run.status == "success"
                    ),
                    "blocking_completed_at": (
                        block_run.completed_at
                        if block_run is not None
                        and block_run.status == "success"
                        else None
                    ),
                    "deferral_count": (
                        int(deferral.deferral_count or 0)
                        if deferral is not None
                        else 0
                    ),
                    "deferral_operator": (
                        deferral.operator_username
                        if deferral is not None
                        else ""
                    ),
                }
            )

        candidate_worker_keys = {
            str(item.get("worker_key") or "") for item in candidates
        }
        confirmed_pairs = {
            (state.worker_key, normalize(state.source_id))
            for state in employment_rows
            if state.dismissal_date is not None
        }
        preliminary_items = list(
            self.db.scalars(
                select(PreliminaryDismissalItem).where(
                    PreliminaryDismissalItem.status == "active",
                    PreliminaryDismissalItem.worker_key != "",
                    PreliminaryDismissalItem.dismissal_date >= today,
                )
            ).all()
        )
        preliminary_worker_keys = {
            item.worker_key for item in preliminary_items if item.worker_key
        }
        preliminary_records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key.in_(preliminary_worker_keys)
                )
            ).all()
        ) if preliminary_worker_keys else []
        preliminary_records_by_pair = {
            (record.worker_key, normalize(record.source_id)): record
            for record in preliminary_records
        }

        for item in preliminary_items:
            source_id = normalize(item.source_id)
            if (item.worker_key, source_id) in confirmed_pairs:
                continue
            # При кадровом подтверждении этого же человека обычная строка уже
            # содержит все организации; предварительную рядом не дублируем.
            if item.worker_key in candidate_worker_keys:
                continue
            record = preliminary_records_by_pair.get(
                (item.worker_key, source_id)
            )
            login = record.login.strip().lower() if record is not None else ""
            email = (
                record.corporate_email.strip().lower()
                if record is not None
                else ""
            )
            try:
                departments = json.loads(item.departments_json or "[]")
            except (TypeError, json.JSONDecodeError):
                departments = []
            if not isinstance(departments, list):
                departments = []
            deferral = deferral_by_pair.get(
                (item.worker_key, item.dismissal_date)
            )
            deferred_until = deferral.deferred_until if deferral else None
            department_text = " / ".join(
                " ".join(str(value or "").split())
                for value in departments
                if " ".join(str(value or "").split())
            )
            placement = " – ".join(
                value
                for value in (department_text, " ".join(item.position.split()))
                if value
            )
            days_until = (item.dismissal_date - today).days
            if days_until == 0:
                timing_label = "Сегодня"
            elif days_until == 1:
                timing_label = "Завтра"
            else:
                timing_label = f"Через {days_until} дн."
            candidates.append(
                {
                    "worker_key": item.worker_key,
                    "fio": item.fio,
                    "login": login,
                    "email": email,
                    "dismissal_date": item.dismissal_date,
                    "timing_label": timing_label,
                    "organizations": [
                        {
                            "source_id": source_id,
                            "source_name": item.source_name or source_id,
                            "dismissal_date": item.dismissal_date,
                            "status": "preliminary",
                            "is_present": True,
                            "placements": [placement] if placement else [],
                        }
                    ],
                    "preliminary": True,
                    "final_dismissal": False,
                    "blocking_required": False,
                    "deferral_allowed": True,
                    "deferred": bool(
                        deferred_until is not None
                        and deferred_until > item.dismissal_date
                    ),
                    "deferred_until": deferred_until,
                    "effective_block_date": max(
                        item.dismissal_date,
                        deferred_until or item.dismissal_date,
                    ),
                    "blocking_completed": False,
                    "blocking_completed_at": None,
                    "deferral_count": (
                        int(deferral.deferral_count or 0)
                        if deferral is not None
                        else 0
                    ),
                    "deferral_operator": (
                        deferral.operator_username
                        if deferral is not None
                        else ""
                    ),
                }
            )

        candidates.sort(
            key=lambda item: (
                item["effective_block_date"],
                str(item["fio"]).casefold(),
            )
        )
        return candidates

    def list_upcoming(self, *, limit: int = 20) -> list[dict]:
        self.ensure_primary_employment_state()
        return self._all_candidates()[: max(1, int(limit))]

    def get_upcoming(
        self,
        *,
        worker_key: str,
        expected_dismissal_date: date,
    ) -> dict:
        """Вернуть актуального кандидата перед показом подробностей."""
        self.ensure_primary_employment_state()
        normalized_key = str(worker_key or "").strip()
        candidate = next(
            (
                item
                for item in self._all_candidates()
                if item["worker_key"] == normalized_key
            ),
            None,
        )
        if candidate is None:
            raise ValueError(
                "Работник больше не находится в ближайших увольнениях"
            )
        if candidate["dismissal_date"] != expected_dismissal_date:
            raise ValueError(
                "Дата увольнения изменилась. Обновите список и откройте подробности снова"
            )
        return candidate

    def list_for_blocking(self, *, limit: int = 10000) -> list[dict]:
        """Все окончательные увольнения, включая уже наступившие.

        Исторический backfill ограничивается датой фактического включения
        автоматизации в FinalDismissalLifecycleService.
        """
        self.ensure_primary_employment_state()
        return [
            item
            for item in self._all_candidates(include_expired=True)
            if item.get("blocking_required")
        ][: max(1, int(limit))]

    def defer(
        self,
        *,
        worker_key: str,
        expected_dismissal_date: date,
        operator_username: str,
    ) -> dict:
        self.ensure_primary_employment_state()
        normalized_key = str(worker_key or "").strip()
        candidate = next(
            (
                item
                for item in self._all_candidates()
                if item["worker_key"] == normalized_key
            ),
            None,
        )
        if candidate is None:
            raise ValueError(
                "Работник больше не находится в ближайших увольнениях"
            )
        if candidate["dismissal_date"] != expected_dismissal_date:
            raise ValueError(
                "Дата увольнения изменилась. Обновите журнал и проверьте запись снова"
            )
        if not candidate.get("deferral_allowed"):
            raise ValueError(
                "Отсрочка не требуется: работа в другой организации продолжается"
            )

        deferral = self.db.scalar(
            select(DismissalDeferral).where(
                DismissalDeferral.worker_key == normalized_key,
                DismissalDeferral.dismissal_date == expected_dismissal_date,
            )
        )
        previous_until = deferral.deferred_until if deferral else None
        base_date = max(
            expected_dismissal_date,
            previous_until or expected_dismissal_date,
        )
        if not candidate.get("preliminary"):
            base_date = max(self.today, base_date)
        deferred_until = base_date + timedelta(days=DEFERRAL_DAYS)

        if deferral is None:
            deferral = DismissalDeferral(
                worker_key=normalized_key,
                dismissal_date=expected_dismissal_date,
                deferred_until=deferred_until,
                operator_username=operator_username,
                deferral_count=1,
            )
            self.db.add(deferral)
        else:
            deferral.deferred_until = deferred_until
            deferral.operator_username = operator_username
            deferral.deferral_count = int(deferral.deferral_count or 0) + 1
            deferral.updated_at = utcnow()

        payload = {
            "version": 1,
            "worker_key": normalized_key,
            "fio": candidate["fio"],
            "login": candidate["login"],
            "corporate_email": candidate["email"],
            "dismissal_date": expected_dismissal_date.isoformat(),
            "previous_deferred_until": (
                previous_until.isoformat() if previous_until else ""
            ),
            "deferred_until": deferred_until.isoformat(),
            "days": DEFERRAL_DAYS,
            "preliminary": bool(candidate.get("preliminary")),
            "organizations": [
                {
                    "source_id": item["source_id"],
                    "source_name": item["source_name"],
                    "dismissal_date": (
                        item["dismissal_date"].isoformat()
                        if item["dismissal_date"] is not None
                        else ""
                    ),
                }
                for item in candidate["organizations"]
            ],
        }
        self.db.add(
            AuditLog(
                actor=operator_username,
                action=DEFERRAL_ACTION,
                target=normalized_key,
                result="deferred",
                details=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        self.db.refresh(deferral)

        return {
            **candidate,
            "deferred": True,
            "deferred_until": deferred_until,
            "effective_block_date": deferred_until,
            "deferral_count": deferral.deferral_count,
            "deferral_operator": operator_username,
        }
