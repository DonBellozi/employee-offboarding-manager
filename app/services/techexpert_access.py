from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import TechExpertSettings
from app.services.ad import ADDirectoryUser, ActiveDirectoryService


DEPARTMENT_SEPARATOR = " / "
ACTIVE_EMPLOYMENT_STATUSES = {"active", "scheduled"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.replace("\u00a0", " ").split()).strip()


def normalize_fio(value: object) -> str:
    return normalize_text(value).casefold().replace("ё", "е")


def normalize_email(value: object) -> str:
    return normalize_text(value).casefold()


def placement_snapshot(record: HRSourceRecord | None) -> dict[str, list[str]]:
    if record is None:
        return {"positions": [], "departments": [], "top_departments": []}
    try:
        values = json.loads(record.placements_json or "[]")
    except (TypeError, json.JSONDecodeError):
        values = []

    positions: list[str] = []
    departments: list[str] = []
    top_departments: list[str] = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        position = normalize_text(item.get("position"))
        department = normalize_text(item.get("department"))
        if position and position not in positions:
            positions.append(position)
        if department and department not in departments:
            departments.append(department)
        top = department.split(DEPARTMENT_SEPARATOR, 1)[0].strip()
        if top and top not in top_departments:
            top_departments.append(top)
    return {
        "positions": positions,
        "departments": departments,
        "top_departments": top_departments,
    }


class TechExpertGroupAccessService:
    """Синхронизирует прямой состав группы AD с кадровой организацией."""

    def __init__(
        self,
        settings: Settings,
        db: Session,
        config: TechExpertSettings,
    ):
        self.settings = settings
        self.db = db
        self.config = config

    @property
    def source_id(self) -> str:
        return normalize_email(self.config.source_domain)

    def _records(self) -> list[HRSourceRecord]:
        if not self.source_id:
            return []
        return list(
            self.db.scalars(
                select(HRSourceRecord).where(
                    HRSourceRecord.source_id == self.source_id
                )
            ).all()
        )

    def search_active_workers(
        self,
        query: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Найти действующих работников организации для ручной привязки."""

        normalized_query = normalize_fio(query)
        if len(normalized_query) < 2 or not self.source_id:
            return []
        active_keys = set(
            self.db.scalars(
                select(HREmploymentState.worker_key).where(
                    HREmploymentState.source_id == self.source_id,
                    HREmploymentState.status.in_(
                        ACTIVE_EMPLOYMENT_STATUSES
                    ),
                    HREmploymentState.is_present.is_(True),
                )
            ).all()
        )
        result: list[dict[str, object]] = []
        for record in self.db.scalars(
            select(HRSourceRecord)
            .where(
                HRSourceRecord.source_id == self.source_id,
                HRSourceRecord.is_present.is_(True),
            )
            .order_by(HRSourceRecord.fio)
        ).all():
            if record.worker_key not in active_keys:
                continue
            if normalized_query not in normalize_fio(record.fio):
                continue
            snapshot = placement_snapshot(record)
            result.append(
                {
                    "record_id": record.id,
                    "fio": record.fio,
                    "corporate_email": record.corporate_email,
                    "positions": snapshot["positions"],
                    "top_departments": snapshot["top_departments"],
                }
            )
            if len(result) >= max(1, min(int(limit), 50)):
                break
        return result

    @staticmethod
    def _add(
        index: dict[str, set[str]],
        value: str,
        worker_key: str,
    ) -> None:
        if value and worker_key:
            index.setdefault(value, set()).add(worker_key)

    def _match_members(
        self,
        members: list[ADDirectoryUser],
        records: list[HRSourceRecord],
    ) -> tuple[dict[str, ADDirectoryUser], list[dict[str, str]]]:
        worker_keys = {
            str(record.worker_key or "").strip()
            for record in records
            if str(record.worker_key or "").strip()
        }
        mappings = (
            list(
                self.db.scalars(
                    select(EmailLoginMapping).where(
                        EmailLoginMapping.worker_key.in_(worker_keys)
                    )
                ).all()
            )
            if worker_keys
            else []
        )

        by_guid: dict[str, set[str]] = {}
        by_login: dict[str, set[str]] = {}
        by_email: dict[str, set[str]] = {}
        by_fio: dict[str, set[str]] = {}
        for mapping in mappings:
            key = str(mapping.worker_key or "").strip()
            self._add(by_guid, normalize_email(mapping.ad_object_guid), key)
            self._add(by_login, normalize_email(mapping.ad_login), key)
        for record in records:
            key = str(record.worker_key or "").strip()
            self._add(by_login, normalize_email(record.login), key)
            self._add(by_email, normalize_email(record.corporate_email), key)
            self._add(by_email, normalize_email(record.personal_email), key)
            self._add(by_fio, normalize_fio(record.fio), key)

        matched: dict[str, ADDirectoryUser] = {}
        issues: list[dict[str, str]] = []
        for member in members:
            methods = (
                ("objectGUID", by_guid.get(normalize_email(member.object_guid), set())),
                ("логин AD", by_login.get(normalize_email(member.username), set())),
                ("e-mail", by_email.get(normalize_email(member.email), set())),
                ("ФИО", by_fio.get(normalize_fio(member.display_name), set())),
            )
            selected: set[str] = set()
            method = ""
            for candidate_method, keys in methods:
                if keys:
                    selected = set(keys)
                    method = candidate_method
                    break
            if len(selected) == 1:
                worker_key = next(iter(selected))
                if worker_key in matched:
                    issues.append(
                        {
                            "ad_login": member.username,
                            "ad_object_guid": member.object_guid,
                            "display_name": member.display_name,
                            "reason": "Для работника найдено несколько участников группы AD",
                        }
                    )
                    continue
                matched[worker_key] = member
            else:
                issues.append(
                    {
                        "ad_login": member.username,
                        "ad_object_guid": member.object_guid,
                        "display_name": member.display_name,
                        "reason": (
                            f"Неоднозначное сопоставление по {method}"
                            if selected
                            else "Кадровая запись организации не найдена"
                        ),
                    }
                )
        return matched, issues

    def sync(self, *, actor: str = "system") -> dict[str, object]:
        if not self.source_id:
            raise ValueError("Не выбрана организация Техэксперта")
        group_dn = str(self.config.ad_group_dn or "").strip()
        if not group_dn:
            raise ValueError("Не настроена группа AD Техэксперта")

        # Сначала полностью читаем группу. При ошибке кадровые отметки не
        # изменяются, поэтому временная недоступность AD не стирает доступы.
        members = ActiveDirectoryService(self.settings).group_members(group_dn)
        records = self._records()
        matched, issues = self._match_members(members, records)

        changed: list[dict[str, object]] = []
        for record in records:
            expected = record.worker_key in matched
            if bool(record.techexpert_access) == expected:
                continue
            previous = bool(record.techexpert_access)
            record.techexpert_access = expected
            changed.append(
                {
                    "record_id": record.id,
                    "worker_key": record.worker_key,
                    "fio": record.fio,
                    "previous": previous,
                    "current": expected,
                    "ad_login": (
                        matched[record.worker_key].username
                        if expected
                        else ""
                    ),
                }
            )

        result = {
            "source_id": self.source_id,
            "members": len(members),
            "matched": len(matched),
            "access_count": sum(
                bool(record.techexpert_access) for record in records
            ),
            "changed": len(changed),
            "changes": changed,
            "issues": issues,
            "checked_at": utcnow().isoformat(),
        }
        # Фоновый цикл не засоряет аудит неизменившимися снимками. Ручная
        # проверка оператора фиксируется всегда.
        if changed or issues or actor != "system":
            self.db.add(
                AuditLog(
                    actor=actor,
                    action="techexpert_group_sync",
                    target=self.source_id,
                    result="issues" if issues else "success",
                    details=json.dumps(result, ensure_ascii=False, sort_keys=True),
                )
            )
        self.db.commit()
        return result

    def remove_unmatched_member(
        self,
        *,
        ad_login: str,
        ad_object_guid: str,
        actor: str,
    ) -> dict[str, object]:
        """Удалить только подтверждённо несопоставленного участника группы."""

        if not self.source_id:
            raise ValueError("Не выбрана организация Техэксперта")
        group_dn = str(self.config.ad_group_dn or "").strip()
        if not group_dn:
            raise ValueError("Не настроена группа AD Техэксперта")
        wanted_guid = normalize_email(ad_object_guid)
        wanted_login = normalize_email(ad_login)
        if not wanted_guid and not wanted_login:
            raise ValueError("Не передан участник группы AD")

        ad = ActiveDirectoryService(self.settings)
        members = ad.group_members(group_dn)
        targets = [
            member
            for member in members
            if (
                wanted_guid
                and normalize_email(member.object_guid) == wanted_guid
            )
            or (
                not wanted_guid
                and normalize_email(member.username) == wanted_login
            )
        ]
        if len(targets) != 1:
            raise ValueError(
                "Участник группы не найден или определяется неоднозначно. "
                "Сначала обновите сведения из AD."
            )
        target = targets[0]
        matched, _issues = self._match_members(members, self._records())
        matched_keys = {
            normalize_email(user.object_guid) or normalize_email(user.username)
            for user in matched.values()
        }
        target_key = normalize_email(target.object_guid) or normalize_email(
            target.username
        )
        if target_key in matched_keys:
            raise ValueError(
                "Участник уже сопоставлен с работником. Удаление через список "
                "ошибок запрещено."
            )

        state = ad.remove_user_from_group(
            target.username,
            group_dn,
            object_guid=target.object_guid,
        )
        if state != "dry_run" and ad.is_user_member_of_group(
            target.username,
            group_dn,
            object_guid=target.object_guid,
        ):
            raise RuntimeError("AD не подтвердил удаление участника из группы")
        self.db.add(
            AuditLog(
                actor=actor,
                action="techexpert_unmatched_member_removed",
                target=target.username,
                result=state,
                details=json.dumps(
                    {
                        "source_id": self.source_id,
                        "ad_login": target.username,
                        "ad_object_guid": target.object_guid,
                        "display_name": target.display_name,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        if state != "dry_run":
            self.sync(actor=actor)
        return {
            "state": state,
            "ad_login": target.username,
            "display_name": target.display_name,
        }

    def match_unmatched_member(
        self,
        *,
        record_id: int,
        ad_login: str,
        ad_object_guid: str,
        actor: str,
    ) -> dict[str, object]:
        """Связать несопоставленного участника группы с кадровым работником."""

        if not self.source_id:
            raise ValueError("Не выбрана организация Техэксперта")
        group_dn = str(self.config.ad_group_dn or "").strip()
        if not group_dn:
            raise ValueError("Не настроена группа AD Техэксперта")

        record = self.db.get(HRSourceRecord, int(record_id))
        if (
            record is None
            or normalize_email(record.source_id) != self.source_id
            or not record.is_present
        ):
            raise LookupError(
                "Работник организации Техэксперта не найден в текущих кадрах"
            )
        employment = self.db.scalar(
            select(HREmploymentState).where(
                HREmploymentState.worker_key == record.worker_key,
                HREmploymentState.source_id == self.source_id,
            )
        )
        if (
            employment is None
            or employment.status not in ACTIVE_EMPLOYMENT_STATUSES
            or not employment.is_present
        ):
            raise ValueError(
                "Выбранный работник больше не активен в организации "
                "Техэксперта"
            )

        wanted_guid = normalize_email(ad_object_guid)
        wanted_login = normalize_email(ad_login)
        if not wanted_guid and not wanted_login:
            raise ValueError("Не передан участник группы AD")
        ad = ActiveDirectoryService(self.settings)
        members = ad.group_members(group_dn)
        targets = [
            member
            for member in members
            if (
                wanted_guid
                and normalize_email(member.object_guid) == wanted_guid
            )
            or (
                not wanted_guid
                and normalize_email(member.username) == wanted_login
            )
        ]
        if len(targets) != 1:
            raise ValueError(
                "Участник группы не найден или определяется неоднозначно. "
                "Сначала обновите сведения из AD."
            )
        target = targets[0]
        if not target.object_guid:
            raise ValueError(
                "AD не вернул objectGUID участника. Надёжное сопоставление "
                "сохранить нельзя."
            )
        target_key = normalize_email(target.object_guid) or normalize_email(
            target.username
        )
        matched, _issues = self._match_members(members, self._records())
        for worker_key, member in matched.items():
            member_key = normalize_email(member.object_guid) or normalize_email(
                member.username
            )
            if member_key != target_key:
                continue
            if worker_key != record.worker_key:
                raise ValueError(
                    "Эта учетная запись AD уже сопоставлена с другим "
                    "работником"
                )
            result = self.sync(actor=actor)
            return {
                "state": "already_mapped",
                "ad_login": target.username,
                "fio": record.fio,
                "issues": len(result["issues"]),
            }

        conflict = next(
            (
                item
                for item in self.db.scalars(
                    select(EmailLoginMapping).where(
                        EmailLoginMapping.worker_key != record.worker_key
                    )
                ).all()
                if normalize_email(item.ad_object_guid)
                == normalize_email(target.object_guid)
                or normalize_email(item.ad_login)
                == normalize_email(target.username)
            ),
            None,
        )
        if conflict is not None:
            raise ValueError(
                "Эта учетная запись AD уже сохранена у другого работника"
            )

        mapping = self.db.scalar(
            select(EmailLoginMapping).where(
                EmailLoginMapping.worker_key == record.worker_key,
                EmailLoginMapping.source_domain == self.source_id,
            )
        )
        created = mapping is None
        if mapping is None:
            mapping = EmailLoginMapping(
                worker_key=record.worker_key,
                source_domain=self.source_id,
                source_email=normalize_email(record.corporate_email),
                ad_object_guid="",
                ad_login="",
                zimbra_id="",
                zimbra_email="",
                created_by=actor,
            )
            self.db.add(mapping)
        previous_login = mapping.ad_login
        previous_guid = mapping.ad_object_guid
        if record.corporate_email:
            mapping.source_email = normalize_email(record.corporate_email)
        mapping.ad_login = target.username
        mapping.ad_object_guid = target.object_guid
        mapping.last_verified_at = utcnow()
        mapping.updated_at = utcnow()
        record.techexpert_access = True
        self.db.add(
            AuditLog(
                actor=actor,
                action="techexpert_group_member_mapped",
                target=target.username,
                result="created" if created else "updated",
                details=json.dumps(
                    {
                        "source_id": self.source_id,
                        "record_id": record.id,
                        "worker_key": record.worker_key,
                        "fio": record.fio,
                        "ad_login": target.username,
                        "ad_object_guid": target.object_guid,
                        "previous_ad_login": previous_login,
                        "previous_ad_object_guid": previous_guid,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        result = self.sync(actor=actor)
        return {
            "state": "mapped",
            "ad_login": target.username,
            "fio": record.fio,
            "issues": len(result["issues"]),
        }
