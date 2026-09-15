(() => {
  const host = document.getElementById('upcoming-dismissals-dynamic');
  if (!host) return;

  const fragmentUrl = host.dataset.fragmentUrl || '/dismissals/upcoming/fragment';
  const refreshIntervalMs = 30000;
  const detailsRefreshIntervalMs = 15000;
  let requestNumber = 0;
  let timer = 0;
  let detailsTimer = 0;

  async function refreshFragment(message = '', error = '') {
    const currentRequest = ++requestNumber;
    const url = new URL(fragmentUrl, window.location.origin);
    if (message) url.searchParams.set('message', message);
    if (error) url.searchParams.set('error', error);

    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'text/html'},
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const html = await response.text();
      if (currentRequest !== requestNumber) return;
      const incoming = new DOMParser().parseFromString(html, 'text/html');
      const identity = element => JSON.stringify([
        element.dataset.workerKey, element.dataset.dismissalDate,
      ]);
      const incomingKeys = new Set(Array.from(
        incoming.querySelectorAll('[data-upcoming-dismissal-details]'), identity
      ));
      const removed = Array.from(
        host.querySelectorAll('[data-upcoming-dismissal-details]')
      ).some(element => !incomingKeys.has(identity(element)));
      const journal = document.querySelector('.journal-card');
      let journalHtml = null;
      if (removed && journal) {
        // Publish both lists together. Do not lose the visible row if the
        // journal request fails, or close details the operator is reading.
        if (journal.querySelector('details[open]')) return;
        const journalResponse = await fetch(new URL('/', window.location.origin), {
          method: 'GET', credentials: 'same-origin', cache: 'no-store',
          headers: {'Accept': 'text/html'},
        });
        if (!journalResponse.ok) throw new Error(`HTTP ${journalResponse.status}`);
        const page = new DOMParser().parseFromString(await journalResponse.text(), 'text/html');
        const updatedJournal = page.querySelector('.journal-card');
        if (!updatedJournal) throw new Error('Journal unavailable');
        journalHtml = updatedJournal.innerHTML;
      }
      if (currentRequest !== requestNumber) return;
      if (journalHtml !== null && journal.querySelector('details[open]')) return;
      if (journalHtml !== null) journal.innerHTML = journalHtml;
      host.innerHTML = html;
    } catch (_) {
      // Сохраняем текущий список: временная ошибка фонового обновления
      // не должна очищать уже показанные увольнения.
    }
  }

  function scheduleRefresh() {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      const detailsAreOpen = host.querySelector(
        'details[data-upcoming-dismissal-details][open]'
      );
      if (document.visibilityState === 'visible' && !detailsAreOpen) {
        await refreshFragment();
      }
      scheduleRefresh();
    }, refreshIntervalMs);
  }

  async function refreshDetails(details) {
    const content = details.querySelector('[data-upcoming-details-content]');
    if (!content) return;
    content.setAttribute('aria-busy', 'true');

    const url = new URL(details.dataset.detailsUrl, window.location.origin);
    url.searchParams.set('worker_key', details.dataset.workerKey || '');
    url.searchParams.set('dismissal_date', details.dataset.dismissalDate || '');

    try {
      const response = await fetch(url, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'text/html'},
      });
      const html = await response.text();
      content.innerHTML = html;
    } catch (_) {
      if (!content.querySelector('.upcoming-details-heading')) {
        content.textContent = 'Не удалось загрузить сохранённые данные. Попробуйте открыть снова.';
      }
    } finally {
      content.removeAttribute('aria-busy');
    }
  }

  function scheduleDetailsRefresh(details) {
    window.clearTimeout(detailsTimer);
    if (!details?.open) return;
    detailsTimer = window.setTimeout(async () => {
      if (details.open && document.visibilityState === 'visible') {
        await refreshDetails(details);
      }
      scheduleDetailsRefresh(details);
    }, detailsRefreshIntervalMs);
  }

  host.addEventListener('toggle', async event => {
    const details = event.target.closest(
      'details[data-upcoming-dismissal-details]'
    );
    if (!details) return;
    if (!details.open) {
      window.clearTimeout(detailsTimer);
      return;
    }
    await refreshDetails(details);
    scheduleDetailsRefresh(details);
  }, true);

  host.addEventListener('click', event => {
    const close = event.target.closest('[data-upcoming-details-close]');
    if (!close) return;
    const details = close.closest('details[data-upcoming-dismissal-details]');
    if (details) details.open = false;
  });

  document.addEventListener('click', event => {
    const close = event.target.closest('[data-journal-details-close]');
    if (!close) return;
    const details = close.closest('details.journal-dismissal-details');
    if (details) details.open = false;
  });

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const details = host.querySelector(
      'details[data-upcoming-dismissal-details][open]'
    );
    if (details) details.open = false;
    const journalDetails = document.querySelector(
      'details.journal-dismissal-details[open]'
    );
    if (journalDetails) journalDetails.open = false;
  });

  host.addEventListener('submit', async event => {
    const form = event.target.closest('form[data-upcoming-dismissal-defer]');
    if (!form) return;

    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const originalText = button?.textContent || '';

    if (button) {
      button.disabled = true;
      button.textContent = 'Сохраняю...';
    }

    try {
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        body: new FormData(form),
        headers: {'Accept': 'application/json'},
      });
      const payload = await response.json().catch(() => ({}));

      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }

      await refreshFragment(payload.message || '');
    } catch (exc) {
      await refreshFragment('', exc?.message || 'Не удалось сохранить отсрочку');
    } finally {
      if (button?.isConnected) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (
      document.visibilityState === 'visible'
      && !host.querySelector('details[data-upcoming-dismissal-details][open]')
    ) {
      refreshFragment();
    }
  });

  scheduleRefresh();
})();
