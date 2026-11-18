"""Six-hour read-only index for manual blocking outside the HR registry."""
import hashlib
import logging
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import ADProvisioningOperation, ProvisioningOperation
from app.models_blocking_directory import BlockingDirectoryAccount, BlockingDirectorySync
from app.services.ad import ActiveDirectoryService
from app.services.zimbra import ZimbraService

logger = logging.getLogger(__name__)
REFRESH_HOURS = 6


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().replace('ё', 'е').split())


def utcnow():
    return datetime.now(timezone.utc)


class BlockingDirectoryService:
    _seed_lock = threading.Lock()

    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    def _upsert(self, system, key, **values):
        row = self.db.scalar(select(BlockingDirectoryAccount).where(
            BlockingDirectoryAccount.system == system, BlockingDirectoryAccount.account_key == key))
        if row is None:
            row = BlockingDirectoryAccount(system=system, account_key=key)
            self.db.add(row)
        for name, value in values.items():
            if name == 'aliases':
                continue
            if (system == 'created' and name in {'ad_login', 'zimbra_email'}
                    and not value and getattr(row, name)):
                continue
            setattr(row, name, value)
        row.search_text = normalized(' '.join(str(values.get(name, '')) for name in
            ('fio', 'login', 'email', 'ad_login', 'zimbra_email')) + ' ' + values.get('aliases', ''))
        row.present = True
        row.checked_at = utcnow()
        self.db.flush()
        return row

    def seed_created(self):
        """Creation history is searchable immediately, including partial success."""
        with self._seed_lock:
            self._seed_created()

    def _seed_created(self):
        for operation in self.db.scalars(select(ProvisioningOperation).where(
            (ProvisioningOperation.ad_created.is_(True)) | (ProvisioningOperation.zimbra_created.is_(True))
        ).order_by(ProvisioningOperation.id)).all():
            fio = ' '.join(filter(None, (operation.last_name, operation.first_name, operation.middle_name)))
            self._upsert('created', f'{operation.login.lower()}|{operation.corporate_email.lower()}',
                fio=fio, login=operation.login, email=operation.corporate_email,
                ad_login=operation.login if operation.ad_created else '',
                zimbra_email=operation.corporate_email if operation.zimbra_created else '',
                status='created')
        for operation in self.db.scalars(select(ADProvisioningOperation).where(
            ADProvisioningOperation.ad_created.is_(True)).order_by(ADProvisioningOperation.id)).all():
            self._upsert('created', f'ad:{operation.login.lower()}', fio=operation.full_name,
                login=operation.login, email=operation.corporate_email, ad_login=operation.login,
                zimbra_email=operation.corporate_email, status='created')
        self.db.commit()

    def search(self, query):
        self.seed_created()
        terms = normalized(query).split()
        return [row for row in self.db.scalars(select(BlockingDirectoryAccount).where(
            BlockingDirectoryAccount.present.is_(True)).order_by(BlockingDirectoryAccount.fio)).all()
            if all(term in row.search_text for term in terms)]

    def status(self):
        return list(self.db.scalars(select(BlockingDirectorySync).order_by(BlockingDirectorySync.system)).all())

    def refresh_due(self):
        self.seed_created()
        for system in ('ad', 'zimbra'):
            state = self.db.get(BlockingDirectorySync, system)
            if state is None:
                state = BlockingDirectorySync(system=system)
                self.db.add(state)
                self.db.commit()
            due = state.next_attempt_at
            if due is not None:
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                if due > utcnow():
                    continue
            try:
                # Finish the remote read before changing any existing snapshot.
                if system == 'ad':
                    users = ActiveDirectoryService(self.settings).directory_users()
                    entries = [(user.object_guid, dict(fio=user.display_name, login=user.username,
                        email=user.email, ad_login=user.username, ad_guid=user.object_guid,
                        status='active' if user.is_enabled else 'disabled')) for user in users]
                else:
                    accounts = ZimbraService(self.settings).directory_accounts()
                    entries = [(item['identity'].zimbra_id, dict(fio=item['fio'],
                        login=item['identity'].login, email=item['identity'].primary_email,
                        zimbra_email=item['identity'].primary_email, zimbra_id=item['identity'].zimbra_id,
                        aliases=' '.join(item['identity'].addresses), status=item['identity'].account_status))
                        for item in accounts]
                for row in self.db.scalars(select(BlockingDirectoryAccount).where(
                    BlockingDirectoryAccount.system == system)).all():
                    row.present = False
                for key, values in entries:
                    if not key:
                        raise ValueError('Справочник вернул учётку без стабильного идентификатора')
                    self._upsert(system, key, **values)
                state.last_success_at = utcnow()
                state.next_attempt_at = utcnow() + timedelta(hours=REFRESH_HOURS)
                state.error = ''
                self.db.commit()
            except Exception:
                self.db.rollback()
                state = self.db.get(BlockingDirectorySync, system)
                state.error = 'Не удалось обновить справочник; используются предыдущие данные'
                state.next_attempt_at = utcnow() + timedelta(minutes=15)
                self.db.commit()
                logger.exception('Ошибка обновления справочника блокировки: %s', system)

    @staticmethod
    def worker_key(row):
        return hashlib.sha256(f'blocking-directory:{row.system}:{row.account_key}'.encode()).hexdigest()


class BlockingDirectoryWorker:
    """Independent of manual actions and dismissal/registration workers."""
    def __init__(self, settings, session_factory):
        self.settings, self.session_factory = settings, session_factory
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='blocking-directory', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            try:
                with self.session_factory() as db:
                    BlockingDirectoryService(self.settings, db).refresh_due()
            except Exception:
                logger.exception('Ошибка фонового справочника блокировки')
            if self._stop.wait(60):
                break
