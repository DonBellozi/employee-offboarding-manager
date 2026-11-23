"""Explicit, resumable primary change; never deletes or closes a mailbox."""
import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_onec_sources import HREmploymentState
from app.models_zimbra_lifecycle import ZimbraEmploymentAction, ZimbraPrimaryTransition
from app.services.zimbra import ZimbraService
from app.services.zimbra_employment_lifecycle import ZimbraEmploymentLifecycleService, ACTIVE_STATUSES, normalize
from app.services.zimbra_lifecycle import ZimbraLifecycleService

logger = logging.getLogger(__name__)
ACTIVE_JOBS = {'queued', 'running', 'waiting_hr'}
STATUS_LABELS = {'queued': 'В очереди', 'running': 'Выполняется', 'waiting_hr': 'Ожидает проверки',
                 'failed': 'Требует проверки', 'completed': 'Выполнено', 'cancelled': 'Отменено'}
PHASE_LABELS = {'backup': 'Создание резервной копии', 'rename': 'Смена основного адреса',
                'cleanup': 'Удаление старого адреса', 'done': 'Переход завершён'}


class TransitionDeferred(RuntimeError):
    pass


class ZimbraPrimaryTransitionService:
    _lock = threading.Lock()

    def __init__(self, settings, db):
        self.settings, self.db = settings, db
        self.employment = ZimbraEmploymentLifecycleService(settings, db)

    def job(self, action_id):
        return self.db.scalar(select(ZimbraPrimaryTransition).where(ZimbraPrimaryTransition.action_id == action_id))

    def inspect(self, action_id):
        action = self.db.get(ZimbraEmploymentAction, action_id)
        if action is None or action.action != 'transition':
            raise ValueError('Переход между организациями не найден')
        if not self.employment._ready():
            raise TransitionDeferred('Ожидается завершение импорта и свежая кадровая выгрузка')
        spec = next((spec for spec in self.employment._current_specs(worker_key=action.worker_key)
                     if spec.plan_key == action.plan_key), None)
        if spec is None or spec.action != 'transition':
            raise ValueError('Переход больше не подтверждён. Обновите сведения после кадровой проверки')
        return action, spec

    def enqueue(self, action_id, actor, *, expected_old, expected_new, confirmed):
        if not confirmed:
            raise ValueError('Подтвердите смену основного адреса и удаление прежнего адреса')
        existing = self.job(action_id)
        if existing is not None and existing.status != 'cancelled':
            return existing
        action, spec = self.inspect(action_id)
        other = self.db.scalar(select(ZimbraPrimaryTransition).where(
            ZimbraPrimaryTransition.active_mailbox_id == spec.zimbra_id,
            ZimbraPrimaryTransition.action_id != action_id))
        if other is not None:
            raise ValueError('Для этого ящика уже есть незавершённая смена адреса. Откройте существующее задание')
        if normalize(expected_old) != spec.target_address or normalize(expected_new) != spec.replacement_address:
            raise ValueError('Адреса изменились. Обновите страницу и подтвердите актуальные адреса')
        identity = ZimbraService(self.settings).accounts_by_ids([spec.zimbra_id]).get(spec.zimbra_id)
        if identity is None or normalize(identity.primary_email) != spec.target_address:
            raise ValueError('Основной адрес ящика изменился; повторите проверку')
        states = {normalize(row.source_id): row for row in self.db.scalars(select(HREmploymentState).where(
            HREmploymentState.worker_key == spec.worker_key)).all()}
        mappings = list(self.db.scalars(select(EmailLoginMapping).where(EmailLoginMapping.worker_key == spec.worker_key)).all())
        identity_addresses = self._addresses(identity)
        mappings = [mapping for mapping in mappings if normalize(mapping.zimbra_id) == normalize(spec.zimbra_id)
                    or identity_addresses & {normalize(mapping.source_email), normalize(mapping.zimbra_email)}]
        records = list(self.db.scalars(select(HRSourceRecord).where(HRSourceRecord.worker_key == spec.worker_key)).all())
        sources = self.employment._address_sources_for_mailbox(identity=identity, mappings=mappings, states=states, records=records)
        new_source = sources.get(spec.replacement_address, spec.replacement_address.rsplit('@', 1)[-1])
        job = existing or ZimbraPrimaryTransition(action_id=action.id)
        values = dict(worker_key=spec.worker_key, active_mailbox_id=spec.zimbra_id,
            zimbra_id=spec.zimbra_id, old_address=spec.target_address, new_address=spec.replacement_address,
            old_source=spec.source_id, new_source=new_source, operator=actor,
            aliases_json=json.dumps(sorted(self._addresses(identity))), status='queued', phase='backup',
            backup_path='', backup_size_bytes=None, last_error='')
        for key, value in values.items():
            setattr(job, key, value)
        self.db.add(job)
        self.db.flush()
        self._validate(job)
        self.db.add(AuditLog(actor=actor, action='zimbra_primary_transition_requested', target=spec.zimbra_id,
            result='queued', details=f'{spec.target_address} -> {spec.replacement_address}; remove_old=true'))
        action.last_error = ''
        action.status = 'intervention'
        self.db.commit()
        return job

    def cancel(self, action_id, actor):
        if not self._lock.acquire(blocking=False):
            raise ValueError('Дождитесь завершения текущего этапа обработки')
        try:
            job = self.job(action_id)
            if job is None or job.status not in {'queued','waiting_hr','failed'}:
                raise ValueError('Это задание сейчас нельзя отменить')
            identity = ZimbraService(self.settings).accounts_by_ids([job.zimbra_id]).get(job.zimbra_id)
            if (identity is None or normalize(identity.zimbra_id) != normalize(job.zimbra_id)
                    or normalize(identity.primary_email) != job.old_address or job.new_address not in self._addresses(identity)):
                raise ValueError('Адреса уже изменены. Сначала завершите или восстановите переход')
            job.status = 'cancelled'
            job.active_mailbox_id = None
            job.last_error = ''
            self.db.add(AuditLog(actor=actor, action='zimbra_primary_transition_cancelled', target=job.zimbra_id,
                result='cancelled', details=f'job={job.id}; phase={job.phase}'))
            self.db.commit()
        finally:
            self._lock.release()

    def retry(self, action_id, actor):
        job = self.job(action_id)
        if job is None or job.status != 'failed':
            raise ValueError('Нет ошибочного задания для повторной проверки')
        job.status = 'queued'
        job.last_error = ''
        self.db.add(AuditLog(actor=actor, action='zimbra_primary_transition_retry', target=job.zimbra_id,
            result='queued', details=f'job={job.id}; phase={job.phase}'))
        self.db.commit()
        return job

    @staticmethod
    def _addresses(identity):
        return {normalize(identity.primary_email), *(normalize(value) for value in identity.addresses)}

    def _validate(self, job):
        # Each phase observes committed HR state again, including changes made
        # during a long export. A deferral applies to the old organization too.
        self.db.expire_all()
        if not self.employment._ready():
            raise TransitionDeferred('Ожидается завершение импорта и свежая кадровая выгрузка')
        states = {normalize(row.source_id): row for row in self.db.scalars(select(HREmploymentState).where(
            HREmploymentState.worker_key == job.worker_key)).all()}
        old, new = states.get(job.old_source), states.get(job.new_source)
        if old is None or new is None or not self.employment._due(old, self.employment._deferrals()):
            raise TransitionDeferred('Срок прекращения доступа в прежней организации не подтверждён или отложен')
        if new.status not in ACTIVE_STATUSES or not new.is_present:
            raise TransitionDeferred('Активная занятость в новой организации больше не подтверждена')
        if not self.employment._settings().allow_backup:
            raise TransitionDeferred('Администратор должен разрешить резервное копирование в настройках Zimbra')

    def _identity(self, service, job):
        identity = service.accounts_by_ids([job.zimbra_id]).get(job.zimbra_id)
        if identity is None or normalize(identity.zimbra_id) != normalize(job.zimbra_id):
            raise ValueError('Исходный физический ящик не найден; подмена другим ящиком запрещена')
        if normalize(identity.account_status) != 'active':
            raise ValueError('Ящик неактивен. Сначала проверьте его состояние')
        if normalize(identity.primary_email) not in {job.old_address, job.new_address}:
            raise ValueError('Основной адрес изменён вне этого задания. Требуется проверка')
        return identity

    def _restore_new_alias(self, service, job):
        identity = self._identity(service, job)
        if normalize(identity.primary_email) != job.old_address or job.new_address in self._addresses(identity):
            return
        owner = service.account_by_address(job.new_address)
        if owner is not None and normalize(owner.zimbra_id) != normalize(job.zimbra_id):
            raise ValueError('Новый адрес занят другим ящиком; восстановление alias требует администратора')
        service.add_alias_by_id(job.zimbra_id, job.new_address)
        if job.new_address not in self._addresses(self._identity(service, job)):
            raise ValueError('Восстановление дополнительного адреса не подтверждено')

    def _run_job(self, job):
        action = self.db.get(ZimbraEmploymentAction, job.action_id)
        job.status = 'running'
        job.last_error = ''
        self.db.commit()
        try:
            self._validate(job)
            service = ZimbraService(self.settings)
            identity = self._identity(service, job)
            if job.phase == 'backup':
                if normalize(identity.primary_email) != job.old_address or job.new_address not in self._addresses(identity):
                    raise ValueError('Адреса ящика изменились до создания резервной копии')
                config = self.employment._settings()
                backup = ZimbraLifecycleService(self.settings, self.db)._backup_account(
                    identity.primary_email, backup_dir=config.backup_dir, run_id=-job.id)
                if not backup.path or backup.size_bytes <= 0:
                    raise ValueError('Резервная копия не подтверждена; смена адреса запрещена')
                job.backup_path, job.backup_size_bytes = backup.path, backup.size_bytes
                job.phase = 'rename'
                self.db.commit()
            self._validate(job)
            if not job.backup_path or not job.backup_size_bytes:
                raise ValueError('Нет подтверждённой резервной копии')
            if job.phase == 'rename':
                try:
                    identity = self._identity(service, job)
                    if normalize(identity.primary_email) == job.old_address:
                        owner = service.account_by_address(job.new_address)
                        if owner is not None and normalize(owner.zimbra_id) != normalize(job.zimbra_id):
                            raise ValueError('Новый адрес принадлежит другому физическому ящику')
                        if job.new_address in self._addresses(identity):
                            self._validate(job)
                            service.remove_alias_by_id(job.zimbra_id, job.new_address)
                        self._validate(job)
                        # Persisted phase makes an interruption after alias
                        # removal resumable. All commands address the same ID.
                        identity = self._identity(service, job)
                        owner = service.account_by_address(job.new_address)
                        if owner is not None and normalize(owner.zimbra_id) != normalize(job.zimbra_id):
                            raise ValueError('Новый адрес занят другим ящиком')
                        if normalize(identity.primary_email) == job.old_address:
                            self._validate(job)
                            service.rename_account(job.zimbra_id, job.new_address)
                    if normalize(self._identity(service, job).primary_email) != job.new_address:
                        raise ValueError('Смена основного адреса не подтверждена Zimbra')
                except Exception:
                    # A lost SSH reply may follow a successful rename. Inspect
                    # the ID before restoring the detached active alias.
                    actual = self._identity(service, job)
                    if normalize(actual.primary_email) != job.new_address:
                        self._restore_new_alias(service, job)
                        raise
                job.phase = 'cleanup'
                self.db.commit()
            self._validate(job)
            identity = self._identity(service, job)
            if normalize(identity.primary_email) != job.new_address:
                raise ValueError('Удалять старый адрес можно только после подтверждённой смены основного')
            if job.old_address in self._addresses(identity):
                self._validate(job)
                service.remove_alias_by_id(job.zimbra_id, job.old_address)
            identity = self._identity(service, job)
            addresses = self._addresses(identity)
            other_aliases = set(json.loads(job.aliases_json)) - {job.old_address, job.new_address}
            if (normalize(identity.primary_email) != job.new_address or job.old_address in addresses
                    or not other_aliases.issubset(addresses)):
                raise ValueError('Итоговый состав адресов не подтверждён. Проверьте ящик')
            job.status, job.phase = 'completed', 'done'
            job.active_mailbox_id = None
            job.completed_at = datetime.now(timezone.utc)
            action.status, action.last_error = 'completed', ''
            action.completed_at = job.completed_at
            action.cancelled_at = None
            self.db.add(AuditLog(actor=job.operator, action='zimbra_primary_transition', target=job.zimbra_id,
                result='success', details=f'{job.old_address} -> {job.new_address}; backup={job.backup_path}; old_removed=true'))
        except TransitionDeferred as exc:
            job.status, job.last_error = 'waiting_hr', str(exc)
        except Exception as exc:
            job.status, job.last_error = 'failed', str(exc)[:4000]
            logger.exception('Не завершён переход Zimbra, задание %s', job.id)
        self.db.commit()

    def process_due(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            jobs = list(self.db.scalars(select(ZimbraPrimaryTransition).where(
                ZimbraPrimaryTransition.status.in_(ACTIVE_JOBS)).order_by(ZimbraPrimaryTransition.id)).all())
            for job in jobs:
                self._run_job(job)
        finally:
            self._lock.release()


class ZimbraPrimaryTransitionWorker:
    def __init__(self, settings, session_factory):
        self.settings, self.session_factory = settings, session_factory
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='zimbra-primary-transition', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                with self.session_factory() as db:
                    ZimbraPrimaryTransitionService(self.settings, db).process_due()
            except Exception:
                logger.exception('Ошибка фоновой смены адреса Zimbra')
            if self._stop.wait(60):
                break
