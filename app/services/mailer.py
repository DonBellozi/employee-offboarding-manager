from __future__ import annotations

import smtplib
import ssl
import time
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formataddr
from html.parser import HTMLParser

from jinja2 import StrictUndefined, meta
from jinja2.exceptions import TemplateError, TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DomainMailProfile


PERSONAL_TEMPLATE_VARIABLES = {
    "full_name",
    "corporate_email",
    "mail_password",
    "mail_domain",
}

CORPORATE_TEMPLATE_VARIABLES = {
    "full_name",
    "corporate_email",
    "ad_login",
    "ad_password",
    "mail_domain",
}

COMBINED_TEMPLATE_VARIABLES = PERSONAL_TEMPLATE_VARIABLES | CORPORATE_TEMPLATE_VARIABLES
DEFAULT_COMBINED_SUBJECT = "Реквизиты учетных записей"
DEFAULT_COMBINED_BODY_HTML = """\
<p>Здравствуйте, {{ full_name }}!</p>
<p>Для Вас созданы учетные записи.</p>
<h3>Вход на компьютер — Active Directory</h3>
<p><strong>Логин:</strong> {{ ad_login }}<br>
<strong>Пароль:</strong> {{ ad_password }}</p>
<h3>Корпоративная почта — Zimbra</h3>
<p><strong>Адрес:</strong> {{ corporate_email }}<br>
<strong>Пароль:</strong> {{ mail_password }}</p>
<p>Не передавайте пароли другим людям. При первом входе система может
потребовать сменить временный пароль.</p>
"""


DEFAULT_PERSONAL_SUBJECT = "Реквизиты корпоративной электронной почты"
DEFAULT_PERSONAL_BODY_HTML = """\
<p>Здравствуйте, {{ full_name }}!</p>
<p>Для Вас создана корпоративная электронная почта.</p>
<p>
  <strong>Логин:</strong> {{ corporate_email }}<br>
  <strong>Пароль:</strong> {{ mail_password }}
</p>
<p>После входа в корпоративную почту Вы получите отдельное письмо с реквизитами учетной записи для входа в компьютер.</p>
"""

DEFAULT_CORPORATE_SUBJECT = "Реквизиты доменной учетной записи"
DEFAULT_CORPORATE_BODY_HTML = """\
<p>Здравствуйте, {{ full_name }}!</p>
<p>Для Вас создана доменная учетная запись.</p>
<p>
  <strong>Логин:</strong> {{ ad_login }}<br>
  <strong>Пароль:</strong> {{ ad_password }}
</p>
<p>При первом входе система может потребовать сменить временный пароль.</p>
"""


class _HTMLToTextParser(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "li",
        "tr",
        "br",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        compact: list[str] = []
        previous_empty = False
        for line in lines:
            empty = not line
            if empty and previous_empty:
                continue
            compact.append(line)
            previous_empty = empty
        return "\n".join(compact).strip()


def html_to_text(html_body: str) -> str:
    parser = _HTMLToTextParser()
    parser.feed(html_body)
    parser.close()
    return parser.text()


def _template_environment(*, autoescape: bool) -> SandboxedEnvironment:
    return SandboxedEnvironment(
        autoescape=autoescape,
        undefined=StrictUndefined,
    )


def validate_mail_template(
    value: str,
    *,
    allowed_variables: set[str],
    field_name: str,
    autoescape: bool,
) -> None:
    if not value.strip():
        raise ValueError(f"Поле «{field_name}» не может быть пустым")

    environment = _template_environment(autoescape=autoescape)
    try:
        parsed = environment.parse(value)
    except TemplateSyntaxError as exc:
        raise ValueError(
            f"Ошибка в поле «{field_name}», строка {exc.lineno}: {exc.message}"
        ) from exc

    unknown = sorted(meta.find_undeclared_variables(parsed) - allowed_variables)
    if unknown:
        raise ValueError(
            f"В поле «{field_name}» используются неизвестные переменные: "
            + ", ".join(f"{{{{ {name} }}}}" for name in unknown)
        )


def render_mail_template(
    value: str,
    context: Mapping[str, object],
    *,
    autoescape: bool,
) -> str:
    try:
        return _template_environment(autoescape=autoescape).from_string(value).render(
            **context
        )
    except TemplateError as exc:
        raise RuntimeError(f"Не удалось сформировать письмо: {exc}") from exc


def _default_sender_email(settings: Settings, domain: str) -> str:
    configured = settings.smtp_from.strip().lower()
    if configured and configured.endswith(f"@{domain.lower()}"):
        return configured
    return f"it@{domain}"


def ensure_domain_mail_profiles(
    db: Session,
    settings: Settings,
) -> list[DomainMailProfile]:
    domains = list(
        dict.fromkeys(
            domain.strip().lower()
            for domain in settings.zimbra_domains
            if domain.strip()
        )
    )
    if not domains and settings.zimbra_primary_domain.strip():
        domains = [settings.zimbra_primary_domain.strip().lower()]

    existing = {
        item.domain.lower(): item
        for item in db.scalars(
            select(DomainMailProfile).where(DomainMailProfile.domain.in_(domains))
        ).all()
    }

    changed = False
    for domain in domains:
        if domain in existing:
            profile = existing[domain]
            if not str(profile.combined_subject or "").strip():
                profile.combined_subject = DEFAULT_COMBINED_SUBJECT
                changed = True
            if not str(profile.combined_body_html or "").strip():
                profile.combined_body_html = DEFAULT_COMBINED_BODY_HTML
                changed = True
            continue
        profile = DomainMailProfile(
            domain=domain,
            sender_name="ИТ-служба",
            sender_email=_default_sender_email(settings, domain),
            personal_subject=DEFAULT_PERSONAL_SUBJECT,
            personal_body_html=DEFAULT_PERSONAL_BODY_HTML,
            corporate_subject=DEFAULT_CORPORATE_SUBJECT,
            corporate_body_html=DEFAULT_CORPORATE_BODY_HTML,
            combined_subject=DEFAULT_COMBINED_SUBJECT,
            combined_body_html=DEFAULT_COMBINED_BODY_HTML,
        )
        db.add(profile)
        existing[domain] = profile
        changed = True

    if changed:
        db.commit()

    return [
        existing[domain]
        for domain in domains
        if domain in existing
    ]


def get_domain_mail_profile(
    db: Session,
    settings: Settings,
    domain: str,
) -> DomainMailProfile:
    normalized = domain.strip().lower()
    profiles = ensure_domain_mail_profiles(db, settings)
    for profile in profiles:
        if profile.domain.lower() == normalized:
            return profile
    raise RuntimeError(
        f"Для почтового домена {normalized} не настроен профиль отправки писем"
    )


class CredentialMailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _send_once(
        self,
        message: EmailMessage,
        *,
        envelope_sender: str,
        recipient: str,
    ) -> None:
        if self.settings.smtp_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
            )
        try:
            client.ehlo()
            if self.settings.smtp_starttls and not self.settings.smtp_ssl:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.settings.smtp_username:
                client.login(self.settings.smtp_username, self.settings.smtp_password)

            # SMTP MAIL FROM должен принадлежать пользователю, под которым
            # выполнена SASL-аутентификация. Заголовок From при этом остается
            # адресом отправителя из профиля почтового домена.
            client.send_message(
                message,
                from_addr=envelope_sender,
                to_addrs=[recipient],
            )
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                # DATA was already accepted. A failed QUIT is not a failed send.
                try:
                    client.close()
                except (OSError, smtplib.SMTPException):
                    pass

    def test_connection(self) -> str:
        """Проверить SMTP, TLS и аутентификацию без отправки письма."""
        if not self.settings.smtp_host:
            raise RuntimeError("Не заполнены настройки SMTP")

        if self.settings.smtp_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
            )

        try:
            client.ehlo()
            if self.settings.smtp_starttls and not self.settings.smtp_ssl:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if self.settings.smtp_username:
                client.login(
                    self.settings.smtp_username,
                    self.settings.smtp_password,
                )
            code, _ = client.noop()
            if int(code) >= 400:
                raise RuntimeError(f"SMTP NOOP вернул код {code}")
        finally:
            try:
                client.quit()
            except smtplib.SMTPException:
                client.close()

        return "Подключение к SMTP и аутентификация выполнены успешно"

    def _send(
        self,
        recipient: str,
        subject: str,
        body_html: str,
        *,
        sender_email: str,
        sender_name: str,
    ) -> None:
        if self.settings.dry_run:
            return
        if not self.settings.smtp_host:
            raise RuntimeError("Не заполнены настройки SMTP")
        if not sender_email:
            raise RuntimeError("Не настроен email отправителя для выбранного домена")

        message = EmailMessage()
        message["From"] = (
            formataddr((sender_name, sender_email))
            if sender_name.strip()
            else sender_email
        )
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(html_to_text(body_html))
        message.add_alternative(body_html, subtype="html")

        # Для Zimbra с reject_authenticated_sender_login_mismatch адрес
        # конверта должен совпадать с SMTP-пользователем. Получатель продолжит
        # видеть обычный From из профиля, например ИТ-служба <it@domain.com>.
        envelope_sender = (
            self.settings.smtp_username.strip().lower()
            if self.settings.smtp_username.strip()
            else sender_email
        )

        last_error: Exception | None = None
        attempts = max(1, self.settings.smtp_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                self._send_once(
                    message,
                    envelope_sender=envelope_sender,
                    recipient=recipient,
                )
                return
            except (OSError, smtplib.SMTPException) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(max(0.0, self.settings.smtp_retry_delay_seconds))
        raise RuntimeError(
            f"SMTP не отправил письмо после {attempts} попыток: {last_error}"
        )

    def send_html(
        self,
        *,
        recipient: str,
        subject: str,
        body_html: str,
        sender_email: str,
        sender_name: str,
    ) -> None:
        """Отправить готовое служебное HTML-письмо через общий SMTP."""
        self._send(
            recipient,
            subject,
            body_html,
            sender_email=sender_email,
            sender_name=sender_name,
        )

    def send_combined_credentials(
        self, *, profile: DomainMailProfile, recipient: str, full_name: str,
        corporate_email: str, mail_password: str, ad_login: str, ad_password: str,
    ) -> None:
        context = {"full_name": full_name, "corporate_email": corporate_email,
                   "mail_password": mail_password, "ad_login": ad_login,
                   "ad_password": ad_password, "mail_domain": profile.domain}
        self._send(
            recipient,
            render_mail_template(profile.combined_subject, context, autoescape=False),
            render_mail_template(profile.combined_body_html, context, autoescape=True),
            sender_email=profile.sender_email, sender_name=profile.sender_name,
        )

    def send_mail_credentials(
        self,
        profile: DomainMailProfile,
        personal_email: str,
        full_name: str,
        corporate_email: str,
        mail_password: str,
    ) -> None:
        context = {
            "full_name": full_name,
            "corporate_email": corporate_email,
            "mail_password": mail_password,
            "mail_domain": profile.domain,
        }
        subject = render_mail_template(
            profile.personal_subject,
            context,
            autoescape=False,
        )
        body = render_mail_template(
            profile.personal_body_html,
            context,
            autoescape=True,
        )
        self._send(
            personal_email,
            subject,
            body,
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
        )

    def send_ad_credentials(
        self,
        profile: DomainMailProfile,
        corporate_email: str,
        full_name: str,
        ad_login: str,
        ad_password: str,
    ) -> None:
        context = {
            "full_name": full_name,
            "corporate_email": corporate_email,
            "ad_login": ad_login,
            "ad_password": ad_password,
            "mail_domain": profile.domain,
        }
        subject = render_mail_template(
            profile.corporate_subject,
            context,
            autoescape=False,
        )
        body = render_mail_template(
            profile.corporate_body_html,
            context,
            autoescape=True,
        )
        self._send(
            corporate_email,
            subject,
            body,
            sender_email=profile.sender_email,
            sender_name=profile.sender_name,
        )
