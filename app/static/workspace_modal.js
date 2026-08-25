(() => {
  const dialog = document.getElementById('workspace-modal');
  const frame = document.getElementById('workspace-modal-frame');
  const title = document.getElementById('workspace-modal-title');
  const closeButton = document.getElementById('workspace-modal-close');

  if (!dialog || !frame || !title) return;

  let refreshOnClose = false;
  let changed = false;

  function modalUrl(href) {
    const url = new URL(href, window.location.origin);
    url.searchParams.set('modal', '1');
    return url.pathname + url.search + url.hash;
  }

  function openWorkspace(href, heading, shouldRefresh = false) {
    refreshOnClose = Boolean(shouldRefresh);
    changed = false;
    title.textContent = heading || 'Окно';
    frame.src = modalUrl(href);

    if (!dialog.open) {
      dialog.showModal();
    }
  }

  function closeWorkspace() {
    if (dialog.open) dialog.close();
  }

  document.addEventListener('click', event => {
    const trigger = event.target.closest('a[data-workspace-modal]');
    if (!trigger) return;

    const href = trigger.getAttribute('href');
    if (!href || href.startsWith('http')) return;

    event.preventDefault();
    openWorkspace(
      href,
      trigger.dataset.workspaceTitle || trigger.textContent.trim(),
      trigger.dataset.workspaceRefresh === 'true'
    );
  });

  closeButton?.addEventListener('click', closeWorkspace);

  dialog.addEventListener('click', event => {
    if (event.target === dialog) closeWorkspace();
  });

  function hardenEmbeddedPage() {
    if (!frame.src || frame.src === 'about:blank') return;

    try {
      const childWindow = frame.contentWindow;
      const childDocument = frame.contentDocument;
      if (!childWindow || !childDocument?.body) return;

      childDocument.body.classList.add('workspace-embedded');

      // A modal is an action workspace, not a second browser. Ordinary links
      // inside the embedded page must never turn the iframe into a navigable
      // copy of the whole application. Closing returns the operator to the
      // exact parent page and its filters.
      if (!childDocument.documentElement.dataset.workspaceLinksLocked) {
        childDocument.documentElement.dataset.workspaceLinksLocked = '1';
        childDocument.addEventListener('click', event => {
          const link = event.target.closest('a[href]');
          if (!link) return;

          const href = link.getAttribute('href') || '';
          if (
            !href
            || href.startsWith('#')
            || href.startsWith('javascript:')
          ) {
            return;
          }

          event.preventDefault();
          event.stopPropagation();

          // Explicitly marked action steps continue inside the same workspace.
          // All other links keep the historical behavior and close the modal.
          if (link.hasAttribute('data-workspace-navigate')) {
            childWindow.location.href = modalUrl(href);
            return;
          }

          closeWorkspace();
        });
      }

      // If an embedded child deliberately signals a successful mutation,
      // close immediately. The dedicated postMessage handler below remains
      // the primary path; this event is a same-origin fallback.
      if (!childDocument.documentElement.dataset.workspaceSavedListener) {
        childDocument.documentElement.dataset.workspaceSavedListener = '1';
        childWindow.addEventListener('workspace-modal-saved', () => {
          changed = true;
          closeWorkspace();
        });
      }
    } catch (_) {
      // Same-origin pages are expected. Keep the modal usable if the browser
      // temporarily denies access while the iframe is navigating.
    }
  }

  frame.addEventListener('load', hardenEmbeddedPage);

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin) return;
    if (event.source !== frame.contentWindow) return;
    if (event.data?.type !== 'workspace-modal-saved') return;
    changed = true;
    closeWorkspace();
  });

  dialog.addEventListener('close', () => {
    frame.src = 'about:blank';
    if (changed || refreshOnClose) {
      window.location.reload();
    }
  });
})();
