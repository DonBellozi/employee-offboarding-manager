(() => {
  const panel = document.getElementById("cleanup-live-progress");
  if (!panel) return;

  const title = panel.querySelector("[data-cleanup-progress-title]");
  const note = panel.querySelector("[data-cleanup-progress-note]");
  const runList = panel.querySelector("[data-cleanup-progress-runs]");
  const startForms = Array.from(document.querySelectorAll("form[data-cleanup-start]"));
  let isActive = panel.dataset.active === "true";
  let hadActiveRun = isActive;

  const setButtonsDisabled = (disabled) => {
    startForms.forEach((form) => {
      form.querySelectorAll("button[type='submit']").forEach((button) => {
        if (button.dataset.cleanupOriginallyDisabled === undefined) {
          button.dataset.cleanupOriginallyDisabled = button.disabled ? "true" : "false";
        }
        button.disabled = disabled || button.dataset.cleanupOriginallyDisabled === "true";
      });
    });
  };

  const addText = (parent, tag, value) => {
    const element = document.createElement(tag);
    element.textContent = value;
    parent.appendChild(element);
  };

  const renderRuns = (runs) => {
    runList.replaceChildren();
    runs.forEach((run) => {
      const article = document.createElement("article");
      article.className = "cleanup-progress-run";

      const caption = document.createElement("div");
      caption.className = "cleanup-progress-caption";
      addText(caption, "strong", run.rule_name);
      addText(caption, "span", `${run.mode_label} · ${run.status_label}`);
      article.appendChild(caption);

      const progress = document.createElement("progress");
      progress.max = run.total_mailboxes > 0 ? run.total_mailboxes : 1;
      progress.value = Math.min(run.processed_mailboxes, progress.max);
      article.appendChild(progress);

      const meta = document.createElement("div");
      meta.className = "cleanup-progress-meta";
      addText(
        meta,
        "span",
        run.total_mailboxes > 0
          ? `Проверено ${run.processed_mailboxes} из ${run.total_mailboxes}`
          : "Подготовка списка ящиков…"
      );
      addText(
        meta,
        "span",
        `Найдено: ${run.found_messages} · Удалено: ${run.deleted_messages} · Ошибок: ${run.error_count}`
      );
      article.appendChild(meta);
      runList.appendChild(article);
    });
  };

  const renderStarting = () => {
    isActive = true;
    hadActiveRun = true;
    panel.hidden = false;
    title.textContent = "Запуск поставлен в очередь";
    note.textContent = "Подготавливаем список почтовых ящиков…";
    runList.replaceChildren();
    setButtonsDisabled(true);
  };

  startForms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      if (isActive) {
        event.preventDefault();
        return;
      }
      renderStarting();
    });
  });

  const refresh = async () => {
    try {
      const response = await fetch(panel.dataset.progressUrl, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const data = await response.json();
      if (data.active) {
        isActive = true;
        hadActiveRun = true;
        panel.hidden = false;
        const cleanupRuns = data.runs.some((run) => run.mode !== "dry_run");
        title.textContent = cleanupRuns ? "Очистка выполняется" : "Проверка выполняется";
        note.textContent = "Страница обновляет состояние автоматически.";
        renderRuns(data.runs);
        setButtonsDisabled(true);
        return;
      }
      isActive = false;
      setButtonsDisabled(false);
      if (hadActiveRun) window.location.reload();
      else panel.hidden = true;
    } catch (_error) {
      note.textContent = "Не удалось обновить состояние. Повторим автоматически.";
    }
  };

  setButtonsDisabled(isActive);
  window.setInterval(refresh, 2000);
  refresh();
})();
