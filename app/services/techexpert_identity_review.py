"""Explicit AD identity decisions for a single organization's dismissal."""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.models_techexpert import TechExpertNotification
from app.services.ad import ActiveDirectoryService, ADIdentityCandidatesError
from app.services.dismissal_ad_mapping import norm, fio
from app.services.onec_import_recovery import running_import

REJECTION_ACTION = 'techexpert_ad_candidates_excluded'


def candidate_fingerprint(db, row, identity, candidates):
    if identity.ad_login or identity.ad_object_guid or not candidates or any(not c.object_guid for c in candidates):
        return ''
    records = db.scalars(select(HRSourceRecord).where(HRSourceRecord.worker_key == row.worker_key)).all()
    mappings = db.scalars(select(EmailLoginMapping).where(EmailLoginMapping.worker_key == row.worker_key)).all()
    states = db.scalars(select(HREmploymentState).where(HREmploymentState.worker_key == row.worker_key)).all()
    data = [row.worker_key, row.employment_event_id, str(row.dismissal_date), row.source_id, fio(row.fio),
        sorted((r.source_id, fio(r.fio), norm(r.login)) for r in records),
        sorted((m.source_domain, norm(m.ad_login), norm(m.ad_object_guid)) for m in mappings),
        sorted((s.source_id, s.status, str(s.dismissal_date)) for s in states),
        sorted((norm(c.object_guid), norm(c.username), fio(c.display_name)) for c in candidates)]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def candidates_excluded(db, row, identity, candidates):
    fingerprint = candidate_fingerprint(db, row, identity, candidates)
    return bool(fingerprint and db.scalar(select(AuditLog.id).where(
        AuditLog.action == REJECTION_ACTION, AuditLog.target == f'techexpert:{row.id}',
        AuditLog.result == 'confirmed', AuditLog.details == fingerprint)))


class TechExpertIdentityReviewService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    def context(self, notification_id):
        from app.services.techexpert_lifecycle import TechExpertLifecycleService
        self.db.expire_all()
        row = self.db.get(TechExpertNotification, notification_id)
        if row is None or row.status not in {'failed', 'intervention', 'pending', 'waiting_ad'}:
            raise ValueError('Уведомление уже завершено или не найдено. Обновите журнал.')
        if running_import(self.db):
            raise ValueError('Выполняется кадровый импорт. Повторите после завершения.')
        lifecycle = TechExpertLifecycleService(self.settings, self.db)
        event, state = lifecycle._current_event_and_state(row)
        if not lifecycle._still_due(row, event, state):
            raise ValueError('Кадровое состояние изменилось. Действие остановлено.')
        if not self.settings.ad_check_enabled:
            raise ValueError('Проверка AD отключена.')
        return row, lifecycle

    def inspect(self, notification_id, login=''):
        row, lifecycle = self.context(notification_id)
        candidates, fingerprint, error = [], '', ''
        try:
            identity = lifecycle._resolve_identity(row)
            try:
                user = ActiveDirectoryService(self.settings).lookup_group_user(
                    identity.ad_login, object_guid=identity.ad_object_guid, full_name=row.fio)
                if user is not None:
                    candidates = [user]
            except ADIdentityCandidatesError as exc:
                candidates = exc.candidates
                fingerprint = candidate_fingerprint(self.db, row, identity, candidates)
        except Exception as exc:
            error = str(exc)
        selected = self.check_login(notification_id, login) if login else None
        return dict(notification=row, candidates=candidates, fingerprint=fingerprint,
                    check_error=error, selected=selected, login=login)

    def check_login(self, notification_id, login):
        row, _ = self.context(notification_id)
        login = norm(login)
        if not login or len(login) > 128 or any(c.isspace() for c in login) or '@' in login:
            raise ValueError('Укажите точный логин AD.')
        user = ActiveDirectoryService(self.settings).get_user(login)
        if user is None or not user.object_guid:
            raise ValueError('AD не подтвердила учётку с objectGUID.')
        records = self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.worker_key == row.worker_key)).all()
        if fio(user.display_name) not in {fio(row.fio), *(fio(r.fio) for r in records)}:
            raise ValueError('ФИО AD не совпадает с кадровой историей. Проверьте владельца учётки.')
        if any(m.worker_key != row.worker_key and (
            norm(m.ad_object_guid) == norm(user.object_guid) or norm(m.ad_login) == norm(user.username))
            for m in self.db.scalars(select(EmailLoginMapping))):
            raise ValueError('Учётка уже сопоставлена другому работнику.')
        return user

    def save(self, notification_id, actor, *, login='', expected_guid='', fingerprint='', confirmed=False):
        if not confirmed:
            raise ValueError('Подтвердите выбор.')
        row, lifecycle = self.context(notification_id)
        if fingerprint:
            context = self.inspect(notification_id)
            if not context['fingerprint'] or context['fingerprint'] != fingerprint:
                raise ValueError('Результат поиска изменился. Проверьте заново.')
            row, lifecycle = self.context(notification_id)  # HR can change during LDAP.
            if candidate_fingerprint(self.db, row, lifecycle._resolve_identity(row), context['candidates']) != fingerprint:
                raise ValueError('Кадровые данные или сопоставление изменились. Проверьте заново.')
            self.db.add(AuditLog(actor=actor, action=REJECTION_ACTION, target=f'techexpert:{row.id}',
                result='confirmed', details=fingerprint))
        else:
            user = self.check_login(notification_id, login)
            if not expected_guid or norm(user.object_guid) != norm(expected_guid):
                raise ValueError('Учётка изменилась после проверки. Проверьте заново.')
            row, lifecycle = self.context(notification_id)
            if any(m.worker_key != row.worker_key and (
                norm(m.ad_object_guid) == norm(user.object_guid) or norm(m.ad_login) == norm(user.username))
                for m in self.db.scalars(select(EmailLoginMapping))):
                raise ValueError('Сопоставление изменилось. Учётка связана с другим работником.')
            now = datetime.now(timezone.utc)
            records = self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.worker_key == row.worker_key)).all()
            sources = {r.source_id: r.corporate_email for r in records}
            sources.setdefault(row.source_id, row.corporate_email)
            for source, address in sources.items():
                mapping = self.db.scalar(select(EmailLoginMapping).where(
                    EmailLoginMapping.worker_key == row.worker_key, EmailLoginMapping.source_domain == source))
                if mapping is None:
                    mapping = EmailLoginMapping(worker_key=row.worker_key, source_domain=source, source_email=address,
                        zimbra_id='', zimbra_email='', created_by=actor)
                    self.db.add(mapping)
                mapping.ad_login, mapping.ad_object_guid = user.username, user.object_guid
                mapping.last_verified_at = now
            row.ad_login, row.ad_object_guid = user.username, user.object_guid
            self.db.add(AuditLog(actor=actor, action='techexpert_ad_identity_confirmed',
                target=f'techexpert:{row.id}', result='confirmed', details=json.dumps(
                    {'login': user.username, 'object_guid': user.object_guid}, ensure_ascii=False)))
            self.db.flush()
            # Reopen the existing global task only if employment ended everywhere.
            # External writes remain in the lifecycle worker behind HR/deferral gates.
            from app.services.final_dismissal_lifecycle import FinalDismissalLifecycleService
            from app.services.upcoming_dismissals import UpcomingDismissalService
            candidate = next((c for c in UpcomingDismissalService(self.settings, self.db)._all_candidates(
                include_expired=True, include_completed=True) if c['worker_key'] == row.worker_key
                and c['dismissal_date'] == row.dismissal_date and c['blocking_required']), None)
            if candidate:
                final = FinalDismissalLifecycleService(self.settings, self.db)
                run = final._ensure_run(candidate)
                final._refresh_run(run)
        row.status, row.last_error, row.next_attempt_at = 'pending', '', None
        row.group_removal_error = ''
        self.db.commit()
