(() => {
  const panel = document.getElementById("cleanup-live-progress");
  if (!panel) return;

  const title = panel.querySelector("[data-cleanup-progress-title]");
  const note = panel.querySelector("[data-cleanup-progress-note]");
  const runList = panel.querySelector("[data-cleanup-progress-runs]");
  const schedulerStatus = document.querySelector("[data-cleanup-scheduler-status]");
  const schedulerLastCheck = document.querySelector("[data-cleanup-scheduler-last-check]");
  const schedulerNextRun = document.querySelector("[data-cleanup-scheduler-next-run]");
  const schedulerRules = document.querySelector("[data-cleanup-scheduler-rules]");
  const schedulerTimezone = document.querySelector("[data-cleanup-scheduler-timezone]");
  const schedulerMessage = document.querySelector("[data-cleanup-scheduler-message]");
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

  const ruleCountLabel = (count) => {
    const lastTwo = count % 100;
    const last = count % 10;
    if (lastTwo >= 11 && lastTwo <= 14) return `${count} правил`;
    if (last === 1) return `${count} правило`;
    if (last >= 2 && last <= 4) return `${count} правила`;
    return `${count} правил`;
  };

  const renderRuns = (summary, runs) => {
    runList.replaceChildren();

    const overall = document.createElement("article");
    overall.className = "cleanup-progress-run cleanup-progress-overall";

    const overallCaption = document.createElement("div");
    overallCaption.className = "cleanup-progress-caption";
    addText(overallCaption, "strong", "Общий проход по почтовым ящикам");
    addText(overallCaption, "span", ruleCountLabel(runs.length));
    overall.appendChild(overallCaption);

    const progress = document.createElement("progress");
    progress.max = summary.total_mailboxes > 0 ? summary.total_mailboxes : 1;
    progress.value = Math.min(summary.processed_mailboxes, progress.max);
    overall.appendChild(progress);

    const overallMeta = document.createElement("div");
    overallMeta.className = "cleanup-progress-meta";
    addText(
      overallMeta,
      "span",
      summary.total_mailboxes > 0
        ? `Проверено ${summary.processed_mailboxes} из ${summary.total_mailboxes}`
        : "Подготовка списка ящиков…"
    );
    addText(
      overallMeta,
      "span",
      "Один проход по ящикам для всех правил"
    );
    overall.appendChild(overallMeta);
    runList.appendChild(overall);

    const ruleStats = document.createElement("div");
    ruleStats.className = "cleanup-progress-rule-stats";
    runs.forEach((run) => {
      const article = document.createElement("article");
      article.className = "cleanup-progress-rule-stat";

      const caption = document.createElement("div");
      caption.className = "cleanup-progress-caption";
      addText(caption, "strong", run.rule_name);
      addText(caption, "span", `${run.mode_label} · ${run.status_label}`);
      article.appendChild(caption);

      const values = document.createElement("span");
      values.className = "cleanup-progress-rule-values";
      values.textContent = run.mode === "dry_run"
        ? `Под удаление: ${run.found_messages} · Ошибок: ${run.error_count}`
        : `Найдено: ${run.found_messages} · Команда удаления: ${run.deleted_messages} · Осталось: ${run.remaining_messages} · Ошибок: ${run.error_count}`;
      article.appendChild(values);
      ruleStats.appendChild(article);
    });
    runList.appendChild(ruleStats);
  };

  const renderScheduler = (scheduler) => {
    if (!scheduler || !schedulerStatus) return;
    schedulerStatus.textContent = scheduler.status_label;
    schedulerStatus.classList.toggle(
      "active",
      scheduler.status === "running" || scheduler.status === "completed"
    );
    schedulerLastCheck.textContent = scheduler.last_check || "—";
    schedulerNextRun.textContent = scheduler.next_run || "—";
    schedulerRules.textContent = scheduler.automatic_rule_count;
    schedulerTimezone.textContent = scheduler.timezone;
    schedulerMessage.textContent = scheduler.message
      || "Планировщик ещё не сообщил состояние.";
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
      renderScheduler(data.scheduler);
      if (data.active) {
        isActive = true;
        hadActiveRun = true;
        panel.hidden = false;
        const cleanupRuns = data.runs.some((run) => run.mode !== "dry_run");
        title.textContent = cleanupRuns ? "Очистка выполняется" : "Проверка выполняется";
        note.textContent = "Страница обновляет состояние автоматически.";
        renderRuns(data.summary, data.runs);
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
