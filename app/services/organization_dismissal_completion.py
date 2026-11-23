"""Local completion evidence for an organization dismissal; never blocks global AD."""
from sqlalchemy import select

from app.models_onec_sources import HREmploymentState
from app.models_techexpert import TechExpertSettings, TechExpertNotification
from app.models_zimbra_lifecycle import ZimbraEmploymentAction
from app.models_synology import SynologyAccountState
from app.services.onec_import_recovery import running_import


def organization_dismissal_complete(db, worker_key, dismissal_date, source_ids, *, mail_check_enabled=True):
    if running_import(db) is not None:
        return False
    sources = {str(value).strip().lower() for value in source_ids}
    if not sources:
        return False
    states = list(db.scalars(select(HREmploymentState).where(HREmploymentState.worker_key == worker_key)))
    active_sources = {state.source_id for state in states if state.status in {'active', 'scheduled'}}
    if not active_sources:
        return False
    current_sources = {state.source_id for state in states if state.dismissal_date == dismissal_date}
    if not sources.issubset(current_sources):
        return False
    if any(state.source_id in sources and state.status in {'active', 'scheduled'} for state in states):
        return False
    if mail_check_enabled:
        actions = list(db.scalars(select(ZimbraEmploymentAction).where(
            ZimbraEmploymentAction.worker_key == worker_key,
            ZimbraEmploymentAction.dismissal_date == dismissal_date,
            ZimbraEmploymentAction.source_id.in_(sources))))
        for source in sources:
            rows = [action for action in actions if action.source_id == source and action.status != 'cancelled']
            if not rows or any(row.status != 'completed' for row in rows):
                return False
    config = db.get(TechExpertSettings, 1)
    if config and config.enabled and config.source_domain.strip().lower() in sources:
        rows = list(db.scalars(select(TechExpertNotification).where(
            TechExpertNotification.worker_key == worker_key,
            TechExpertNotification.source_id.in_(sources),
            TechExpertNotification.dismissal_date == dismissal_date)))
        if not rows or any(row.status not in {'sent', 'skipped'} for row in rows):
            return False
    for account in db.scalars(select(SynologyAccountState).where(SynologyAccountState.worker_key == worker_key)):
        domain = (account.email or '').lower().rsplit('@', 1)[-1]
        if domain in sources and account.is_present and account.is_active:
            return False
    return True
