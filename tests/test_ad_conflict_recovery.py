from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.models import AuditLog, HRSourceRecord
from app.models_dismissal_lifecycle import FinalDismissalAutomationState, FinalDismissalBlockRun
from app.models_onec_sources import HREmploymentState
from app.routers.dashboard_dismissals import _journal_items
from app.services import final_dismissal_lifecycle as module
from app.services.upcoming_dismissals import UpcomingDismissalService


STAMP = datetime(2030, 1, 16, 20, 0, tzinfo=ZoneInfo('Europe/Moscow'))


@pytest.fixture
def scenario(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        settings = Settings(_env_file=None, app_timezone='Europe/Moscow', onec_source_domain='a.test',
                            zimbra_domains=['a.test','b.test'])
        records=[]
        for domain, login in [('a.test','first'), ('b.test','second')]:
            row=HRSourceRecord(worker_key='worker', source_id=domain, fio='Иванов Иван Иванович',
                login=login, corporate_email=f'{login}@{domain}', is_present=True)
            records.append(row)
            db.add_all([row, HREmploymentState(worker_key='worker', source_id=domain,
                fio=row.fio, status='dismissed', dismissal_date=STAMP.date(), is_present=True)])
        db.add(FinalDismissalAutomationState(id=1, activated_on=date(2030,1,1)))
        db.commit()
        monkeypatch.setattr(module.FinalDismissalLifecycleService,'local_now',property(lambda self: STAMP))
        monkeypatch.setattr(UpcomingDismissalService,'today',property(lambda self: STAMP.date()))
        service=module.FinalDismissalLifecycleService(settings,db)
        service._ready=Mock(return_value=True)
        service._create_reactivation_alerts=Mock()
        service._post_reconcile_worker=Mock()
        ad=Mock()
        ad.get_user.return_value=SimpleNamespace(username='first',object_guid='guid-first',is_enabled=False)
        monkeypatch.setattr(module,'ActiveDirectoryService',lambda settings: ad)
        yield db,settings,records,service,ad
    engine.dispose()


def start(scenario):
    db,settings,records,service,ad=scenario
    service.process()
    run=db.scalar(select(FinalDismissalBlockRun))
    assert run.status=='intervention'
    assert 'разные логины AD' in run.last_error
    ad.get_user.assert_not_called()
    return run


def resolve(scenario):
    db,settings,records,service,ad=scenario
    records[1].login='first'
    db.commit()


def test_resolved_collision_finishes_existing_run_and_moves_to_journal(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    assert UpcomingDismissalService(settings,db).list_upcoming()
    resolve(scenario)
    service.process()
    assert run.status=='success' and run.completed_at is not None and run.last_error==''
    assert len(db.scalars(select(FinalDismissalBlockRun)).all())==1
    assert UpcomingDismissalService(settings,db).list_upcoming()==[]
    items=[item for item in _journal_items(db,today=STAMP.date(),timezone_name='Europe/Moscow')
           if item['kind']=='dismissal' and item['record_id']==run.id]
    assert len(items)==1
    ad.disable_user.assert_not_called()  # Manually disabled already: read confirms it.
    old=next(t for t in service._targets(run.id) if t.target_key=='ad:conflict')
    assert old.status=='cancelled' and old.last_result==module.RESOLVED_AD_CONFLICT
    assert 'разные логины AD' in old.last_error  # History was not deleted.


def test_already_completed_valid_target_not_repeated_and_repair_idempotent(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    candidate=next(iter(service._candidate_map().values()))
    service._ensure_run(candidate)
    target=next(t for t in service._targets(run.id) if t.target_key=='ad:first')
    target.status='already_completed'
    target.completed_at=STAMP
    db.commit()
    service.process()
    service.process()
    assert run.status=='success'
    ad.get_user.assert_not_called()
    assert len(db.scalars(select(AuditLog).where(AuditLog.action=='final_dismissal_ad_conflict_resolved')).all())==1


def test_unresolved_collision_still_requires_attention(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    service.process()
    assert run.status=='intervention' and run.completed_at is None
    assert UpcomingDismissalService(settings,db).list_upcoming()
    ad.disable_user.assert_not_called()


def test_missing_plan_does_not_silently_erase_conflict(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    for row in records: row.login=''
    db.commit()
    service.process()
    assert run.status=='intervention'
    assert next(t for t in service._targets(run.id) if t.target_key=='ad:conflict').status=='intervention'


def test_real_ad_error_is_not_hidden_by_resolved_plan(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    ad.get_user.side_effect=RuntimeError('access denied')
    service.process()
    assert run.status=='intervention' and run.completed_at is None
    assert 'access denied' in run.last_error
    assert UpcomingDismissalService(settings,db).list_upcoming()


def test_not_ready_does_not_repair_or_perform_actions(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    service._ready.return_value=False
    assert service.process()['status']=='sources_not_ready'
    assert run.status=='intervention'
    ad.get_user.assert_not_called()


def test_deferral_prevents_recovery_action_until_due(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    original=service._candidate_map
    candidate=next(iter(original().values()))
    candidate['effective_block_date']=date(2030,1,23)
    service._candidate_map=lambda:{('worker',STAMP.date()):candidate}
    service.process()
    assert run.completed_at is None
    ad.get_user.assert_not_called()


def test_new_conflict_reopens_diagnostic_and_blocks_old_pending_target(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    candidate=next(iter(service._candidate_map().values()))
    service._ensure_run(candidate)
    records[1].login='second'
    db.commit()
    service.process()
    old=next(t for t in service._targets(run.id) if t.target_key=='ad:conflict')
    assert old.status=='intervention' and old.last_result==''
    assert run.status=='intervention' and run.completed_at is None
    ad.get_user.assert_not_called()
    resolve(scenario)
    service.process()
    assert run.status=='success'


def test_active_in_second_organization_never_blocks(scenario):
    db,settings,records,service,ad=scenario
    run=start(scenario)
    resolve(scenario)
    state=db.scalar(select(HREmploymentState).where(HREmploymentState.source_id=='b.test'))
    state.status='active'
    state.dismissal_date=None
    db.commit()
    service.process()
    assert run.status=='cancelled' and run.completed_at is None
    ad.get_user.assert_not_called()
