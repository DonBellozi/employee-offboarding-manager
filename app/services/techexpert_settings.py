from __future__ import annotations

import re
from datetime import time

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models_onec_sources import OneCAdditionalSource
from app.models_techexpert import TechExpertSettings
from app.services.mailer import validate_mail_template


TECHEXPERT_TEMPLATE_VARIABLES = {
    "employees",
    "employee_count",
    "full_name",
    "corporate_email",
    "organization",
    "department",
    "dismissal_date",
}

TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES = {
    "full_name",
    "position",
    "corporate_email",
    "mobile_phone",
    "login",
    "department",
    "organization",
}

TECHEXPERT_RECOVERY_TEMPLATE_VARIABLES = (
    TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES
)

LEGACY_TECHEXPERT_SUBJECT = (
    "Прекращение доступа к системе «Техэксперт»: {{ full_name }}"
)

LEGACY_TECHEXPERT_BODY_HTML = """\
<p>Здравствуйте!</p>
<p>Просим прекратить доступ к системе «Техэксперт» для работника:</p>
<p>
  <strong>ФИО:</strong> {{ full_name }}<br>
  <strong>Корпоративный e-mail:</strong> {{ corporate_email }}<br>
  <strong>Организация:</strong> {{ organization }}
</p>
<p>Это автоматическое уведомление по подтвержденному кадровому событию.</p>
"""

DEFAULT_TECHEXPERT_SUBJECT = (
    "Список на прекращение доступа к системе «Техэксперт» "
    "({{ employee_count }})"
)

PREVIOUS_DEFAULT_TECHEXPERT_BODY_HTML = """\
<p>Здравствуйте!</p>
<p>Просим прекратить доступ к системе «Техэксперт» для следующих работников:</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead>
    <tr>
      <th>ФИО</th>
      <th>Корпоративный e-mail</th>
      <th>Организация</th>
      <th>Дата увольнения</th>
    </tr>
  </thead>
  <tbody>
  {% for employee in employees %}
    <tr>
      <td>{{ employee.full_name }}</td>
      <td>{{ employee.corporate_email }}</td>
      <td>{{ employee.organization }}</td>
      <td>{{ employee.dismissal_date }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<p>Это автоматическое уведомление по подтвержденным кадровым событиям.</p>
"""

DEFAULT_TECHEXPERT_BODY_HTML = """\
<p>Здравствуйте!</p>
<p>Просим прекратить доступ к системе «Техэксперт» для следующих работников:</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead>
    <tr>
      <th>ФИО</th>
      <th>Корпоративный e-mail</th>
      <th>Организация</th>
      <th>Подразделение</th>
      <th>Дата увольнения</th>
    </tr>
  </thead>
  <tbody>
  {% for employee in employees %}
    <tr>
      <td>{{ employee.full_name }}</td>
      <td>{{ employee.corporate_email }}</td>
      <td>{{ employee.organization }}</td>
      <td>{{ employee.department }}</td>
      <td>{{ employee.dismissal_date }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<p>Это автоматическое уведомление по подтвержденным кадровым событиям.</p>
"""

DEFAULT_TECHEXPERT_REGISTRATION_SUBJECT = (
    "Регистрация пользователя в системе «Техэксперт» — {{ full_name }}"
)

PREVIOUS_DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML = """\
<p><strong>{{ department }}</strong></p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead>
    <tr>
      <th>ФИО</th>
      <th>Должность</th>
      <th>E-mail</th>
      <th>Телефон</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>{{ full_name }}</td>
      <td>{{ position }}</td>
      <td>{{ corporate_email }}</td>
      <td>{{ mobile_phone }}</td>
    </tr>
  </tbody>
</table>
"""

DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML = """\
<p><strong>{{ department }}</strong></p>
<p>Просим использовать указанный логин при регистрации пользователя.</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead>
    <tr>
      <th>ФИО</th>
      <th>Должность</th>
      <th>E-mail</th>
      <th>Телефон</th>
      <th>Логин</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>{{ full_name }}</td>
      <td>{{ position }}</td>
      <td>{{ corporate_email }}</td>
      <td>{{ mobile_phone }}</td>
      <td>{{ login }}</td>
    </tr>
  </tbody>
</table>
"""

DEFAULT_TECHEXPERT_RECOVERY_SUBJECT = (
    "Восстановление доступа к системе «Техэксперт» — {{ full_name }}"
)

DEFAULT_TECHEXPERT_RECOVERY_BODY_HTML = """\
<p><strong>{{ department }}</strong></p>
<p>
  Пользователь уже состоит в группе доступа AD «Техэксперт».
  Просим восстановить его существующую учетную запись в системе
  «Техэксперт» и повторно направить пользователю логин и пароль на
  корпоративный e-mail.
</p>
<table border="1" cellpadding="6" cellspacing="0">
  <thead>
    <tr>
      <th>ФИО</th>
      <th>Должность</th>
      <th>E-mail</th>
      <th>Телефон</th>
      <th>Логин AD</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>{{ full_name }}</td>
      <td>{{ position }}</td>
      <td>{{ corporate_email }}</td>
      <td>{{ mobile_phone }}</td>
      <td>{{ login }}</td>
    </tr>
  </tbody>
</table>
"""


def build_techexpert_template_context(
    employees: list[dict[str, str]],
) -> dict[str, object]:
    """Контекст списка и совместимости со старыми одиночными шаблонами."""

    organizations = list(
        dict.fromkeys(item["organization"] for item in employees)
    )
    dismissal_dates = list(
        dict.fromkeys(item["dismissal_date"] for item in employees)
    )
    departments = list(
        dict.fromkeys(item.get("department", "") for item in employees)
    )
    return {
        "employees": employees,
        "employee_count": len(employees),
        "full_name": ", ".join(item["full_name"] for item in employees),
        "corporate_email": ", ".join(
            item["corporate_email"] for item in employees
        ),
        "organization": ", ".join(organizations),
        "department": ", ".join(value for value in departments if value),
        "dismissal_date": ", ".join(dismissal_dates),
    }


def normalize(value: str) -> str:
    return str(value or "").strip().lower()


def parse_notification_time(value: str) -> time:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{2}):(\d{2})", text)
    if not match:
        raise ValueError("Время отправки должно быть в формате ЧЧ:ММ")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("Указано недопустимое время отправки")
    return time(hour, minute)


def normalize_email(value: str, *, field_name: str) -> str:
    try:
        return validate_email(
            str(value or "").strip(),
            check_deliverability=False,
        ).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(f"Некорректный {field_name}: {exc}") from exc


def ensure_techexpert_settings(db: Session) -> TechExpertSettings:
    row = db.get(TechExpertSettings, 1)
    if row is not None:
        changed = False
        if (
            row.subject.strip() == LEGACY_TECHEXPERT_SUBJECT.strip()
            and row.body_html.strip() == LEGACY_TECHEXPERT_BODY_HTML.strip()
        ) or (
            row.subject.strip() == DEFAULT_TECHEXPERT_SUBJECT.strip()
            and row.body_html.strip()
            == PREVIOUS_DEFAULT_TECHEXPERT_BODY_HTML.strip()
        ):
            row.subject = DEFAULT_TECHEXPERT_SUBJECT
            row.body_html = DEFAULT_TECHEXPERT_BODY_HTML
            changed = True
        if not row.registration_subject.strip():
            row.registration_subject = DEFAULT_TECHEXPERT_REGISTRATION_SUBJECT
            changed = True
        if not row.registration_body_html.strip():
            row.registration_body_html = (
                DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML
            )
            changed = True
        elif (
            row.registration_body_html.strip()
            == PREVIOUS_DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML.strip()
        ):
            row.registration_body_html = (
                DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML
            )
            changed = True
        if not row.recovery_subject.strip():
            row.recovery_subject = DEFAULT_TECHEXPERT_RECOVERY_SUBJECT
            changed = True
        if not row.recovery_body_html.strip():
            row.recovery_body_html = DEFAULT_TECHEXPERT_RECOVERY_BODY_HTML
            changed = True
        if changed:
            db.commit()
            db.refresh(row)
        return row
    row = TechExpertSettings(
        id=1,
        enabled=False,
        notification_time="08:45",
        subject=DEFAULT_TECHEXPERT_SUBJECT,
        body_html=DEFAULT_TECHEXPERT_BODY_HTML,
        registration_subject=DEFAULT_TECHEXPERT_REGISTRATION_SUBJECT,
        registration_body_html=DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML,
        recovery_subject=DEFAULT_TECHEXPERT_RECOVERY_SUBJECT,
        recovery_body_html=DEFAULT_TECHEXPERT_RECOVERY_BODY_HTML,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TechExpertSettingsService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db

    def get(self) -> TechExpertSettings:
        return ensure_techexpert_settings(self.db)

    def available_domains(self) -> list[str]:
        mail_domains = {
            normalize(domain)
            for domain in self.settings.zimbra_domains
            if normalize(domain)
        }
        hr_domains = {
            normalize(value)
            for value in self.db.scalars(
                select(OneCAdditionalSource.mail_domain).where(
                    OneCAdditionalSource.enabled.is_(True)
                )
            ).all()
            if normalize(value)
        }
        if hr_domains:
            return sorted(mail_domains.intersection(hr_domains))
        return sorted(mail_domains)

    def validate(
        self,
        *,
        source_domain: str,
        ad_group_dn: str,
        recipient_email: str,
        notification_time: str,
        subject: str,
        body_html: str,
        registration_subject: str | None = None,
        registration_body_html: str | None = None,
        recovery_subject: str | None = None,
        recovery_body_html: str | None = None,
    ) -> dict[str, str]:
        domain = normalize(source_domain)
        if not domain:
            raise ValueError("Выберите организацию Техэксперта")
        if domain not in self.available_domains():
            raise ValueError(
                "Выбранная организация отсутствует среди кадровых/почтовых доменов"
            )
        group_dn = str(ad_group_dn or "").strip()
        if not group_dn:
            raise ValueError("Укажите DN группы доступа AD")
        recipient = normalize_email(
            recipient_email,
            field_name="e-mail получателя",
        )
        parsed_time = parse_notification_time(notification_time)
        normalized_time = f"{parsed_time.hour:02d}:{parsed_time.minute:02d}"

        validate_mail_template(
            subject,
            allowed_variables=TECHEXPERT_TEMPLATE_VARIABLES,
            field_name="Тема письма",
            autoescape=False,
        )
        validate_mail_template(
            body_html,
            allowed_variables=TECHEXPERT_TEMPLATE_VARIABLES,
            field_name="HTML-шаблон письма",
            autoescape=True,
        )
        registration_subject = (
            str(registration_subject or "").strip()
            or DEFAULT_TECHEXPERT_REGISTRATION_SUBJECT
        )
        registration_body_html = (
            str(registration_body_html or "").strip()
            or DEFAULT_TECHEXPERT_REGISTRATION_BODY_HTML
        )
        validate_mail_template(
            registration_subject,
            allowed_variables=TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES,
            field_name="Тема письма о регистрации",
            autoescape=False,
        )
        validate_mail_template(
            registration_body_html,
            allowed_variables=TECHEXPERT_REGISTRATION_TEMPLATE_VARIABLES,
            field_name="HTML-шаблон письма о регистрации",
            autoescape=True,
        )
        recovery_subject = (
            str(recovery_subject or "").strip()
            or DEFAULT_TECHEXPERT_RECOVERY_SUBJECT
        )
        recovery_body_html = (
            str(recovery_body_html or "").strip()
            or DEFAULT_TECHEXPERT_RECOVERY_BODY_HTML
        )
        validate_mail_template(
            recovery_subject,
            allowed_variables=TECHEXPERT_RECOVERY_TEMPLATE_VARIABLES,
            field_name="Тема письма о восстановлении доступа",
            autoescape=False,
        )
        validate_mail_template(
            recovery_body_html,
            allowed_variables=TECHEXPERT_RECOVERY_TEMPLATE_VARIABLES,
            field_name="HTML-шаблон письма о восстановлении доступа",
            autoescape=True,
        )
        return {
            "source_domain": domain,
            "ad_group_dn": group_dn,
            "recipient_email": recipient,
            "notification_time": normalized_time,
            "subject": subject.strip(),
            "body_html": body_html.strip(),
            "registration_subject": registration_subject,
            "registration_body_html": registration_body_html,
            "recovery_subject": recovery_subject,
            "recovery_body_html": recovery_body_html,
        }

    def save(
        self,
        *,
        enabled: bool,
        source_domain: str,
        ad_group_dn: str,
        recipient_email: str,
        notification_time: str,
        subject: str,
        body_html: str,
        actor: str,
        registration_subject: str | None = None,
        registration_body_html: str | None = None,
        recovery_subject: str | None = None,
        recovery_body_html: str | None = None,
    ) -> TechExpertSettings:
        row = self.get()
        values = self.validate(
            source_domain=source_domain,
            ad_group_dn=ad_group_dn,
            recipient_email=recipient_email,
            notification_time=notification_time,
            subject=subject,
            body_html=body_html,
            registration_subject=(
                row.registration_subject
                if registration_subject is None
                else registration_subject
            ),
            registration_body_html=(
                row.registration_body_html
                if registration_body_html is None
                else registration_body_html
            ),
            recovery_subject=(
                row.recovery_subject
                if recovery_subject is None
                else recovery_subject
            ),
            recovery_body_html=(
                row.recovery_body_html
                if recovery_body_html is None
                else recovery_body_html
            ),
        )
        row.enabled = bool(enabled)
        row.source_domain = values["source_domain"]
        row.ad_group_dn = values["ad_group_dn"]
        row.recipient_email = values["recipient_email"]
        row.notification_time = values["notification_time"]
        row.subject = values["subject"]
        row.body_html = values["body_html"]
        row.registration_subject = values["registration_subject"]
        row.registration_body_html = values["registration_body_html"]
        row.recovery_subject = values["recovery_subject"]
        row.recovery_body_html = values["recovery_body_html"]
        row.updated_by = str(actor or "").strip()
        self.db.commit()
        self.db.refresh(row)
        return row
