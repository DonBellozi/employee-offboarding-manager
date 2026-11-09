"""Recipient planning for an explicitly selected HR worker, never FIO guessing."""
from sqlalchemy import select

from app.models import CredentialDelivery, EmailLoginMapping, HRSourceRecord
from app.services.onec_import_recovery import running_import
from app.services.personnel_structure import PersonnelStructureService


def norm(value):
    return str(value or "").strip().lower()


def delivery_result(row):
    return {"email": row.email, "recipient": row.recipient, "status": row.status, "note": row.note}


DELIVERY_LABELS = {"pending": "Ожидает отправки", "sent": "Отправлено", "failed": "Не отправлено",
                   "skipped": "Пропущено", "unverified": "Требует проверки"}


def delivery_details(rows):
    return [(f"Реквизиты → {row.email}", DELIVERY_LABELS.get(row.status, row.status)
             + (f" · {row.note}" if row.note else "")) for row in rows]


def plan_recipients(db, *, operation, zimbra, personal_email, corporate_email, aliases=(), worker_key=""):
    """Additional recipients fail closed. Known created mailbox remains deliverable."""
    corporate_email = norm(corporate_email)
    created_addresses = {corporate_email, *(norm(a) for a in aliases)}
    rows = {}
    groups = {}
    warnings = []

    def add(email, group, status="pending", note=""):
        email = norm(email)
        if not email:
            return
        row = rows.get(email)
        if row is None:
            row = CredentialDelivery(operation_id=operation.id, email=email)
            rows[email] = row
            db.add(row)
        else:
            for key in list(groups):
                groups[key] = [r for r in groups[key] if r is not row]
                if not groups[key]: del groups[key]
        row.status, row.note = status, note
        if status == "pending":
            groups.setdefault(group, []).append(row)

    # The created address wins over its aliases, including a personal-address alias.
    if norm(personal_email):
        add(personal_email, "created" if norm(personal_email) in created_addresses else f"email:{norm(personal_email)}")
    add(corporate_email, "created")
    groups['created'].sort(key=lambda r: r.email != corporate_email)
    if not worker_key:
        for group in groups.values():
            for row in group: row.recipient = group[0].email
        return list(rows.values()), groups, warnings
    if running_import(db):
        warnings.append("Дополнительные адреса не проверены: выполняется кадровый импорт.")
    else:
        personnel = PersonnelStructureService(db)
        worker = personnel.worker_state(worker_key)
        active_sources = {norm(e.source_id) for e in worker.employments if e.active and e.is_present}
        records = list(db.scalars(select(HRSourceRecord)).all())
        extra = sorted({norm(r.corporate_email) for r in records if r.worker_key == worker_key
                        and norm(r.source_id) in active_sources and r.is_present and norm(r.corporate_email)})
        mappings = list(db.scalars(select(EmailLoginMapping)).all())
        to_lookup = []
        for email in extra:
            if email == corporate_email:
                continue
            # Any contradictory ownership requires an operator, not a best guess.
            owners = {r.worker_key for r in records if norm(r.corporate_email) == email and r.worker_key}
            owners.update(m.worker_key for m in mappings if email in {norm(m.source_email), norm(m.zimbra_email)} and m.worker_key)
            if owners != {worker_key} or email.count('@') != 1 or any(c.isspace() for c in email):
                add(email, "", "unverified", "Неоднозначный или некорректный адрес")
                warnings.append(f"Дополнительный адрес {email} требует проверки.")
            elif email in created_addresses:
                add(email, "created")
            else:
                to_lookup.append(email)
        try:
            accounts = zimbra.accounts_by_addresses([corporate_email, *to_lookup]) if to_lookup else {}
        except Exception:
            accounts = {}
        created = accounts.get(corporate_email)
        created_id = norm(getattr(created, 'zimbra_id', ''))
        for email in to_lookup:
            account = accounts.get(email)
            zid = norm(getattr(account, 'zimbra_id', ''))
            addresses = {norm(a) for a in getattr(account, 'addresses', ())}
            other_owners = {m.worker_key for m in mappings if zid and norm(m.zimbra_id) == zid and m.worker_key != worker_key}
            if not zid or email not in addresses or other_owners:
                add(email, "", "unverified", "Ящик или его владелец не подтверждён")
                warnings.append(f"Дополнительный ящик {email} не подтверждён; письмо не отправлено.")
            elif norm(account.account_status) != 'active':
                add(email, "", "skipped", "Ящик не активен")
                if email == norm(personal_email):
                    warnings.append("Указанный личный адрес совпадает с неактивным корпоративным ящиком.")
            else:
                group = "created" if zid == created_id or corporate_email in addresses else f"zimbra:{zid}"
                # A personal address equal to an HR corporate address gets the
                # same physical-mailbox identity, rather than a second message.
                add(email, group)
    # Prefer corporate delivery for the created box; otherwise a current HR address.
    for group in groups.values():
        for row in group:
            row.recipient = group[0].email
            if row.email != row.recipient:
                row.note = f"Общий ящик; отправка на {row.recipient}"
    return list(rows.values()), groups, warnings
