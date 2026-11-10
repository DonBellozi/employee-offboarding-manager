"""Explicit operator resolution of historical HR login ambiguity. AD reads only."""
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AuditLog, EmailLoginMapping, HRSourceRecord
from app.models_dismissal_lifecycle import FinalDismissalBlockRun, FinalDismissalBlockTarget
from app.services.ad import ActiveDirectoryService
from app.services.onec_import_recovery import running_import
from app.services.personnel_structure import PersonnelStructureService


def norm(value):
    return str(value or "").strip().casefold().strip("{}")


def fio(value):
    return " ".join(norm(value).replace("ё", "е").split())


class DismissalADMappingService:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    def context(self, run_id):
        self.db.expire_all()
        run = self.db.get(FinalDismissalBlockRun, run_id)
        if run is None or run.status not in {"pending", "running", "partial", "intervention"}:
            raise ValueError("Увольнение не найдено или уже завершено. Обновите журнал.")
        if running_import(self.db):
            raise ValueError("Выполняется кадровый импорт. Повторите после его завершения.")
        worker = PersonnelStructureService(self.db).worker_state(run.worker_key)
        if not worker.final_dismissed or worker.final_dismissal_date != run.dismissal_date:
            raise ValueError("Кадровое состояние изменилось или есть активная занятость. Сопоставление остановлено.")
        conflict = self.db.scalar(select(FinalDismissalBlockTarget.id).where(
            FinalDismissalBlockTarget.run_id == run.id,
            FinalDismissalBlockTarget.system == "ad",
            FinalDismissalBlockTarget.target_key == "ad:conflict",
        ))
        if conflict is None:
            raise ValueError("Для этой записи нет коллизии AD.")
        records = list(self.db.scalars(select(HRSourceRecord).where(
            HRSourceRecord.worker_key == run.worker_key,
        ).order_by(HRSourceRecord.source_id)))
        if not records:
            raise ValueError("Нет сохранённых кадровых записей работника.")
        mappings = list(self.db.scalars(select(EmailLoginMapping).where(EmailLoginMapping.worker_key == run.worker_key)))
        return {"run": run, "records": records, "mapping_by_source": {norm(m.source_domain): m for m in mappings}}

    def inspect(self, run_id, login):
        context = self.context(run_id)
        login = norm(login)
        if not login or len(login) > 128 or any(c.isspace() for c in login) or '@' in login:
            raise ValueError("Укажите точный логин AD.")
        if not self.settings.ad_check_enabled:
            raise ValueError("Проверка AD отключена.")
        try:
            user = ActiveDirectoryService(self.settings).get_user(login)
        except Exception:
            raise ValueError("Не удалось проверить AD. Повторите после восстановления связи.") from None
        if user is None or not user.object_guid:
            raise ValueError("AD не подтвердила учётку с objectGUID.")
        if fio(user.display_name) not in {fio(r.fio) for r in context['records']}:
            raise ValueError("ФИО в AD не совпадает с кадровой историей. Требуется отдельная проверка владельца.")
        mappings = list(self.db.scalars(select(EmailLoginMapping)))
        if any(m.worker_key != context['run'].worker_key and norm(m.ad_object_guid) == norm(user.object_guid)
               for m in mappings if m.ad_object_guid):
            raise ValueError("Эта AD-учётка сопоставлена другому работнику.")
        return user

    def save(self, run_id, login, expected_guid, actor, confirmed):
        if not confirmed or not expected_guid:
            raise ValueError("Проверьте учётку и подтвердите выбор.")
        user = self.inspect(run_id, login)
        if norm(user.object_guid) != norm(expected_guid):
            raise ValueError("Учётка AD изменилась после проверки. Проверьте заново.")
        context = self.context(run_id)  # HR may change during the LDAP request.
        mappings = list(self.db.scalars(select(EmailLoginMapping)))
        if any(m.worker_key != context['run'].worker_key and norm(m.ad_object_guid) == norm(user.object_guid)
               for m in mappings if m.ad_object_guid):
            raise ValueError("Сопоставление изменилось. Учётка связана с другим работником.")
        now = datetime.now(timezone.utc)
        changed = False
        previous = []
        for record in context['records']:
            mapping = next((m for m in mappings if m.worker_key == record.worker_key
                            and norm(m.source_domain) == norm(record.source_id)), None)
            if mapping is None:
                mapping = EmailLoginMapping(worker_key=record.worker_key, source_domain=norm(record.source_id),
                    source_email=record.corporate_email, ad_object_guid='', ad_login='', zimbra_id='',
                    zimbra_email='', created_by=actor)
                self.db.add(mapping)
            if norm(mapping.ad_object_guid) != norm(user.object_guid) or norm(mapping.ad_login) != norm(user.username):
                changed = True
                previous.append({'source_id': record.source_id, 'ad_login': mapping.ad_login,
                                 'ad_object_guid': mapping.ad_object_guid})
            mapping.ad_object_guid = user.object_guid
            mapping.ad_login = user.username
            mapping.last_verified_at = now
            mapping.updated_at = now
        if changed:
            self.db.add(AuditLog(actor=actor, action='dismissal_ad_identity_confirmed',
                target=f"final-dismissal:{run_id}", result='success', details=json.dumps({
                    'ad_login': user.username, 'ad_object_guid': user.object_guid,
                    'sources': [r.source_id for r in context['records']], 'scope': 'ad_only',
                    'previous': previous,
                }, ensure_ascii=False)))
        self.db.commit()
        return user
