"""Manual recall queue: fixed batches, one combined search per mailbox.

Only this coordinator owns the ORM session; pool workers receive immutable
targets and a cooperative stop event. No lifecycle/background HR locks.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from sqlalchemy import select, update

from app.models_zimbra_recall import ZimbraMailRecallBatch, ZimbraMailRecallRun
from app.services.zimbra import ZimbraService
from app.services.zimbra_mail_cleanup import SEARCH_LIMIT, ZimbraMailCleanupService, utcnow
from app.services.zimbra_mail_recall import (
    DELETE_PASSES, RecallCancelled, RecallMailboxResult, RecallTarget,
)


def combined_query(service, mailbox: str, targets: tuple[RecallTarget, ...]) -> str:
    queries = [service.build_message_query(
        target.message_id, preserve_sent=mailbox == target.author_mailbox,
    ) for target in targets]
    return queries[0] if len(queries) == 1 else " OR ".join(f"({query})" for query in queries)


def process_mailbox(service, zimbra, mailbox, targets, cancel):
    started = time.monotonic()
    query = combined_query(service, mailbox, targets)
    by_message_id = {target.message_id: target.run_id for target in targets}
    found = {target.run_id: set() for target in targets}
    attempted = {target.run_id: set() for target in targets}
    deleted = {target.run_id: set() for target in targets}
    errors = {target.run_id: "" for target in targets}
    remaining_ids: set[str] = set()
    more = False
    client = None
    try:
        if cancel.is_set():
            return None
        client = zimbra._client()

        def search():
            return ZimbraMailCleanupService.parse_search_output(zimbra.execute_mailbox_command(
                client, mailbox, ["search", "-t", "message", "-l", str(SEARCH_LIMIT), query],
                timeout=180,
            ))

        for _ in range(DELETE_PASSES):
            if cancel.is_set():
                break
            hits = search()
            remaining_ids = set(hits.message_ids)
            more = hits.more
            if not remaining_ids:
                break
            approved: dict[str, int] = {}
            for local_id in hits.message_ids:
                if cancel.is_set():
                    break
                if len(targets) == 1:
                    run_id = targets[0].run_id
                else:
                    candidate = service._read_candidate(zimbra, client, mailbox, local_id)
                    run_id = by_message_id.get(candidate.message_id)
                    if run_id is None:
                        raise RuntimeError("Найденное письмо не соответствует Message-ID пакета; удаление в ящике остановлено")
                approved[local_id] = run_id
                found[run_id].add(local_id)
            # One delete command for this mailbox, after every hit is identified.
            if cancel.is_set():
                break
            zimbra.execute_mailbox_command(
                client, mailbox, ["deleteMessage", ",".join(approved)], timeout=180, mutating=True,
            )
            for local_id, run_id in approved.items():
                attempted[run_id].add(local_id)
        # Even after Stop, finish this read-only verification of a command
        # already sent. We cannot revoke a remote deletion or claim it undone.
        if any(attempted.values()):
            verification = search()
            remaining_ids = set(verification.message_ids)
            more = verification.more
            if more:
                # A truncated result cannot prove that absent IDs were deleted.
                raise RuntimeError("Проверка удаления достигла лимита; результат не подтверждён")
            for run_id in deleted:
                deleted[run_id] = attempted[run_id] - remaining_ids
        if more:
            raise RuntimeError("Достигнут лимит копий в ящике; часть сообщений могла остаться")
    except Exception as exc:
        for run_id in errors:
            errors[run_id] = str(exc)[:2000]
    finally:
        if client is not None:
            client.close()
    return {target.run_id: RecallMailboxResult(
        mailbox=mailbox,
        found=len(found[target.run_id]),
        deleted=len(deleted[target.run_id]),
        remaining=len(found[target.run_id] - deleted[target.run_id]),
        sent_excluded=mailbox == target.author_mailbox,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=errors[target.run_id],
    ) for target in targets}


def _cancelled(service, batch, event):
    status = service.db.scalar(select(ZimbraMailRecallBatch.status).where(
        ZimbraMailRecallBatch.id == batch.id,
    ))
    if status == "stopping":
        event.set()
    return event.is_set()


def _prepare_target(service, run, zimbra, event):
    if event.is_set():
        raise RecallCancelled()
    author = zimbra.administrative_account_by_address(run.sender_email)
    if author is None:
        raise RuntimeError("Не удалось однозначно определить основной ящик автора. Удаление не начато, чтобы сохранить «Отправленные»")
    run.author_mailbox = author.primary_email
    if run.lookup_mailbox:
        lookup = zimbra.administrative_account_by_address(run.lookup_mailbox)
        if lookup is None:
            raise RuntimeError("Ящик для поиска полученного письма не найден. Укажите конкретного получателя, не группу рассылки")
        run.lookup_mailbox = lookup.primary_email
    service.db.commit()
    if event.is_set():
        raise RecallCancelled()
    if not run.message_id:
        candidates = service._find_source_candidates(run, zimbra, event)
        run.candidates_json = json.dumps([row.as_dict(service.settings.app_timezone) for row in candidates], ensure_ascii=False)
        if not candidates:
            raise RuntimeError("Письмо не найдено в выбранном ящике. Проверьте дату, время, получателя и тему или укажите Message-ID полученной копии")
        if len(candidates) > 1:
            run.status = "needs_selection"
            run.error_message = "Найдено несколько писем. Выберите нужное; остальные запросы пакета продолжат выполняться"
            service._audit(run.initiated_by, "zimbra_mail_recall_needs_selection", f"recall:{run.id}", {"candidate_count": len(candidates)})
            service.db.commit()
            return None
        candidate = candidates[0]
        run.message_id, run.source_local_id = candidate.message_id, candidate.local_id
        run.source_subject, run.source_sent_at = candidate.subject, candidate.sent_at
    service.db.commit()
    return RecallTarget(run.id, run.message_id, run.author_mailbox)


def execute_batch(service, batch):
    started = time.monotonic()
    event = threading.Event()
    with service._events_lock:
        service._cancel_events[batch.id] = event
    runs = service.batch_runs(batch.id)
    try:
        if service.settings.dry_run or service.settings.zimbra_backend == "disabled":
            raise RuntimeError("Удаление запрещено текущими настройками Zimbra / DRY_RUN")
        zimbra = ZimbraService(service.settings)
        targets = []
        seen = set()
        for run in runs:
            if _cancelled(service, batch, event):
                break
            run.status, run.started_at = "running", utcnow()
            service.db.commit()
            try:
                target = _prepare_target(service, run, zimbra, event)
                if target:
                    if target.message_id in seen:
                        raise ValueError("Это письмо уже обрабатывается другим запросом того же пакета")
                    seen.add(target.message_id)
                    targets.append(target)
            except RecallCancelled:
                event.set()
                break
            except Exception as exc:
                service._fail(run, exc, started=started)

        if targets and not _cancelled(service, batch, event):
            with zimbra._query_lock:
                mailboxes = list(dict.fromkeys(zimbra.list_user_mailboxes()))
            if any(target.author_mailbox not in mailboxes for target in targets):
                raise RuntimeError("Ящик одного из авторов отсутствует в списке пользовательских ящиков. Удаление не начато")
            batch.total_mailboxes = len(mailboxes)
            by_id = {run.id: run for run in runs}
            for target in targets:
                by_id[target.run_id].total_mailboxes = len(mailboxes)
            service.db.commit()
            iterator = iter(mailboxes)
            workers = max(1, min(int(service.settings.zimbra_mail_cleanup_workers), len(mailboxes) or 1))
            details = {target.run_id: [] for target in targets}
            # Bounded in-flight window: never enqueue thousands of futures.
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zimbra-recall") as pool:
                pending = set()

                def submit_next():
                    if event.is_set():
                        return
                    mailbox = next(iterator, None)
                    if mailbox is not None:
                        pending.add(pool.submit(process_mailbox, service, zimbra, mailbox, tuple(targets), event))

                for _ in range(workers):
                    submit_next()
                while pending:
                    _cancelled(service, batch, event)
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        if result is not None:
                            batch.processed_mailboxes += 1
                            for run_id, item in result.items():
                                run = by_id[run_id]
                                run.processed_mailboxes += 1
                                run.matched_mailboxes += int(item.found > 0)
                                run.found_messages += item.found
                                run.deleted_messages += item.deleted
                                run.remaining_messages += item.remaining
                                run.error_count += bool(item.error)
                                run.progress_at = utcnow()
                                if item.found or item.error:
                                    details[run_id].append(item.as_dict())
                                    run.details_json = json.dumps(details[run_id], ensure_ascii=False)
                            for field in ("found_messages", "deleted_messages", "remaining_messages", "error_count"):
                                setattr(batch, field, sum(getattr(run, field) for run in runs))
                            batch.duration_ms = int((time.monotonic() - started) * 1000)
                            service.db.commit()
                        _cancelled(service, batch, event)
                        submit_next()
        stopped = _cancelled(service, batch, event)
        for run in runs:
            # An operator may already have selected an ambiguous candidate
            # and queued it as a separate batch while other targets ran.
            if service.db.scalar(select(ZimbraMailRecallRun.batch_id).where(
                ZimbraMailRecallRun.id == run.id,
            )) != batch.id:
                continue
            if run.status not in {"running", "queued"}:
                continue
            run.completed_at, run.progress_at = utcnow(), utcnow()
            run.duration_ms = int((time.monotonic() - started) * 1000)
            if stopped:
                run.status = "cancelled"
                run.error_message = "Остановлено администратором. Обработана только часть ящиков; уже удалённые письма не восстановлены"
            elif run.error_count or run.remaining_messages:
                run.status = "warning" if run.deleted_messages else "failed"
                run.error_message = "Не все копии удалось удалить или подтвердить удаление. Проверьте ошибки"
            elif not run.found_messages:
                run.status = "warning"
                run.error_message = "По этому Message-ID копий не найдено. ID в «Отправленных» может отличаться: возьмите его из полученного письма или ищите в ящике получателя"
            else:
                run.status, run.error_message = "success", ""
            service._audit(run.initiated_by, "zimbra_mail_recall_completed", f"recall:{run.id}", {
                "status": run.status, "batch_id": batch.id,
                "checked_mailboxes": run.processed_mailboxes,
                "deleted_messages": run.deleted_messages, "error_count": run.error_count,
            })
        batch.status = "cancelled" if stopped else "success" if all(run.status == "success" for run in runs) else "warning"
    except Exception as exc:
        service.db.rollback()
        for run in runs:
            if run.status in {"running", "queued"}:
                service._fail(run, exc, started=started)
        batch.status = "failed"
        batch.error_message = str(exc)[:4000]
    finally:
        # Refresh the stop flag before writing the terminal state, but never
        # resume a batch automatically after an application restart.
        if service.db.scalar(select(ZimbraMailRecallBatch.status).where(ZimbraMailRecallBatch.id == batch.id)) == "stopping":
            batch.status = "cancelled"
        for field in ("found_messages", "deleted_messages", "remaining_messages", "error_count"):
            setattr(batch, field, sum(int(getattr(run, field) or 0) for run in runs))
        batch.duration_ms = int((time.monotonic() - started) * 1000)
        batch.completed_at = utcnow()
        service._audit(batch.initiated_by, "zimbra_mail_recall_batch_completed", f"recall-batch:{batch.id}", {
            "status": batch.status, "checked_mailboxes": batch.processed_mailboxes,
            "deleted_messages": batch.deleted_messages,
        })
        service.db.commit()
        with service._events_lock:
            service._cancel_events.pop(batch.id, None)


def execute_queue(service):
    while True:
        if not service._run_lock.acquire(blocking=False):
            return
        try:
            while True:
                batch = service.db.scalar(select(ZimbraMailRecallBatch).where(
                    ZimbraMailRecallBatch.status == "queued",
                ).order_by(ZimbraMailRecallBatch.id).limit(1).execution_options(populate_existing=True))
                if batch is None:
                    break
                claimed = service.db.execute(update(ZimbraMailRecallBatch).where(
                    ZimbraMailRecallBatch.id == batch.id,
                    ZimbraMailRecallBatch.status == "queued",
                ).values(status="running"))
                service.db.commit()
                if claimed.rowcount:
                    execute_batch(service, batch)
        finally:
            service._run_lock.release()
        # Close the enqueue-vs-unlock race: a concurrent starter may have seen
        # the old lock just as this worker observed an empty queue.
        if service.db.scalar(select(ZimbraMailRecallBatch.id).where(
            ZimbraMailRecallBatch.status == "queued",
        ).limit(1)) is None:
            return
