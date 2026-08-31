from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine, ensure_compatibility_schema
from app.routers import (
    admin,
    auth,
    employees,
    dashboard_dismissals,
    hr_reconcile_multisource,
    hr_registry_alias,
    hr_registry_mapping,
    hr_registry_multisource,
    mail_templates,
    onec_sources,
    preliminary_dismissals,
    settings_ui,
    synology,
    techexpert,
    telegram_settings,
    zimbra_lifecycle,
    zimbra_mail_cleanup,
    zimbra_observer,
    zimbra_protection,
)
from app.security import CSRFMismatchError, ensure_bootstrap_admin
from app.services.blocking_worker import BlockingQueueWorker
from app.services.dismissal_notifications import DismissalNotificationWorker
from app.services.dismissal_details_cache import DismissalDetailsSnapshotWorker
from app.services.final_dismissal_lifecycle import FinalDismissalLifecycleWorker
from app.services.onec_scheduler import OneCAutoImportScheduler
from app.services.onec_sources import OneCSourceRegistryService
from app.services.synology_scheduler import SynologyLifecycleScheduler
from app.services.telegram_worker import TelegramNotificationWorker
from app.services.techexpert_lifecycle import TechExpertLifecycleWorker
from app.services.zimbra_observer_scheduler import ZimbraObserverScheduler
from app.services.zimbra_employment_lifecycle import (
    ZimbraEmploymentLifecycleWorker,
)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # В опытной эксплуатации приложение всегда работает с реальными действиями,
    # поэтому рабочий запуск с шаблонным APP_SECRET_KEY запрещен безусловно.
    if settings.app_secret_key.startswith("change-me"):
        raise RuntimeError("Замените APP_SECRET_KEY перед рабочим запуском")
    Base.metadata.create_all(bind=engine)
    ensure_compatibility_schema()
    with SessionLocal() as db:
        source_registry = OneCSourceRegistryService(settings, db)
        source_registry.ensure_primary()
        source_registry.apply_primary_to_settings()
        ensure_bootstrap_admin(db, settings)

    onec_scheduler = OneCAutoImportScheduler(settings, SessionLocal)
    blocking_worker = BlockingQueueWorker(settings, SessionLocal)
    dismissal_notification_worker = DismissalNotificationWorker(
        settings,
        SessionLocal,
    )
    dismissal_details_worker = DismissalDetailsSnapshotWorker(
        settings,
        SessionLocal,
    )
    final_dismissal_worker = FinalDismissalLifecycleWorker(
        settings,
        SessionLocal,
    )
    synology_scheduler = SynologyLifecycleScheduler(settings, SessionLocal)
    telegram_worker = TelegramNotificationWorker(
        settings.app_secret_key,
        SessionLocal,
    )
    zimbra_observer_scheduler = ZimbraObserverScheduler(settings, SessionLocal)
    zimbra_employment_worker = ZimbraEmploymentLifecycleWorker(
        settings,
        SessionLocal,
    )
    techexpert_worker = TechExpertLifecycleWorker(settings, SessionLocal)
    onec_scheduler.start()
    blocking_worker.start()
    dismissal_notification_worker.start()
    dismissal_details_worker.start()
    final_dismissal_worker.start()
    synology_scheduler.start()
    telegram_worker.start()
    zimbra_observer_scheduler.start()
    zimbra_employment_worker.start()
    techexpert_worker.start()
    try:
        yield
    finally:
        techexpert_worker.stop()
        zimbra_employment_worker.stop()
        zimbra_observer_scheduler.stop()
        telegram_worker.stop()
        synology_scheduler.stop()
        final_dismissal_worker.stop()
        dismissal_details_worker.stop()
        dismissal_notification_worker.stop()
        blocking_worker.stop()
        onec_scheduler.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie=settings.session_cookie_name,
    same_site=settings.session_cookie_samesite,
    https_only=settings.session_cookie_secure,
    max_age=8 * 60 * 60,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)
app.include_router(hr_registry_multisource.router)
app.include_router(hr_registry_alias.router)
app.include_router(hr_registry_mapping.router)
app.include_router(dashboard_dismissals.router)
app.include_router(employees.router)
app.include_router(hr_reconcile_multisource.router)
app.include_router(settings_ui.router)
app.include_router(onec_sources.router)
app.include_router(preliminary_dismissals.router)
app.include_router(synology.router)
app.include_router(techexpert.router)
app.include_router(telegram_settings.router)
app.include_router(zimbra_observer.router)
app.include_router(zimbra_protection.router)
app.include_router(zimbra_lifecycle.router)
app.include_router(zimbra_mail_cleanup.router)
app.include_router(mail_templates.router)
app.include_router(admin.router)


@app.exception_handler(CSRFMismatchError)
async def csrf_mismatch_handler(request: Request, _: CSRFMismatchError):
    request.session.clear()
    return RedirectResponse("/login?csrf_error=1", status_code=303)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    if (
        request.url.path == "/admin/mail-templates/preview/body"
        or (
            request.url.path.startswith("/techexpert/registration/")
            and request.url.path.endswith("/body")
        )
    ):
        # Предпросмотр выполняется в iframe без sandbox-разрешений. Почтовым
        # шаблонам нужны inline-стили, но скрипты и любые действия запрещены.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src 'self' data: https: http:; font-src 'self' data:; "
            "script-src 'none'; connect-src 'none'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; "
            "form-action 'self'"
        )
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
def health():
    return {"status": "ok"}
