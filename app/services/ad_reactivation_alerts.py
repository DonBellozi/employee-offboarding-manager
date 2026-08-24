from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissal_lifecycle import ADReactivationAlert
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.personnel_structure import PersonnelStructureService
from app.services.techexpert_access import normalize_email, normalize_fio


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ADReactivationAlertService:
    """Повторная проверка и восстановление AD после возврата работника."""

    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def _open_alert(self, alert_id: int) -> ADReactivationAlert:
        alert = self.db.get(ADReactivationAlert, int(alert_id))
        if alert is None:
            raise LookupError("Предупреждение AD не найдено")
        if alert.status != "open":
            raise ValueError("По этому предупреждению уже принято решение")
        return alert

    def _require_active(self, alert: ADReactivationAlert) -> None:
        if not PersonnelStructureService(self.db).active_anywhere(
            alert.worker_key
        ):
            raise ValueError(
                "Работник больше не активен в кадровых данных. "
                "Восстановление AD запрещено."
            )

    def _audit(
        self,
        alert: ADReactivationAlert,
        *,
        actor: str,
        action: str,
        result: str,
        candidate_count: int = 0,
        error: str = "",
    ) -> None:
        self.db.add(
            AuditLog(
                actor=actor,
                action=action,
                target=f"ad-reactivation:{alert.id}",
                result=result,
                details=json.dumps(
                    {
                        "worker_key": alert.worker_key,
                        "block_run_id": alert.block_run_id,
                        "dismissal_date": (
                            alert.dismissal_date.isoformat()
                            if alert.dismissal_date
                            else ""
                        ),
                        "ad_login": alert.ad_login,
                        "candidate_count": candidate_count,
                        "resolution": alert.resolution,
                        "error": error,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )

    @staticmethod
    def _candidate_key(user: ADDirectoryUser) -> str:
        return normalize_email(user.object_guid) or normalize_email(
            user.username
        )

    def _discover_candidates(
        self,
        alert: ADReactivationAlert,
        ad: ActiveDirectoryService,
    ) -> list[ADDirectoryUser]:
        candidates: dict[str, ADDirectoryUser] = {}

        def add(user: ADDirectoryUser | None) -> None:
            if user is None:
                return
            key = self._candidate_key(user)
            if key:
                candidates[key] = user

        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == alert.worker_key
                )
            ).all()
        )
        records = list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.worker_key == alert.worker_key
                )
            ).all()
        )
        guids = {
            normalize_email(value)
            for value in [
                alert.ad_object_guid,
                *(mapping.ad_object_guid for mapping in mappings),
            ]
            if normalize_email(value)
        }
        logins = {
            normalize_email(value)
            for value in [
                alert.ad_login,
                *(mapping.ad_login for mapping in mappings),
                *(record.login for record in records),
            ]
            if normalize_email(value)
        }
        emails = {
            normalize_email(value)
            for value in [
                *(record.corporate_email for record in records),
                *(mapping.source_email for mapping in mappings),
            ]
            if normalize_email(value)
        }

        for guid in sorted(guids):
            add(ad.get_user_by_object_guid(guid))
        for login in sorted(logins):
            add(ad.get_user(login))
        for email in sorted(emails):
            for user in ad.users_by_email(email, limit=5):
                add(user)

        fio = normalize_fio(alert.fio)
        if fio:
            for user in ad.search_users(alert.fio, limit=10):
                if normalize_fio(user.display_name) == fio:
                    add(user)

        return sorted(
            candidates.values(),
            key=lambda user: (
                not user.is_enabled,
                normalize_email(user.username),
            ),
        )

    @staticmethod
    def _candidate_payload(user: ADDirectoryUser) -> dict[str, object]:
        return {
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "object_guid": user.object_guid,
            "is_enabled": bool(user.is_enabled),
        }

    def _save_unique_mapping(
        self,
        alert: ADReactivationAlert,
        user: ADDirectoryUser,
    ) -> None:
        alert.ad_login = user.username
        alert.ad_object_guid = user.object_guid
        for mapping in self.db.scalars(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == alert.worker_key
            )
        ).all():
            mapping.ad_login = user.username
            if user.object_guid:
                mapping.ad_object_guid = user.object_guid
            mapping.last_verified_at = utcnow()

    def refresh(
        self,
        *,
        alert_id: int,
        actor: str,
    ) -> tuple[ADReactivationAlert, list[ADDirectoryUser]]:
        alert = self._open_alert(alert_id)
        self._require_active(alert)
        try:
            candidates = self._discover_candidates(
                alert,
                ActiveDirectoryService(self.settings),
            )
            alert.candidates_json = json.dumps(
                [self._candidate_payload(user) for user in candidates],
                ensure_ascii=False,
                sort_keys=True,
            )
            alert.last_checked_at = utcnow()
            alert.updated_at = utcnow()
            if len(candidates) == 1:
                self._save_unique_mapping(alert, candidates[0])
                alert.last_error = ""
            elif not candidates:
                alert.last_error = "Учетные записи AD работника не найдены"
            else:
                alert.last_error = (
                    f"Найдено несколько учетных записей AD: {len(candidates)}. "
                    "Автоматический выбор запрещён."
                )
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_refreshed",
                result="success" if len(candidates) == 1 else "attention",
                candidate_count=len(candidates),
                error=alert.last_error,
            )
            self.db.commit()
            self.db.refresh(alert)
            return alert, candidates
        except Exception as exc:
            alert.last_error = str(exc)[:4000]
            alert.last_checked_at = utcnow()
            alert.updated_at = utcnow()
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_refreshed",
                result="error",
                error=alert.last_error,
            )
            self.db.commit()
            raise

    def _restore_user(
        self,
        alert: ADReactivationAlert,
        user: ADDirectoryUser,
        *,
        actor: str,
        candidate_count: int,
    ) -> ADReactivationAlert:
        try:
            if self.settings.dry_run:
                self._audit(
                    alert,
                    actor=actor,
                    action="ad_reactivation_restored",
                    result="dry_run",
                    candidate_count=candidate_count,
                )
                self.db.commit()
                return alert

            ad = ActiveDirectoryService(self.settings)
            resolution = "already_enabled"
            if not user.is_enabled or user.is_expired:
                ad.reactivate_existing_user(user.distinguished_name)
                verified = (
                    ad.get_user_by_object_guid(user.object_guid)
                    if user.object_guid
                    else ad.get_user(user.username)
                )
                if (
                    verified is None
                    or not verified.is_enabled
                    or verified.is_expired
                ):
                    raise RuntimeError(
                        "AD не подтвердил включение учетной записи"
                    )
                user = verified
                resolution = "restored"
            self._save_unique_mapping(alert, user)
            alert.status = "resolved"
            alert.resolution = resolution
            alert.resolved_by = str(actor or "").strip()
            alert.resolved_at = utcnow()
            alert.last_error = ""
            alert.updated_at = utcnow()
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_restored",
                result="success",
                candidate_count=candidate_count,
            )
            self.db.commit()
            self.db.refresh(alert)
            return alert
        except Exception as exc:
            alert.last_error = str(exc)[:4000]
            alert.updated_at = utcnow()
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_restored",
                result="error",
                candidate_count=candidate_count,
                error=alert.last_error,
            )
            self.db.commit()
            raise

    def restore(self, *, alert_id: int, actor: str) -> ADReactivationAlert:
        alert, candidates = self.refresh(alert_id=alert_id, actor=actor)
        if len(candidates) != 1:
            error = (
                alert.last_error
                or "Не удалось однозначно определить учетную запись AD"
            )
            alert.last_error = error
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_restored",
                result="error",
                candidate_count=len(candidates),
                error=error,
            )
            self.db.commit()
            raise ValueError(error)
        return self._restore_user(
            alert,
            candidates[0],
            actor=actor,
            candidate_count=1,
        )

    def restore_candidate(
        self,
        *,
        alert_id: int,
        ad_login: str,
        ad_object_guid: str,
        actor: str,
    ) -> ADReactivationAlert:
        alert, candidates = self.refresh(alert_id=alert_id, actor=actor)
        wanted_guid = normalize_email(ad_object_guid)
        wanted_login = normalize_email(ad_login)
        selected = [
            user
            for user in candidates
            if (
                wanted_guid
                and normalize_email(user.object_guid) == wanted_guid
            )
            or (
                not wanted_guid
                and normalize_email(user.username) == wanted_login
            )
        ]
        if len(selected) != 1:
            error = (
                "Выбранная учетная запись больше не найдена или определяется "
                "неоднозначно. Обновите сведения ещё раз."
            )
            alert.last_error = error
            self._audit(
                alert,
                actor=actor,
                action="ad_reactivation_restored",
                result="error",
                candidate_count=len(candidates),
                error=error,
            )
            self.db.commit()
            raise ValueError(error)
        return self._restore_user(
            alert,
            selected[0],
            actor=actor,
            candidate_count=len(candidates),
        )
