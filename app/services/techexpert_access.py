from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_techexpert import TechExpertSettings
from app.services.ad import ADDirectoryUser, ActiveDirectoryService


DEPARTMENT_SEPARATOR = " / "


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
