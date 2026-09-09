(() => {
  const page = document.querySelector('[data-recall-page]');
  if (!page) return;

  const disableSubmittedForm = (form, label) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (!button) return;
      button.disabled = true;
      button.textContent = label;
    });
  };

  const startForm = page.querySelector('[data-recall-start-form]');
  if (startForm) disableSubmittedForm(startForm, 'Запускается…');
  page.querySelectorAll('[data-recall-candidate-form]').forEach((form) => {
    disableSubmittedForm(form, 'Запускается…');
  });

  const progress = page.querySelector('[data-recall-progress]');
  if (!progress) return;

  const runId = Number(progress.dataset.runId);
  if (!Number.isFinite(runId) || runId < 1) return;

  const phase = progress.querySelector('[data-recall-phase]');
  const status = progress.querySelector('[data-recall-status]');
  const bar = progress.querySelector('[data-recall-progress-bar]');
  const fields = {
    processed_mailboxes: progress.querySelector('[data-recall-processed]'),
    total_mailboxes: progress.querySelector('[data-recall-total]'),
    found_messages: progress.querySelector('[data-recall-found]'),
    deleted_messages: progress.querySelector('[data-recall-deleted]'),
    remaining_messages: progress.querySelector('[data-recall-remaining]'),
    error_count: progress.querySelector('[data-recall-errors]'),
  };

  const render = (run) => {
    if (phase) phase.textContent = run.phase || '';
    if (status) status.textContent = run.status_label || run.status || '';
    Object.entries(fields).forEach(([name, node]) => {
      if (!node) return;
      const value = Number(run[name] || 0);
      node.textContent = name === 'total_mailboxes' && !value ? '—' : String(value);
    });
    if (bar) {
      const total = Number(run.total_mailboxes || 0);
      if (total > 0) {
        bar.max = total;
        bar.value = Math.min(Number(run.processed_mailboxes || 0), total);
      } else {
        bar.removeAttribute('value');
      }
    }
  };

  const poll = async () => {
    try {
      const response = await fetch(`/zimbra-recall/progress?run_id=${runId}`, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!payload.run) {
        window.setTimeout(poll, 2500);
        return;
      }
      render(payload.run);
      if (!payload.active) {
        window.location.assign(payload.run.result_url);
        return;
      }
    } catch (_error) {
      // Временный сетевой сбой не прерывает серверную операцию.
    }
    window.setTimeout(poll, 2000);
  };

  window.setTimeout(poll, 700);
})();
