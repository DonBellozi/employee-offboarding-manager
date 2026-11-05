from __future__ import annotations

from urllib.parse import quote_plus

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditLog, DomainMailProfile
from app.security import get_or_create_csrf, require_admin, validate_csrf
from app.services.dismissal_mailer import (
    DISMISSAL_TEMPLATE_VARIABLES,
    ensure_dismissal_mail_templates,
)
from app.services.mailer import (
    COMBINED_TEMPLATE_VARIABLES,
    CORPORATE_TEMPLATE_VARIABLES,
    PERSONAL_TEMPLATE_VARIABLES,
    CredentialMailer,
    ensure_domain_mail_profiles,
    render_mail_template,
    validate_mail_template,
)
from app.time_utils import register_datetime_filters


router = APIRouter()
templates = register_datetime_filters(Jinja2Templates(directory="app/templates"))

TEMPLATE_DEFINITIONS = {
    "combined": {
        "label": "Все реквизиты — AD и почта",
        "short_label": "Все реквизиты",
        "description": "После создания AD и почты: отдельно на личный и корпоративный адрес.",
        "variables": COMBINED_TEMPLATE_VARIABLES,
    },
    "personal": {
        "label": "Реквизиты корпоративной почты",
        "short_label": "Реквизиты почты",
        "description": "Для отдельного создания почты или частичной готовности УЗ.",
        "variables": PERSONAL_TEMPLATE_VARIABLES,
    },
    "corporate": {
        "label": "Реквизиты доменной учетной записи",
        "short_label": "Реквизиты AD",
        "description": "Для отдельного создания AD. Только новые реквизиты AD.",
        "variables": CORPORATE_TEMPLATE_VARIABLES,
    },
    "dismissal": {
        "label": "Возврат оборудования при увольнении",
        "short_label": "Увольнение / оборудование",
        "description": "Уведомление при первом появлении даты увольнения в организации.",
        "variables": DISMISSAL_TEMPLATE_VARIABLES,
    },
}



def _test_context(template_key: str, domain: str) -> dict[str, str]:
    corporate_email = f"ivanov.ii@{domain}"
    common = {
        "full_name": "Иванов Иван Иванович",
        "corporate_email": corporate_email,
        "mail_domain": domain,
    }
    if template_key == "combined":
        return {**common, "mail_password": "TEST-Mail-Password-123!",
                "ad_login": "ivanov.ii", "ad_password": "TEST-AD-Password-123!"}
    if template_key == "personal":
        return {
            **common,
            "mail_password": "TEST-Mail-Password-123!",
        }
    if template_key == "corporate":
        return {
            **common,
            "ad_login": "ivanov.ii",
            "ad_password": "TEST-AD-Password-123!",
        }
    if template_key == "dismissal":
        return {
            **common,
            "dismissal_date": "20.08.2026",
            "return_deadline": "18.08.2026",
            "return_deadline_text": "не позднее 18.08.2026",
            "organization": "Тестовая организация",
            "organizations": "Тестовая организация",
            "personal_email": "ivanov@example.net",
        }
    raise ValueError("Неизвестный тип шаблона")


def _validate_sender_for_profile(
    *,
    profile: DomainMailProfile,
    sender_email: str,
) -> str:
    try:
        normalized_sender = validate_email(
            sender_email.strip(),
            check_deliverability=False,
        ).normalized.lower()
    except EmailNotValidError as exc:
        raise ValueError(
            f"Некорректный e-mail отправителя: {exc}"
        ) from exc

    sender_domain = normalized_sender.rsplit("@", 1)[1]
    if sender_domain != profile.domain.strip().lower():
        raise ValueError(
            "E-mail отправителя должен принадлежать домену "
            f"{profile.domain}"
        )
    return normalized_sender

def _redirect(
    *,
    profile_id: int,
    template_key: str,
    message: str = "",
    error: str = "",
) -> RedirectResponse:
    params = [f"selected={profile_id}:{template_key}"]
    if message:
        params.append(f"message={quote_plus(message)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    return RedirectResponse(
        f"/admin/mail-templates?{'&'.join(params)}",
        status_code=303,
    )


def _mail_template_preview_document(body_html: str) -> str:
    """Сохранить полный HTML письма или обернуть фрагмент без Jinja-рендера."""
    body = str(body_html or "").strip()
    if not body:
        body = '<p style="color:#64748b">Шаблон пуст.</p>'
    lowered = body.casefold()
    if "<!doctype" in lowered or "<html" in lowered:
        return body
    return (
        "<!doctype html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "</head>\n"
        '<body style="margin:24px; color:#172033; '
        'font-family:Arial,sans-serif; line-height:1.5">'
        f"{body}</body>\n"
        "</html>"
    )


def _page_context(
    request: Request,
    *,
    settings: Settings,
    db: Session,
):
    current = require_admin(request)
    profiles = ensure_domain_mail_profiles(db, settings)
    dismissal_templates = ensure_dismissal_mail_templates(db, settings)
    selected = request.query_params.get("selected", "").strip()
    valid_keys = {
        f"{profile.id}:{template_key}"
        for profile in profiles
        for template_key in TEMPLATE_DEFINITIONS
    }
    if selected not in valid_keys:
        selected = (
            f"{profiles[0].id}:combined"
            if profiles
            else ""
        )

    return {
        "user": current,
        "csrf": get_or_create_csrf(request),
        "profiles": profiles,
        "dismissal_templates": dismissal_templates,
        "template_definitions": TEMPLATE_DEFINITIONS,
        "selected_key": selected,
        "message": request.query_params.get("message", ""),
        "error": request.query_params.get("error", ""),
    }


@router.get("/admin/mail-templates")
def mail_templates(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return templates.TemplateResponse(
        request,
        "admin_mail_templates.html",
        _page_context(
            request,
            settings=settings,
            db=db,
        ),
    )


@router.post(
    "/admin/mail-templates/preview/body",
    response_class=HTMLResponse,
)
def preview_mail_template_body(
    request: Request,
    body_html: str = Form(""),
    csrf: str = Form(...),
):
    """Показать текущий HTML-черновик без подстановки шаблонных переменных."""
    validate_csrf(request, csrf)
    require_admin(request)
    if len(body_html) > 1_000_000:
        return HTMLResponse(
            '<p style="font-family:Arial,sans-serif">Шаблон слишком большой.</p>',
            status_code=413,
        )
    return HTMLResponse(_mail_template_preview_document(body_html))


@router.post("/admin/mail-templates/{profile_id}")
def update_mail_template(
    profile_id: int,
    request: Request,
    template_key: str = Form(...),
    sender_name: str = Form(""),
    sender_email: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    key = template_key.strip().lower()

    try:
        definition = TEMPLATE_DEFINITIONS.get(key)
        if definition is None:
            raise ValueError("Неизвестный тип шаблона")

        profile = db.get(DomainMailProfile, profile_id)
        if profile is None:
            raise ValueError("Профиль почтового домена не найден")

        configured_domains = {
            domain.strip().lower()
            for domain in settings.zimbra_domains
            if domain.strip()
        }
        if configured_domains and profile.domain.lower() not in configured_domains:
            raise ValueError("Этот домен отсутствует в настройках Zimbra")

        normalized_sender = _validate_sender_for_profile(
            profile=profile,
            sender_email=sender_email,
        )

        validate_mail_template(
            subject,
            allowed_variables=set(definition["variables"]),
            field_name="Тема письма",
            autoescape=False,
        )
        validate_mail_template(
            body_html,
            allowed_variables=set(definition["variables"]),
            field_name="HTML-шаблон письма",
            autoescape=True,
        )

        profile.sender_name = sender_name.strip()
        profile.sender_email = normalized_sender
        profile.updated_by = current.username

        if key == "combined":
            profile.combined_subject = subject.strip()
            profile.combined_body_html = body_html.strip()
        elif key == "personal":
            profile.personal_subject = subject.strip()
            profile.personal_body_html = body_html.strip()
        elif key == "corporate":
            profile.corporate_subject = subject.strip()
            profile.corporate_body_html = body_html.strip()
        else:
            dismissal_templates = ensure_dismissal_mail_templates(
                db,
                settings,
            )
            dismissal_template = dismissal_templates.get(
                profile.domain.strip().lower()
            )
            if dismissal_template is None:
                raise ValueError("Шаблон увольнения для домена не найден")
            dismissal_template.subject = subject.strip()
            dismissal_template.body_html = body_html.strip()
            dismissal_template.updated_by = current.username

        db.add(
            AuditLog(
                actor=current.username,
                action="mail_template_update",
                target=f"{profile.domain}:{key}",
                result="success",
                details=f"sender={normalized_sender}",
            )
        )
        db.commit()
        return _redirect(
            profile_id=profile.id,
            template_key=key,
            message=f"Шаблон «{definition['label']}» сохранен",
        )
    except Exception as exc:
        db.rollback()
        return _redirect(
            profile_id=profile_id,
            template_key=key or "personal",
            error=str(exc),
        )


@router.post("/admin/mail-templates/{profile_id}/test")
def test_mail_template(
    profile_id: int,
    request: Request,
    template_key: str = Form(...),
    sender_name: str = Form(""),
    sender_email: str = Form(...),
    subject: str = Form(...),
    body_html: str = Form(...),
    test_recipient: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    validate_csrf(request, csrf)
    current = require_admin(request)
    key = template_key.strip().lower()

    try:
        definition = TEMPLATE_DEFINITIONS.get(key)
        if definition is None:
            raise ValueError("Неизвестный тип шаблона")

        profile = db.get(DomainMailProfile, profile_id)
        if profile is None:
            raise ValueError("Профиль почтового домена не найден")

        configured_domains = {
            domain.strip().lower()
            for domain in settings.zimbra_domains
            if domain.strip()
        }
        if configured_domains and profile.domain.lower() not in configured_domains:
            raise ValueError("Этот домен отсутствует в настройках Zimbra")

        normalized_sender = _validate_sender_for_profile(
            profile=profile,
            sender_email=sender_email,
        )
        try:
            normalized_recipient = validate_email(
                test_recipient.strip(),
                check_deliverability=False,
            ).normalized.lower()
        except EmailNotValidError as exc:
            raise ValueError(
                f"Некорректный тестовый адрес: {exc}"
            ) from exc

        variables = set(definition["variables"])
        validate_mail_template(
            subject,
            allowed_variables=variables,
            field_name="Тема письма",
            autoescape=False,
        )
        validate_mail_template(
            body_html,
            allowed_variables=variables,
            field_name="HTML-шаблон письма",
            autoescape=True,
        )

        context = _test_context(key, profile.domain.strip().lower())
        rendered_subject = render_mail_template(
            subject,
            context,
            autoescape=False,
        )
        rendered_body = render_mail_template(
            body_html,
            context,
            autoescape=True,
        )

        if settings.dry_run:
            return {
                "ok": True,
                "sent": False,
                "message": (
                    "DRY_RUN включен – тестовое письмо сформировано, "
                    "но не отправлено."
                ),
            }

        CredentialMailer(settings)._send(
            normalized_recipient,
            f"[ТЕСТ] {rendered_subject}",
            rendered_body,
            sender_email=normalized_sender,
            sender_name=sender_name.strip(),
        )

        db.add(
            AuditLog(
                actor=current.username,
                action="mail_template_test_send",
                target=f"{profile.domain}:{key}",
                result="success",
                details=(
                    f"recipient={normalized_recipient}; "
                    f"sender={normalized_sender}"
                ),
            )
        )
        db.commit()
        return {
            "ok": True,
            "sent": True,
            "message": f"Тестовое письмо отправлено на {normalized_recipient}",
        }
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
