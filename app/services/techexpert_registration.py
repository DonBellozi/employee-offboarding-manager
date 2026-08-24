from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuditLog,
    DomainAccessUser,
    EmailLoginMapping,
    HRSourceRecord,
)
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import (
    TechExpertRegistrationRequest,
    TechExpertSettings,
)
from app.services.ad import ADDirectoryUser, ActiveDirectoryService
from app.services.mailer import (
    CredentialMailer,
    get_domain_mail_profile,
    render_mail_template,
)
from app.services.techexpert_access import normalize_email, normalize_fio, normalize_text
from app.services.techexpert_settings import normalize_email as validate_email


ACTIVE_EMPLOYMENT_STATUSES = {"active", "scheduled"}
DEPARTMENT_SEPARATOR = " / "


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def preview_document(body_html: str) -> str:
    body = str(body_html or "").strip()
    if not body:
        body = '<p style="color:#64748b">Письмо не сформировано.</p>'
    lowered = body.casefold()
    if "<!doctype" in lowered or "<html" in lowered:
        return body
    return (
        "<!doctype html>\n"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "</head>"
        '<body style="margin:24px;color:#172033;'
        'font-family:Arial,sans-serif;line-height:1.5">'
        f"{body}</body></html>"
    )


def operator_mail_identity(
    db: Session,
    settings: Settings,
    *,
    actor: str,
    actor_source: str,
) -> dict[str, str]:
    """Получить безопасную подпись оператора из назначенного AD-доступа."""

    login = normalize_email(actor)
    display_name = ""
    email = ""
    if actor_source == "ad" and login:
        access = db.scalar(
            select(DomainAccessUser).where(
                DomainAccessUser.username == login,
                DomainAccessUser.is_active.is_(True),
            )
        )
        if access is not None:
            display_name = normalize_text(access.display_name)
            email = normalize_email(access.email)

        # Данные сохраняются при назначении доступа. Если старое назначение
        # не содержит ФИО или почту, аккуратно дополняем подпись живым чтением
        # AD, но не блокируем письмо из-за этой вспомогательной проверки.
        if not display_name or not email:
            try:
                directory_user = ActiveDirectoryService(settings).get_user(
                    login
                )
                if directory_user is not None:
                    display_name = (
                        normalize_text(directory_user.display_name)
                        or display_name
                    )
                    email = normalize_email(directory_user.email) or email
            except Exception:
                pass

    return {
        "login": login,
        "display_name": display_name or login or "Оператор системы",
        "email": email,
    }


def append_operator_signature(
    body_html: str,
    identity: dict[str, str],
) -> str:
    """Добавить подпись оператора в HTML-письмо и его предпросмотр."""

    display_name = escape(
        normalize_text(identity.get("display_name")) or "Оператор системы"
    )
    email = escape(normalize_email(identity.get("email")))
    login = escape(normalize_email(identity.get("login")))
    contact = email or login
    contact_line = (
        f'<br><span style="color:#64748b">{contact}</span>'
        if contact and contact.casefold() != display_name.casefold()
        else ""
    )
    signature = (
        '<div style="margin-top:24px;padding-top:16px;'
        'border-top:1px solid #e2e8f0">'
        "С уважением,<br>"
        f"<strong>{display_name}</strong>"
        f"{contact_line}"
        "</div>"
    )
    body = str(body_html or "").rstrip()
    closing_body = body.casefold().rfind("</body>")
    if closing_body >= 0:
        return f"{body[:closing_body]}{signature}{body[closing_body:]}"
    return f"{body}\n{signature}"


class TechExpertRegistrationService:
    """Ручной запрос регистрации с предпросмотром и подтвержденным действием."""

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

    def _require_configuration(self) -> None:
        if not self.source_id:
            raise ValueError("В настройках Техэксперта не выбрана организация")
        if not str(self.config.ad_group_dn or "").strip():
            raise ValueError("В настройках Техэксперта не указана группа AD")
        if not str(self.config.recipient_email or "").strip():
            raise ValueError("Не указан получатель уведомлений Техэксперта")
        if not str(self.config.registration_subject or "").strip():
            raise ValueError("Не настроена тема письма о регистрации")
        if not str(self.config.registration_body_html or "").strip():
            raise ValueError("Не настроен шаблон письма о регистрации")

    def _active_states(self) -> dict[str, HREmploymentState]:
        if not self.source_id:
            return {}
        return {
            row.worker_key: row
            for row in self.db.scalars(
                select(HREmploymentState).where(
                    HREmploymentState.source_id == self.source_id,
                    HREmploymentState.status.in_(ACTIVE_EMPLOYMENT_STATUSES),
                    HREmploymentState.is_present.is_(True),
                )
            ).all()
        }

    @staticmethod
    def placements(record: HRSourceRecord) -> list[dict[str, object]]:
        try:
            raw = json.loads(record.placements_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw = []
        result: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for value in raw if isinstance(raw, list) else []:
            if not isinstance(value, dict):
                continue
            department = normalize_text(value.get("department"))
            position = normalize_text(value.get("position"))
            key = (department.casefold(), position.casefold())
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "index": len(result),
                    "department": department,
                    "top_department": (
                        department.split(DEPARTMENT_SEPARATOR, 1)[0].strip()
                        or "Без подразделения"
                    ),
                    "position": position,
                }
            )
        if not result:
            result.append(
                {
                    "index": 0,
                    "department": "",
                    "top_department": "Без подразделения",
                    "position": "",
                }
            )
        return result

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, object]]:
        self._require_configuration()
        normalized_query = normalize_fio(query)
        if len(normalized_query) < 2:
            return []
        states = self._active_states()
        records = self.db.scalars(
            select(HRSourceRecord)
            .where(
                HRSourceRecord.source_id == self.source_id,
                HRSourceRecord.is_present.is_(True),
            )
            .order_by(HRSourceRecord.fio)
        ).all()
        result: list[dict[str, object]] = []
        for record in records:
            state = states.get(record.worker_key)
            if state is None or normalized_query not in normalize_fio(record.fio):
                continue
            result.append(
                {
                    "record": record,
                    "placements": self.placements(record),
                    "employment_status": state.status,
                    "dismissal_date": state.dismissal_date,
                }
            )
            if len(result) >= max(1, min(int(limit), 50)):
                break
        return result

    def active_record(
        self,
        record_id: int,
    ) -> tuple[HRSourceRecord, HREmploymentState]:
        self._require_configuration()
        record = self.db.get(HRSourceRecord, int(record_id))
        if record is None or normalize_email(record.source_id) != self.source_id:
            raise LookupError("Работник организации Техэксперта не найден")
        state = self.db.scalar(
            select(HREmploymentState).where(
                HREmploymentState.worker_key == record.worker_key,
                HREmploymentState.source_id == self.source_id,
            )
        )
        if (
            state is None
            or state.status not in ACTIVE_EMPLOYMENT_STATUSES
            or not state.is_present
            or not record.is_present
        ):
            raise ValueError("Работник больше не активен в этой организации")
        return record, state

    def _resolve_ad(self, record: HRSourceRecord) -> ADDirectoryUser:
        mappings = list(
            self.db.scalars(
                select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == record.worker_key
                )
            ).all()
        )
        preferred = next(
            (
                value
                for value in mappings
                if normalize_email(value.source_domain) == self.source_id
            ),
            None,
        )
        guid = normalize_email(preferred.ad_object_guid) if preferred else ""
        login = normalize_email(preferred.ad_login) if preferred else ""
        if preferred is None:
            identities = {
                (
                    normalize_email(value.ad_object_guid),
                    normalize_email(value.ad_login),
                )
                for value in mappings
                if value.ad_object_guid or value.ad_login
            }
            if len(identities) == 1:
                guid, login = next(iter(identities))
            elif len(identities) > 1:
                raise ValueError("Для работника найдено несколько AD-сопоставлений")
            else:
                login = normalize_email(record.login)

        ad = ActiveDirectoryService(self.settings)
        user = ad.get_user_by_object_guid(guid) if guid else None
        if user is None and login:
            user = ad.get_user(login)
        if user is None and record.corporate_email:
            candidates = ad.users_by_email(record.corporate_email, limit=5)
            unique = {
                value.object_guid or value.username: value for value in candidates
            }
            if len(unique) == 1:
                user = next(iter(unique.values()))
        if user is None:
            candidates = [
                value
                for value in ad.search_users(record.fio, limit=10)
                if normalize_fio(value.display_name) == normalize_fio(record.fio)
            ]
            unique = {
                value.object_guid or value.username: value for value in candidates
            }
            if len(unique) == 1:
                user = next(iter(unique.values()))
        if user is None:
            raise ValueError("Для работника не найдена учетная запись AD")
        if not user.is_enabled:
            raise ValueError("Учетная запись AD работника отключена")
        return user

    def _is_group_member(self, user: ADDirectoryUser) -> bool:
        return ActiveDirectoryService(self.settings).is_user_member_of_group(
            user.username,
            self.config.ad_group_dn,
            object_guid=user.object_guid,
        )

    def _sync_access_marker(
        self,
        record: HRSourceRecord,
        is_member: bool,
    ) -> None:
        if bool(record.techexpert_access) == bool(is_member):
            return
        record.techexpert_access = bool(is_member)
        self.db.commit()

    def selected_record(self, record_id: int) -> dict[str, object]:
        record, state = self.active_record(record_id)
        membership_state = "unknown"
        membership_error = ""
        ad_login = ""
        try:
            ad_user = self._resolve_ad(record)
            ad_login = ad_user.username
            is_member = self._is_group_member(ad_user)
            membership_state = "member" if is_member else "not_member"
            self._sync_access_marker(record, is_member)
        except Exception as exc:
            membership_error = str(exc)
        last_sent = self.db.scalar(
            select(TechExpertRegistrationRequest)
            .where(
                TechExpertRegistrationRequest.worker_key == record.worker_key,
                TechExpertRegistrationRequest.source_id == self.source_id,
                TechExpertRegistrationRequest.email_status == "sent",
            )
            .order_by(TechExpertRegistrationRequest.id.desc())
        )
        return {
            "record": record,
            "state": state,
            "placements": self.placements(record),
            "last_sent": last_sent,
            "membership_state": membership_state,
            "membership_error": membership_error,
            "ad_login": ad_login,
        }

    def prepare(
        self,
        *,
        record_id: int,
        placement_index: int,
        actor: str,
        actor_source: str = "",
    ) -> TechExpertRegistrationRequest:
        record, _state = self.active_record(record_id)
        corporate_email = validate_email(
            record.corporate_email,
            field_name="корпоративный e-mail работника",
        )
        placements = self.placements(record)
        if placement_index < 0 or placement_index >= len(placements):
            raise ValueError("Выбранное кадровое назначение не найдено")
        placement = placements[placement_index]
        ad_user = self._resolve_ad(record)
        is_member = self._is_group_member(ad_user)
        self._sync_access_marker(record, is_member)
        if is_member:
            raise ValueError(
                "Работник уже состоит в группе Техэксперта. "
                "Повторная регистрация не требуется."
            )
        profile = get_domain_mail_profile(
            self.db,
            self.settings,
            self.source_id,
        )
        context = {
            "full_name": record.fio,
            "position": str(placement["position"] or "Не указана"),
            "corporate_email": corporate_email,
            "mobile_phone": normalize_text(record.mobile_phone) or "Не указан",
            "department": str(placement["top_department"]),
            "organization": record.source_name or self.source_id,
        }
        operator_identity = operator_mail_identity(
            self.db,
            self.settings,
            actor=actor,
            actor_source=actor_source,
        )
        rendered_body = render_mail_template(
            self.config.registration_body_html,
            context,
            autoescape=True,
        )
        request = TechExpertRegistrationRequest(
            worker_key=record.worker_key,
            source_id=self.source_id,
            source_name=record.source_name or self.source_id,
            hr_record_id=record.id,
            fio=record.fio,
            department=str(placement["top_department"]),
            placement_department=str(placement["department"]),
            position=str(placement["position"]),
            corporate_email=corporate_email,
            mobile_phone=normalize_text(record.mobile_phone),
            ad_login=ad_user.username,
            ad_object_guid=ad_user.object_guid,
            recipient_email=validate_email(
                self.config.recipient_email,
                field_name="получатель уведомлений",
            ),
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
            subject=render_mail_template(
                self.config.registration_subject,
                context,
                autoescape=False,
            ),
            body_html=append_operator_signature(
                rendered_body,
                operator_identity,
            ),
            created_by=str(actor or "").strip(),
        )
        self.db.add(request)
        self.db.flush()
        self.db.add(
            AuditLog(
                actor=actor,
                action="techexpert_registration_prepared",
                target=f"request:{request.id}",
                result="success",
                details=json.dumps(
                    {
                        "worker_key": record.worker_key,
                        "source_id": self.source_id,
                        "fio": record.fio,
                        "department": request.department,
                        "position": request.position,
                        "recipient": request.recipient_email,
                        "operator_display_name": operator_identity[
                            "display_name"
                        ],
                        "operator_email": operator_identity["email"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self.db.commit()
        self.db.refresh(request)
        return request

    def get_request(self, request_id: int) -> TechExpertRegistrationRequest:
        request = self.db.get(TechExpertRegistrationRequest, int(request_id))
        if request is None or normalize_email(request.source_id) != self.source_id:
            raise LookupError("Запрос регистрации не найден")
        return request

    def _snapshot_matches(
        self,
        request: TechExpertRegistrationRequest,
        record: HRSourceRecord,
    ) -> bool:
        placement_exists = any(
            normalize_text(value["department"])
            == normalize_text(request.placement_department)
            and normalize_text(value["position"]) == normalize_text(request.position)
            for value in self.placements(record)
        )
        return bool(
            normalize_fio(record.fio) == normalize_fio(request.fio)
            and normalize_email(record.corporate_email)
            == normalize_email(request.corporate_email)
            and normalize_text(record.mobile_phone)
            == normalize_text(request.mobile_phone)
            and placement_exists
        )

    def _audit_result(
        self,
        request: TechExpertRegistrationRequest,
        actor: str,
    ) -> None:
        self.db.add(
            AuditLog(
                actor=actor,
                action="techexpert_registration_execute",
                target=f"request:{request.id}",
                result=request.status,
                details=json.dumps(
                    {
                        "worker_key": request.worker_key,
                        "group_status": request.group_status,
                        "email_status": request.email_status,
                        "recipient": request.recipient_email,
                        "error": request.last_error,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )

    def execute(
        self,
        *,
        request_id: int,
        actor: str,
    ) -> TechExpertRegistrationRequest:
        request = self.get_request(request_id)
        if request.email_status == "sent":
            return request
        if request.status == "processing":
            raise ValueError("Этот запрос уже выполняется")

        request.status = "processing"
        request.attempts += 1
        request.last_error = ""
        request.updated_at = utcnow()
        self.db.commit()

        try:
            record, _state = self.active_record(request.hr_record_id)
            if record.worker_key != request.worker_key or not self._snapshot_matches(
                request, record
            ):
                request.status = "stale"
                request.last_error = (
                    "Кадровые данные изменились после предпросмотра. "
                    "Подготовьте письмо заново."
                )
                self._audit_result(request, actor)
                self.db.commit()
                return request
            ad_user = self._resolve_ad(record)
            request.ad_login = ad_user.username
            request.ad_object_guid = ad_user.object_guid
        except Exception as exc:
            request.status = "failed"
            request.group_status = "failed"
            request.group_error = str(exc)
            request.last_error = str(exc)
            self._audit_result(request, actor)
            self.db.commit()
            return request

        ad = ActiveDirectoryService(self.settings)
        try:
            group_status = ad.ensure_user_in_group(
                request.ad_login,
                self.config.ad_group_dn,
                object_guid=request.ad_object_guid,
            )
            request.group_status = group_status
            request.group_error = ""
            if group_status == "dry_run":
                request.status = "dry_run"
                request.email_status = "dry_run"
                self._audit_result(request, actor)
                self.db.commit()
                return request
            if not ad.is_user_member_of_group(
                request.ad_login,
                self.config.ad_group_dn,
                object_guid=request.ad_object_guid,
            ):
                raise RuntimeError(
                    "AD не подтвердил членство работника в группе Техэксперта"
                )
            record.techexpert_access = True
            self.db.commit()
        except Exception as exc:
            request.status = "failed"
            request.group_status = "failed"
            request.group_error = str(exc)
            request.last_error = str(exc)
            request.email_status = "not_started"
            self._audit_result(request, actor)
            self.db.commit()
            return request

        try:
            CredentialMailer(self.settings).send_html(
                recipient=request.recipient_email,
                subject=request.subject,
                body_html=request.body_html,
                sender_email=request.sender_email,
                sender_name=request.sender_name,
            )
            request.email_status = "sent"
            request.email_error = ""
            request.status = "sent"
            request.sent_at = utcnow()
            request.last_error = ""
        except Exception as exc:
            request.email_status = "failed"
            request.email_error = str(exc)
            request.status = "partial"
            request.last_error = str(exc)
        request.updated_at = utcnow()
        self._audit_result(request, actor)
        self.db.commit()
        self.db.refresh(request)
        return request

    def history(self, *, limit: int = 20) -> list[TechExpertRegistrationRequest]:
        if not self.source_id:
            return []
        return list(
            self.db.scalars(
                select(TechExpertRegistrationRequest)
                .where(TechExpertRegistrationRequest.source_id == self.source_id)
                .order_by(TechExpertRegistrationRequest.id.desc())
                .limit(max(1, min(int(limit), 100)))
            ).all()
        )
