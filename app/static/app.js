(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    tzFile: null,
    stdFiles: [],
    libraryFiles: [],
    libraryDocs: [],
    selectedStandards: new Set(),
    templates: [],
    runId: null,
    lastRun: null,
    results: [],
    filters: { status: "ALL", category: "", q: "" },
    pollTimer: null,
    estimateTimer: null,
    token: sessionStorage.getItem("tz-token") || "",
    sheetsToken: null,
    tokenClient: null,
    savedSheetRunId: null,
    apiOk: true,
  };

  const STATUS_NAMES = {
    COMPLIES: "Соответствует",
    NONCOMPLIANCE: "Несоответствие",
    MISSING: "Не найдено в ТЗ",
    EXPERT_REVIEW: "Экспертная оценка",
    INSUFFICIENT_DATA: "Недостаточно данных",
    SOURCE_CONFLICT: "Конфликт источников",
    NOT_APPLICABLE: "Не применимо",
  };
  const SEVERITY_NAMES = { critical: "Критично", major: "Существенно", minor: "Незначительно", info: "Информация" };
  const RUN_STATUS_NAMES = {
    QUEUED: "В очереди",
    RUNNING: "Выполняется",
    COMPLETED: "Завершён",
    COMPLETED_WITH_REVIEW: "Завершён (есть замечания)",
    PARTIAL: "Частично",
    FAILED: "Ошибка",
    CANCELLED: "Остановлен",
  };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function fmt(value) {
    const number = Number(value || 0);
    return number.toLocaleString("ru-RU");
  }

  function notify(id, text, type) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("hidden", "good", "bad");
    if (type) el.classList.add(type);
  }

  function withToken(url) {
    if (!state.token) return url;
    return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(state.token);
  }

  async function api(url, options) {
    const opts = Object.assign({}, options || {});
    const headers = new Headers(opts.headers || {});
    if (state.token) headers.set("X-API-Token", state.token);
    opts.headers = headers;
    const response = await fetch(url, opts);
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json().catch(() => ({})) : await response.text();
    if (!response.ok) {
      let detail = data && data.detail ? data.detail : "HTTP " + response.status;
      if (response.status === 405) {
        detail = "Сервер не поддерживает этот метод (HTTP 405). Похоже, запущена старая версия кода — перезапустите start-local.bat.";
      } else if (response.status === 404 && url.indexOf("/progress") >= 0) {
        detail = "Сервер не поддерживает прогресс проверки (HTTP 404). Перезапустите start-local.bat, чтобы загрузить новую версию.";
      }
      throw new Error(detail);
    }
    return data;
  }

  async function checkServer() {
    const warning = $("server-warning");
    try {
      const health = await fetch("/health", { cache: "no-store" }).then((response) => response.json());
      const features = Array.isArray(health.features) ? health.features : [];
      const required = ["estimate", "background_runs", "progress", "compare", "templates"];
      const missing = required.filter((feature) => !features.includes(feature));
      state.apiOk = health.api_version === 3 && missing.length === 0;
      if (!state.apiOk) {
        warning.classList.remove("hidden");
        warning.innerHTML =
          "<b>Сервер запущен старой версией кода.</b><br>" +
          "Интерфейс новый, но API не поддерживает: " + esc((missing.length ? missing : ["api_version 3"]).join(", ")) + ". " +
          "Шаблоны, оценка стоимости, <b>прогресс проверки</b> и сравнение прогонов будут давать ошибки.<br>" +
          "Закройте окно сервера (Ctrl+C) и снова запустите <code>start-local.bat</code>, затем обновите страницу.";
      } else {
        warning.classList.add("hidden");
      }
    } catch (error) {
      state.apiOk = false;
      warning.classList.remove("hidden");
      warning.innerHTML = "<b>Сервер недоступен.</b> Запустите <code>start-local.bat</code> и обновите страницу.";
    }
  }

  /* ------------------------------ навигация ------------------------------ */

  const titles = {
    check: "Проверить требования ТЗ",
    library: "Библиотека СТО",
    history: "История проверок",
    settings: "Настройки ИИ",
    help: "Инструкция",
  };

  function activatePage(page) {
    document.querySelectorAll(".page").forEach((el) => el.classList.toggle("active", el.id === "page-" + page));
    document.querySelectorAll(".nav-btn").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
    $("page-title").textContent = titles[page] || "";
  }

  function go(page) {
    activatePage(page);
    if (page === "library") loadLibrary();
    if (page === "history") loadHistory();
    if (page === "settings") loadSettings();
  }

  document.querySelectorAll(".nav-btn").forEach((button) => {
    button.addEventListener("click", () => go(button.dataset.page));
  });
  $("header-settings").onclick = () => go("settings");

  /* ------------------------------ проверка ------------------------------- */

  function setFileLabel(input, target) {
    const files = Array.from(input.files || []);
    if (!files.length) {
      target.textContent = input.multiple ? "Файлы не выбраны" : "Файл не выбран";
      return;
    }
    target.textContent = files.map((file) => file.name + " (" + Math.ceil(file.size / 1024) + " КБ)").join("\n");
  }

  $("tz-file").addEventListener("change", (event) => {
    state.tzFile = event.target.files[0] || null;
    setFileLabel($("tz-file"), $("tz-file-name"));
    scheduleEstimate();
  });
  $("std-files").addEventListener("change", (event) => {
    state.stdFiles = Array.from(event.target.files || []);
    setFileLabel($("std-files"), $("std-file-names"));
  });
  $("library-files").addEventListener("change", (event) => {
    state.libraryFiles = Array.from(event.target.files || []);
    setFileLabel($("library-files"), $("library-file-names"));
  });
  $("tz-url").addEventListener("input", scheduleEstimate);
  $("analysis-mode").addEventListener("change", scheduleEstimate);
  $("project-context").addEventListener("input", scheduleEstimate);

  function scheduleEstimate() {
    clearTimeout(state.estimateTimer);
    state.estimateTimer = setTimeout(runEstimate, 500);
  }

  async function runEstimate() {
    const box = $("estimate-box");
    if (!state.apiOk) {
      box.textContent = "Сервер запущен старой версией. Перезапустите start-local.bat и обновите страницу.";
      return;
    }
    const tzUrl = $("tz-url").value.trim();
    if (!state.tzFile && !tzUrl) {
      box.textContent = "Загрузите ТЗ, чтобы увидеть оценку объёма и стоимости проверки.";
      return;
    }
    const form = new FormData();
    if (state.tzFile) form.append("tz_file", state.tzFile, state.tzFile.name);
    form.append("tz_url", tzUrl);
    form.append("analysis_mode", $("analysis-mode").value);
    form.append("context", $("project-context").value.trim());
    form.append("standard_ids", JSON.stringify(selectedScope()));
    box.innerHTML = '<span class="spinner"></span> Считаю объём…';
    try {
      const data = await api("/api/estimate", { method: "POST", body: form });
      const cost = Number(data.estimated_cost || 0);
      const costText = cost > 0 ? " · ≈ $" + cost.toFixed(3) : "";
      box.innerHTML =
        "<b>" + data.llm_batches + "</b> пакетов · фрагментов ТЗ: <b>" + data.tz_chunks_analyzed +
        "</b> из " + data.tz_chunks_total + " · токенов ≈ <b>" + fmt(data.estimated_total_tokens) +
        "</b>" + costText + "<br><span class='small'>" + esc(data.note || "") + "</span>";
    } catch (error) {
      box.textContent = "Оценка недоступна: " + (/405|404/.test(error.message)
        ? "сервер старой версии — перезапустите start-local.bat."
        : error.message);
    }
  }

  $("run-check").onclick = async () => {
    if (!state.apiOk) {
      notify("check-message", "Сервер запущен старой версией кода. Закройте окно сервера (Ctrl+C), снова запустите start-local.bat и обновите страницу.", "bad");
      return;
    }
    const tzUrl = $("tz-url").value.trim();
    if (!state.tzFile && !tzUrl) {
      notify("check-message", "Добавьте файл ТЗ или ссылку.", "bad");
      return;
    }
    if (state.tzFile && tzUrl) {
      notify("check-message", "Укажите либо файл ТЗ, либо ссылку, но не оба варианта.", "bad");
      return;
    }
    const button = $("run-check");
    button.disabled = true;
    const startedAt = Date.now();
    button.textContent = "Запускаю… 0 с";
    const ticker = setInterval(() => {
      button.textContent = "Запускаю… " + Math.round((Date.now() - startedAt) / 1000) + " с";
    }, 1000);
    notify("check-message", "Создаю фоновую проверку…");
    const form = new FormData();
    if (state.tzFile) form.append("tz_file", state.tzFile, state.tzFile.name);
    form.append("tz_url", tzUrl);
    state.stdFiles.forEach((file) => form.append("standard_files", file, file.name));
    form.append("standard_urls", $("std-urls").value);
    form.append("context", $("project-context").value.trim());
    form.append("instructions", $("analysis-instructions").value.trim());
    form.append("title", $("run-title").value.trim());
    form.append("analysis_mode", $("analysis-mode").value);
    form.append("standard_ids", JSON.stringify(selectedScope()));
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90000);
    let startedOk = false;
    try {
      const data = await api("/api/analyze", { method: "POST", body: form, signal: controller.signal });
      startedOk = true;
      state.runId = data.run_id;
      state.lastRun = null;
      state.results = [];
      state.filters = { status: "ALL", category: "", q: "" };
      $("results-search").value = "";
      $("category-filter").value = "";
      $("results").className = "empty";
      $("results").textContent = "Анализ выполняется в фоне…";
      $("results-filters").classList.add("hidden");
      $("export-buttons").classList.add("hidden");
      $("progress-panel").classList.remove("hidden");
      if (data.warnings && data.warnings.length) notify("check-message", data.warnings.join("\n"));
      else notify("check-message", "Проверка запущена. ID: " + data.run_id);
      pollProgress();
    } catch (error) {
      const message = error.name === "AbortError"
        ? "Сервер не ответил за 90 секунд. Вероятно, запущена старая версия или проверка выполняется синхронно. Перезапустите start-local.bat."
        : error.message;
      notify("check-message", "Проверка не запущена: " + message, "bad");
      $("results").className = "empty";
      $("results").textContent = "Нет результатов. Исправьте ошибку и повторите.";
    } finally {
      clearInterval(ticker);
      clearTimeout(timeout);
      if (!startedOk) {
        button.disabled = false;
        button.textContent = "Проверить требования ТЗ";
      }
    }
  };

  $("cancel-run").onclick = async () => {
    if (!state.runId) return;
    if (!confirm("Остановить проверку после текущих пакетов?")) return;
    try {
      await api("/api/runs/" + encodeURIComponent(state.runId) + "/cancel", { method: "POST" });
      notify("check-message", "Останавливаю после текущих пакетов…");
    } catch (error) {
      notify("check-message", error.message, "bad");
    }
  };

  function showProgress(data) {
    const progress = data.progress || {};
    const total = Number(progress.batches_total || 0);
    const done = Number(progress.batches_done || 0);
    let percent = total ? Math.round((done / total) * 100) : 0;
    if (progress.phase === "indexing") {
      const embTotal = Number(progress.embedding_total || 0);
      const embDone = Number(progress.embedding_done || 0);
      percent = embTotal ? Math.min(15, Math.round((embDone / embTotal) * 15)) : 2;
    }
    if (progress.phase === "done") percent = 100;
    $("progress-panel").classList.remove("hidden");
    $("progress-fill").style.width = percent + "%";
    $("progress-percent").textContent = percent + "%";
    $("progress-text").innerHTML =
      '<span class="spinner"></span>' + esc(progress.message || "Выполняется…");
    const tokens = progress.tokens || {};
    $("progress-meta").textContent =
      "Пакетов: " + done + " из " + total +
      " · ошибок: " + (progress.batches_failed || 0) +
      " · результатов: " + (progress.results_count || data.result_count || 0) +
      " · токенов: " + fmt(tokens.total_tokens || 0) +
      " · прошло: " + (progress.elapsed_sec || 0) + " c";
  }

  async function pollProgress() {
    if (!state.runId) return;
    try {
      const data = await api("/api/runs/" + encodeURIComponent(state.runId) + "/progress");
      showProgress(data);
      if (!data.running && !["QUEUED", "RUNNING"].includes(data.status)) {
        clearTimeout(state.pollTimer);
        state.pollTimer = null;
        await finishRun(data.status);
        return;
      }
    } catch (error) {
      notify("check-message", "Не удалось получить прогресс: " + error.message, "bad");
      clearTimeout(state.pollTimer);
      $("run-check").disabled = false;
      $("run-check").textContent = "Проверить требования ТЗ";
      return;
    }
    state.pollTimer = setTimeout(pollProgress, 1200);
  }

  async function finishRun(status) {
    try {
      const run = await api("/api/runs/" + encodeURIComponent(state.runId));
      state.lastRun = run;
      state.savedSheetRunId = null;
      renderRun(run);
      const messages = [];
      if (run.warnings && run.warnings.length) messages.push(run.warnings.join("\n"));
      const label = RUN_STATUS_NAMES[status] || status;
      notify(
        "check-message",
        "Проверка: " + label + ". ID: " + run.run_id + ". Результатов: " + run.result_count + "." +
          (messages.length ? "\n" + messages.join("\n") : ""),
        status === "COMPLETED" ? "good" : status === "FAILED" ? "bad" : ""
      );
      $("export-buttons").classList.remove("hidden");
      $("save-sheets").disabled = !state.sheetsToken;
      $("run-check").disabled = false;
      $("run-check").textContent = "Проверить требования ТЗ";
    } catch (error) {
      notify("check-message", "Не удалось загрузить результат: " + error.message, "bad");
    }
  }

  $("clear-form").onclick = () => {
    if (!confirm("Очистить поля формы?")) return;
    ["tz-file", "tz-url", "run-title", "std-files", "std-urls", "project-context", "analysis-instructions"].forEach((id) => {
      $(id).value = "";
    });
    state.tzFile = null;
    state.stdFiles = [];
    $("tz-file-name").textContent = "Файл не выбран";
    $("std-file-names").textContent = "Файлы не выбраны";
    $("estimate-box").textContent = "Загрузите ТЗ, чтобы увидеть оценку объёма и стоимости проверки.";
  };

  /* --------------------------- отрисовка результатов --------------------- */

  function sourceCell(name, url, locator, quote) {
    const label = esc(name || "Источник");
    const linked = url && /^https:\/\//i.test(url)
      ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + label + "</a>"
      : label;
    const quoteClass = (quote || "").length > 220 ? " clamp" : "";
    return (
      "<div class='evidence'><b>" + linked + "</b>" +
      "<div class='small'>" + esc(locator || "Пункт/страница не определены") + "</div>" +
      "<div class='evidence-quote" + quoteClass + "'>" + esc(quote || "Цитата не представлена") + "</div>" +
      "</div>"
    );
  }

  function statusBadge(result) {
    const color = result.color || "yellow";
    return "<span class='badge " + esc(color) + "'>" + esc(STATUS_NAMES[result.status] || result.status) + "</span>";
  }

  function severityBadge(result) {
    if (!result.severity) return "";
    return "<span class='badge sev-" + esc(result.severity) + "'>" + esc(SEVERITY_NAMES[result.severity] || result.severity) + "</span>";
  }

  function renderRun(run) {
    state.results = run.results || [];
    state.lastRun = run;
    renderMetrics();
    renderFilters();
    renderResults();
    $("results-filters").classList.remove("hidden");
  }

  function renderMetrics() {
    const counts = { green: 0, red: 0, yellow: 0, gray: 0 };
    state.results.forEach((item) => { counts[item.color || "yellow"] = (counts[item.color || "yellow"] || 0) + 1; });
    const box = $("result-metrics");
    box.classList.remove("hidden");
    box.innerHTML =
      metric("Соответствует", counts.green, "#067647") +
      metric("Несоответствие", counts.red, "#b42318") +
      metric("Экспертная оценка", counts.yellow, "#93370d") +
      metric("Не применимо", counts.gray, "#475467");
  }

  function metric(label, value, color) {
    return "<div class='metric'><div class='metric-label'>" + label + "</div>" +
      "<div class='metric-value' style='color:" + color + "'>" + value + "</div></div>";
  }

  function statusCounts() {
    const counts = {};
    state.results.forEach((item) => { counts[item.status] = (counts[item.status] || 0) + 1; });
    return counts;
  }

  function renderFilters() {
    const counts = statusCounts();
    const chips = ['<button class="chip' + (state.filters.status === "ALL" ? " active" : "") + '" data-status="ALL">Все<span class="count">' + state.results.length + "</span></button>"];
    Object.keys(STATUS_NAMES).forEach((status) => {
      if (!counts[status]) return;
      chips.push(
        '<button class="chip' + (state.filters.status === status ? " active" : "") + '" data-status="' + status + '">' +
        STATUS_NAMES[status] + '<span class="count">' + counts[status] + "</span></button>"
      );
    });
    $("status-filters").innerHTML = chips.join("");

    const categories = Array.from(new Set(state.results.map((item) => item.category).filter(Boolean))).sort();
    $("category-filter").innerHTML =
      '<option value="">Все категории</option>' +
      categories.map((category) => '<option value="' + esc(category) + '"' + (state.filters.category === category ? " selected" : "") + ">" + esc(category) + "</option>").join("");
  }

  function filteredResults() {
    const query = state.filters.q.toLowerCase().trim();
    return state.results.filter((item) => {
      if (state.filters.status !== "ALL" && item.status !== state.filters.status) return false;
      if (state.filters.category && item.category !== state.filters.category) return false;
      if (!query) return true;
      return [
        item.category, item.section, item.parameter, item.tz_quote, item.standard_quote,
        item.explanation, item.recommendation, item.semantic_reasoning,
      ].join(" ").toLowerCase().includes(query);
    });
  }

  function renderResults() {
    const rows = filteredResults();
    const container = $("results");
    if (!rows.length) {
      container.className = "empty";
      container.textContent = state.results.length ? "По фильтру ничего не найдено." : "Модель не вернула результатов. Это не означает, что ТЗ соответствует СТО.";
      return;
    }
    let html = '<div class="tablewrap"><table><thead><tr>' +
      "<th>Статус</th><th>Раздел / параметр</th><th>Фрагмент ТЗ</th><th>Фрагмент СТО</th>" +
      "<th>Сравнение</th><th>Оценка и рекомендация</th></tr></thead><tbody>";
    rows.forEach((result) => {
      const comparison = [
        esc(result.operator || ""),
        esc(result.expected_value == null ? "" : result.expected_value),
        esc(result.expected_unit || ""),
        " / ",
        esc(result.actual_value == null ? "" : result.actual_value),
        esc(result.actual_unit || ""),
      ].join(" ");
      const notes = [];
      if (result.semantic_reasoning) notes.push("<p><b>Сопоставление:</b> " + esc(result.semantic_reasoning) + "</p>");
      if (result.applicability_assessment) notes.push("<p><b>Применимость:</b> " + esc(result.applicability_assessment) + "</p>");
      if (result.technical_assessment) notes.push("<p><b>Техническая оценка:</b> " + esc(result.technical_assessment) + "</p>");
      if (result.recommendation) notes.push("<p><b>Рекомендация:</b> " + esc(result.recommendation) + "</p>");
      if (result.conversion_note) notes.push("<p class='small'>" + esc(result.conversion_note) + "</p>");
      if (result.review_note) notes.push("<p class='small'>" + esc(result.review_note) + "</p>");
      html += "<tr>" +
        "<td>" + statusBadge(result) + "<div class='small'>" + (result.evidence_verified ? "цитаты подтверждены" : "цитаты не подтверждены") + "</div><div style='margin-top:4px'>" + severityBadge(result) + "</div><div class='small'>уверенность: " + esc(result.confidence == null ? "—" : result.confidence) + "</div></td>" +
        "<td><b>" + esc(result.category) + "</b><div>" + esc(result.parameter) + "</div>" +
        (result.section ? "<div class='small'>" + esc(result.section) + "</div>" : "") +
        (result.finding_type ? "<div class='small'>" + esc(result.finding_type) + "</div>" : "") + "</td>" +
        "<td>" + sourceCell(result.tz_filename, result.tz_url, result.tz_locator, result.tz_quote) + "</td>" +
        "<td>" + sourceCell(result.standard_filename, result.standard_url, result.standard_locator, result.standard_quote) + "</td>" +
        "<td>" + comparison + "</td>" +
        "<td>" + esc(result.explanation || "") + notes.join("") + "</td>" +
        "</tr>";
    });
    html += "</tbody></table></div>";
    container.className = "";
    container.innerHTML = html;
  }

  $("status-filters").addEventListener("click", (event) => {
    const chip = event.target.closest("[data-status]");
    if (!chip) return;
    state.filters.status = chip.dataset.status;
    renderFilters();
    renderResults();
  });
  $("category-filter").addEventListener("change", (event) => {
    state.filters.category = event.target.value;
    renderResults();
  });
  $("results-search").addEventListener("input", (event) => {
    state.filters.q = event.target.value;
    renderResults();
  });
  $("results").addEventListener("click", (event) => {
    const quote = event.target.closest(".evidence-quote.clamp");
    if (quote) quote.classList.toggle("open");
  });

  $("export-xlsx").onclick = () => {
    if (state.runId) window.location.href = withToken("/api/runs/" + encodeURIComponent(state.runId) + "/export.xlsx");
  };
  $("export-json").onclick = () => {
    if (state.runId) window.location.href = withToken("/api/runs/" + encodeURIComponent(state.runId) + "/export.json");
  };

  /* ------------------------------ библиотека ---------------------------- */

  function selectedScope() {
    const select = $("standard-scope");
    if (!select) return [];
    return Array.from(select.selectedOptions || []).map((option) => option.value);
  }

  async function loadStandardScope() {
    try {
      const data = await api("/api/library/standards");
      const current = new Set(selectedScope());
      const docs = (data.documents || [])
        .filter((doc) => doc.status !== "RETIRED")
        .sort((left, right) => {
          const statusOrder = (doc) => (doc.status === "ACTIVE" ? 0 : 1);
          return statusOrder(left) - statusOrder(right) || String(left.filename).localeCompare(String(right.filename));
        });
      $("standard-scope").innerHTML = docs.map((doc) => {
        const prefix = doc.status === "ACTIVE" ? "" : "[" + doc.status + "] ";
        return "<option value='" + esc(doc.source_id) + "'" + (current.has(doc.source_id) ? " selected" : "") + ">" +
          esc(prefix + doc.filename + (doc.revision ? " · " + doc.revision : "")) + "</option>";
      }).join("");
    } catch (error) {
      /* ограничение области не критично: анализ пойдёт по всей библиотеке */
    }
  }

  async function loadLibrary() {
    try {
      const data = await api("/api/library/standards");
      state.libraryDocs = data.documents || [];
      const stats = data.stats || {};
      $("lib-doc-count").textContent = stats.documents || 0;
      $("lib-active-count").textContent = stats.active || 0;
      $("lib-chunk-count").textContent = fmt(stats.chunks || 0);
      $("lib-char-count").textContent = fmt(stats.characters || 0);
      renderLibraryTable();
      await loadStandardScope();
    } catch (error) {
      $("library-table").className = "notice bad";
      $("library-table").textContent = "Не удалось загрузить библиотеку: " + error.message;
    }
  }

  function renderLibraryTable() {
    const query = $("library-search").value.toLowerCase().trim();
    const docs = state.libraryDocs.filter((doc) =>
      [doc.filename, doc.source_id, doc.status, doc.revision, doc.applicability].join(" ").toLowerCase().includes(query)
    );
    if (!docs.length) {
      $("library-table").className = "empty";
      $("library-table").textContent = state.libraryDocs.length ? "По фильтру ничего не найдено." : "Библиотека пуста. Добавьте СТО выше.";
      return;
    }
    let html = '<div class="tablewrap"><table><thead><tr>' +
      '<th><input type="checkbox" id="table-select-all"></th><th>Документ</th><th>Редакция</th><th>Статус</th>' +
      "<th>Фрагментов</th><th>Область применения</th><th>Действия</th></tr></thead><tbody>";
    docs.forEach((doc) => {
      const checked = state.selectedStandards.has(doc.source_id) ? " checked" : "";
      html += "<tr>" +
        "<td><input type='checkbox' class='row-check' data-select='" + esc(doc.source_id) + "'" + checked + "></td>" +
        "<td><b>" + esc(doc.filename) + "</b><div class='small'>" + esc(doc.source_id) + "</div>" +
        (doc.source_url ? '<div><a href="' + esc(doc.source_url) + '" target="_blank" rel="noopener">Источник</a></div>' : "") +
        "<div class='small'>Импорт: " + esc((doc.imported_at || "").slice(0, 10)) + "</div></td>" +
        "<td><input data-revision='" + esc(doc.source_id) + "' value='" + esc(doc.revision || "") + "' placeholder='Редакция / дата' style='min-width:130px'></td>" +
        "<td><select data-status='" + esc(doc.source_id) + "'>" +
          '<option value="PENDING_REVIEW"' + (doc.status === "PENDING_REVIEW" ? " selected" : "") + ">Требует проверки</option>" +
          '<option value="ACTIVE"' + (doc.status === "ACTIVE" ? " selected" : "") + ">ACTIVE — утверждён</option>" +
          '<option value="DRAFT"' + (doc.status === "DRAFT" ? " selected" : "") + ">Черновик</option>" +
          '<option value="RETIRED"' + (doc.status === "RETIRED" ? " selected" : "") + ">Утратил действие</option>" +
        "</select></td>" +
        "<td>" + esc(doc.chunk_count) + "</td>" +
        "<td><input data-applicability='" + esc(doc.source_id) + "' value='" + esc(doc.applicability || "") + "' placeholder='Область применения' style='min-width:190px'></td>" +
        "<td><button class='btn small' data-save='" + esc(doc.source_id) + "'>Сохранить</button> " +
        "<button class='btn small danger' data-delete='" + esc(doc.source_id) + "'>Удалить</button></td>" +
        "</tr>";
    });
    html += "</tbody></table></div>";
    $("library-table").className = "";
    $("library-table").innerHTML = html;

    $("library-table").querySelectorAll("[data-save]").forEach((button) => {
      button.onclick = () => saveStandard(button.dataset.save);
    });
    $("library-table").querySelectorAll("[data-delete]").forEach((button) => {
      button.onclick = () => deleteStandard(button.dataset.delete);
    });
    $("library-table").querySelectorAll("[data-select]").forEach((box) => {
      box.onchange = () => {
        if (box.checked) state.selectedStandards.add(box.dataset.select);
        else state.selectedStandards.delete(box.dataset.select);
      };
    });
    const selectAll = $("table-select-all");
    if (selectAll) {
      selectAll.onchange = () => {
        docs.forEach((doc) => {
          if (selectAll.checked) state.selectedStandards.add(doc.source_id);
          else state.selectedStandards.delete(doc.source_id);
        });
        renderLibraryTable();
      };
    }
  }

  async function saveStandard(sourceId) {
    const table = $("library-table");
    const status = table.querySelector('[data-status="' + CSS.escape(sourceId) + '"]').value;
    const revision = table.querySelector('[data-revision="' + CSS.escape(sourceId) + '"]').value;
    const applicability = table.querySelector('[data-applicability="' + CSS.escape(sourceId) + '"]').value;
    try {
      await api("/api/library/standards/" + encodeURIComponent(sourceId), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status, revision, applicability }),
      });
      notify("library-message", "Сохранено: " + sourceId + " · " + status, "good");
      await loadLibrary();
    } catch (error) {
      notify("library-message", error.message, "bad");
    }
  }

  async function deleteStandard(sourceId) {
    if (!confirm("Удалить СТО " + sourceId + " из библиотеки?")) return;
    try {
      await api("/api/library/standards/" + encodeURIComponent(sourceId), { method: "DELETE" });
      state.selectedStandards.delete(sourceId);
      notify("library-message", "Документ удалён из библиотеки.", "good");
      await loadLibrary();
    } catch (error) {
      notify("library-message", error.message, "bad");
    }
  }

  $("select-all").onchange = (event) => {
    if (event.target.checked) state.libraryDocs.forEach((doc) => state.selectedStandards.add(doc.source_id));
    else state.selectedStandards.clear();
    renderLibraryTable();
  };

  $("bulk-apply").onclick = async () => {
    const ids = Array.from(state.selectedStandards);
    if (!ids.length) {
      notify("library-message", "Отметьте документы в таблице.", "bad");
      return;
    }
    const payload = { source_ids: ids };
    const status = $("bulk-status").value;
    if (status) payload.status = status;
    if ($("bulk-revision").value.trim()) payload.revision = $("bulk-revision").value.trim();
    if ($("bulk-applicability").value.trim()) payload.applicability = $("bulk-applicability").value.trim();
    if (Object.keys(payload).length === 1) {
      notify("library-message", "Укажите статус, редакцию или область применения.", "bad");
      return;
    }
    try {
      const result = await api("/api/library/standards", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      notify("library-message", "Обновлено документов: " + result.changed + ".", "good");
      state.selectedStandards.clear();
      await loadLibrary();
    } catch (error) {
      notify("library-message", error.message, "bad");
    }
  };

  $("bulk-delete").onclick = async () => {
    const ids = Array.from(state.selectedStandards);
    if (!ids.length) {
      notify("library-message", "Отметьте документы в таблице.", "bad");
      return;
    }
    if (!confirm("Удалить выбранные документы (" + ids.length + ")? Действие необратимо.")) return;
    let removed = 0;
    for (const id of ids) {
      try {
        await api("/api/library/standards/" + encodeURIComponent(id), { method: "DELETE" });
        removed += 1;
      } catch (error) {
        notify("library-message", "Ошибка удаления " + id + ": " + error.message, "bad");
      }
    }
    state.selectedStandards.clear();
    notify("library-message", "Удалено документов: " + removed + ".", "good");
    await loadLibrary();
  };

  $("import-library").onclick = async () => {
    const urls = $("library-urls").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!state.libraryFiles.length && !urls.length) {
      notify("library-message", "Выберите файлы СТО или вставьте ссылки.", "bad");
      return;
    }
    const button = $("import-library");
    button.disabled = true;
    button.textContent = "Импортирую…";
    notify("library-message", "Извлекаю текст и строю индекс. Для большой партии это может занять несколько минут.");
    try {
      const form = new FormData();
      state.libraryFiles.forEach((file) => form.append("standard_files", file, file.name));
      form.append("standard_urls", urls.join("\n"));
      const result = await api("/api/library/import", { method: "POST", body: form });
      const errors = (result.items || []).filter((item) => item.status === "ERROR");
      let message = "Импорт завершён. Новых: " + result.imported + "; дубликатов: " + result.duplicates + "; ошибок: " + result.errors + ".";
      if (errors.length) message += "\n" + errors.map((item) => item.input + ": " + item.error).join("\n");
      notify("library-message", message, errors.length ? "bad" : "good");
      $("library-files").value = "";
      $("library-urls").value = "";
      state.libraryFiles = [];
      $("library-file-names").textContent = "Файлы не выбраны";
      await loadLibrary();
    } catch (error) {
      notify("library-message", "Не удалось импортировать: " + error.message, "bad");
    } finally {
      button.disabled = false;
      button.textContent = "Импортировать в библиотеку";
    }
  };

  $("refresh-library").onclick = loadLibrary;
  $("refresh-library-2").onclick = loadLibrary;
  $("library-search").addEventListener("input", renderLibraryTable);

  /* ------------------------------- история ------------------------------ */

  async function loadHistory() {
    try {
      const rows = await api("/api/runs");
      if (!rows.length) {
        $("history-table").className = "empty";
        $("history-table").textContent = "История пока пуста. Выполните первую проверку.";
        return;
      }
      let html = '<div class="tablewrap"><table><thead><tr>' +
        "<th>Дата</th><th>Название</th><th>Файл ТЗ</th><th>Статус</th><th>Результатов</th><th>Действие</th></tr></thead><tbody>";
      rows.forEach((run) => {
        html += "<tr><td>" + esc(new Date(run.created_at).toLocaleString("ru-RU")) + "</td>" +
          "<td>" + esc(run.title) + "</td><td>" + esc(run.tz_filename) + "</td>" +
          "<td>" + esc(RUN_STATUS_NAMES[run.status] || run.status) + "</td>" +
          "<td>" + esc(run.result_count) + "</td>" +
          "<td><button class='btn small' data-run='" + esc(run.run_id) + "'>Открыть</button> " +
          "<a class='btn small' href='" + withToken("/api/runs/" + encodeURIComponent(run.run_id) + "/export.xlsx") + "'>Excel</a></td></tr>";
      });
      html += "</tbody></table></div>";
      $("history-table").className = "";
      $("history-table").innerHTML = html;
      $("history-table").querySelectorAll("[data-run]").forEach((button) => {
        button.onclick = () => openRun(button.dataset.run);
      });
      const options = rows.map((run) =>
        "<option value='" + esc(run.run_id) + "'>" + esc(new Date(run.created_at).toLocaleString("ru-RU")) + " · " + esc(run.title) + "</option>"
      ).join("");
      $("compare-base").innerHTML = "<option value=''>Базовый (старый) прогон…</option>" + options;
      $("compare-other").innerHTML = "<option value=''>Новый прогон…</option>" + options;
    } catch (error) {
      $("history-table").className = "notice bad";
      $("history-table").textContent = error.message;
    }
  }

  async function openRun(runId) {
    try {
      const run = await api("/api/runs/" + encodeURIComponent(runId));
      state.runId = runId;
      renderRun(run);
      $("history-detail").innerHTML =
        "<h3>Результаты: " + esc(run.title) + " · " + esc(run.run_id) + "</h3>" +
        "<div class='toolbar'><a class='btn' href='" + withToken("/api/runs/" + encodeURIComponent(runId) + "/export.xlsx") + "'>Скачать Excel</a>" +
        "<button class='btn' id='history-to-check'>Показать на странице проверки</button></div>";
      $("history-to-check").onclick = () => go("check");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      $("history-detail").textContent = error.message;
    }
  }

  $("refresh-history").onclick = loadHistory;

  $("compare-btn").onclick = async () => {
    const base = $("compare-base").value;
    const other = $("compare-other").value;
    if (!base || !other) {
      $("compare-result").innerHTML = "<div class='notice bad'>Выберите два прогона.</div>";
      return;
    }
    if (base === other) {
      $("compare-result").innerHTML = "<div class='notice bad'>Выберите разные прогоны.</div>";
      return;
    }
    $("compare-result").innerHTML = "<div class='notice'><span class='spinner'></span> Сравниваю…</div>";
    try {
      const data = await api("/api/runs/" + encodeURIComponent(base) + "/compare/" + encodeURIComponent(other));
      const items = data.items || [];
      let html = "<div class='notice good'>Добавлено: " + data.summary.added + " · исчезло: " + data.summary.removed +
        " · изменилось: " + data.summary.changed + " · без изменений: " + data.summary.unchanged + "</div>";
      if (!items.length) {
        html += "<div class='empty'>Различий по статусам и важности не найдено.</div>";
      } else {
        html += '<div class="tablewrap"><table><thead><tr><th>Изменение</th><th>Категория</th><th>Параметр</th>' +
          "<th>Было</th><th>Стало</th><th>Пункт ТЗ</th></tr></thead><tbody>";
        items.forEach((item) => {
          const label = item.kind === "added" ? "Новое" : item.kind === "removed" ? "Исчезло" : "Изменено";
          html += "<tr class='diff-" + esc(item.kind) + "'><td>" + label + "</td><td>" + esc(item.category) + "</td>" +
            "<td>" + esc(item.parameter) + "</td>" +
            "<td>" + esc((STATUS_NAMES[item.status_before] || item.status_before || "—") + (item.severity_before ? " · " + (SEVERITY_NAMES[item.severity_before] || item.severity_before) : "")) + "</td>" +
            "<td>" + esc((STATUS_NAMES[item.status_after] || item.status_after || "—") + (item.severity_after ? " · " + (SEVERITY_NAMES[item.severity_after] || item.severity_after) : "")) + "</td>" +
            "<td>" + esc(item.tz_locator || "") + "</td></tr>";
        });
        html += "</tbody></table></div>";
      }
      $("compare-result").innerHTML = html;
    } catch (error) {
      const message = /404|HTTP 404/.test(error.message)
        ? "Сервер не нашёл один из прогонов. Если оба прогона есть в истории — вероятно, запущена старая версия сервера; перезапустите start-local.bat."
        : error.message;
      $("compare-result").innerHTML = "<div class='notice bad'>" + esc(message) + "</div>";
    }
  };

  /* ------------------------------- шаблоны ------------------------------ */

  async function loadTemplates() {
    try {
      state.templates = await api("/api/templates");
      $("template-select").innerHTML = state.templates.length
        ? "<option value=''>Выберите шаблон…</option>" + state.templates.map((template) =>
            "<option value='" + esc(template.id) + "'>" + esc(template.name) + "</option>"
          ).join("")
        : "<option value=''>Нет сохранённых шаблонов</option>";
    } catch (error) {
      state.templates = [];
      $("template-select").innerHTML = "<option value=''>Шаблоны недоступны</option>";
      notify("check-message", "Не удалось загрузить шаблоны: " + error.message, "bad");
    }
  }

  $("template-load").onclick = () => {
    const id = $("template-select").value;
    const template = state.templates.find((item) => item.id === id);
    if (!template) {
      notify("check-message", state.templates.length ? "Выберите шаблон в списке." : "Сохранённых шаблонов пока нет: введите название и нажмите «Сохранить как шаблон».", "bad");
      return;
    }
    $("project-context").value = template.context || "";
    $("analysis-instructions").value = template.instructions || "";
    $("template-name").value = template.name;
    scheduleEstimate();
    notify("check-message", "Применён шаблон: " + template.name + ".", "good");
  };

  $("template-save").onclick = async () => {
    const name = $("template-name").value.trim();
    if (!name) {
      notify("check-message", "Введите название шаблона в поле рядом с кнопкой.", "bad");
      $("template-name").focus();
      return;
    }
    if (!$("project-context").value.trim() && !$("analysis-instructions").value.trim()) {
      notify("check-message", "Заполните контекст или инструкции перед сохранением шаблона.", "bad");
      return;
    }
    const button = $("template-save");
    button.disabled = true;
    try {
      const created = await api("/api/templates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name,
          context: $("project-context").value,
          instructions: $("analysis-instructions").value,
        }),
      });
      await loadTemplates();
      $("template-select").value = created.id;
      notify("check-message", "Шаблон «" + name + "» сохранён и выбран в списке.", "good");
    } catch (error) {
      notify("check-message", "Шаблон не сохранён: " + error.message, "bad");
    } finally {
      button.disabled = false;
    }
  };

  $("template-delete").onclick = async () => {
    const id = $("template-select").value;
    const template = state.templates.find((item) => item.id === id);
    if (!template) {
      notify("check-message", "Выберите шаблон для удаления.", "bad");
      return;
    }
    if (!confirm("Удалить шаблон «" + template.name + "»?")) return;
    try {
      await api("/api/templates/" + encodeURIComponent(id), { method: "DELETE" });
      $("template-name").value = "";
      await loadTemplates();
      notify("check-message", "Шаблон удалён.", "good");
    } catch (error) {
      notify("check-message", error.message, "bad");
    }
  };

  /* ------------------------------- настройки ---------------------------- */

  async function loadSettings() {
    try {
      const settings = await api("/api/settings");
      $("api-base").value = settings.base_url;
      $("api-model").value = settings.model;
      $("key-state").textContent = settings.has_api_key
        ? (settings.api_key_from_env ? "(ключ из переменной окружения)" : "(ключ сохранён)")
        : "(ключ не задан)";
      $("google-client").value = settings.google_client_id || "";
      $("sheet-id").value = settings.spreadsheet_id || "";
      $("use-embeddings").checked = !!settings.use_embeddings;
      $("embedding-model").value = settings.embedding_model || "";
      $("embedding-base").value = settings.embedding_base_url || "";
      $("embedding-key-state").textContent = settings.has_embedding_api_key ? "(ключ сохранён)" : "(не задан)";
      $("concurrency").value = settings.concurrency || 3;
      $("price-input").value = settings.price_input || 0;
      $("price-output").value = settings.price_output || 0;
      if (settings.auth_required && !state.token) {
        notify("token-message", "Сервер требует токен доступа — введите его ниже.", "bad");
      }
      const pill = $("server-pill");
      pill.textContent = settings.auth_required || !/^(127\.0\.0\.1|localhost)$/.test(location.hostname)
        ? "● Серверный режим"
        : "● Локальный сервер";
      pill.classList.toggle("server", settings.auth_required || !/^(127\.0\.0\.1|localhost)$/.test(location.hostname));
    } catch (error) {
      notify("settings-message", "Не удалось загрузить настройки: " + error.message, "bad");
      if (/токен/i.test(error.message)) {
        activatePage("settings");
        notify("token-message", "Введите токен доступа и сохраните его в сессии.", "bad");
      }
    }
  }

  $("save-settings").onclick = async () => {
    const body = {
      base_url: $("api-base").value.trim(),
      model: $("api-model").value.trim(),
      api_key: $("api-key").value,
      clear_api_key: $("clear-key").checked,
      google_client_id: $("google-client").value.trim(),
      spreadsheet_id: $("sheet-id").value.trim(),
      use_embeddings: $("use-embeddings").checked,
      embedding_model: $("embedding-model").value.trim(),
      embedding_base_url: $("embedding-base").value.trim(),
      embedding_api_key: $("embedding-key").value,
      clear_embedding_api_key: $("clear-embedding-key").checked,
      concurrency: Number($("concurrency").value || 3),
      price_input: Number($("price-input").value || 0),
      price_output: Number($("price-output").value || 0),
    };
    try {
      const result = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      $("api-key").value = "";
      $("clear-key").checked = false;
      $("embedding-key").value = "";
      $("clear-embedding-key").checked = false;
      $("key-state").textContent = result.has_api_key ? "(ключ сохранён)" : "(ключ не задан)";
      $("embedding-key-state").textContent = result.has_embedding_api_key ? "(ключ сохранён)" : "(не задан)";
      notify("settings-message", "Настройки сохранены. Ключ не возвращается в браузер.", "good");
      scheduleEstimate();
    } catch (error) {
      notify("settings-message", error.message, "bad");
    }
  };

  $("test-provider").onclick = async () => {
    notify("settings-message", "Проверяю подключение…");
    try {
      const result = await api("/api/settings/test", { method: "POST" });
      let message = "Подключение работает. Модель: " + (result.model || "—") + ".";
      if (result.embeddings) {
        message += result.embeddings.ok
          ? " Эмбеддинги: " + result.embeddings.dim + " измерений."
          : " Эмбеддинги провайдера недоступны (" + (result.embeddings.error || "нет ответа") + "); при проверке будут автоматически использованы локальные.";
      }
      notify("settings-message", message, result.embeddings && result.embeddings.ok === false ? "" : "good");
    } catch (error) {
      notify("settings-message", "Проверка не прошла: " + error.message, "bad");
    }
  };

  $("save-token").onclick = () => {
    state.token = $("auth-token").value.trim();
    if (state.token) sessionStorage.setItem("tz-token", state.token);
    else sessionStorage.removeItem("tz-token");
    refreshLogLink();
    notify("token-message", state.token ? "Токен сохранён в этой вкладке." : "Токен удалён.", "good");
    loadSettings();
  };

  function refreshLogLink() {
    const link = $("download-log");
    if (link) link.href = withToken("/api/logs/download");
  }

  $("show-log").onclick = async () => {
    const output = $("log-output");
    output.classList.remove("hidden");
    output.textContent = "Загружаю журнал…";
    try {
      const data = await api("/api/logs?lines=200");
      output.textContent = data.lines && data.lines.length
        ? data.lines.join("")
        : "Журнал пока пуст. Выполните проверку или действие в приложении.";
      output.scrollTop = output.scrollHeight;
    } catch (error) {
      output.textContent = "Не удалось прочитать журнал: " + error.message;
    }
  };

  /* ---------------------------- Google Sheets --------------------------- */

  $("connect-sheets").onclick = () => {
    const clientId = $("google-client").value.trim();
    if (!clientId) {
      notify("sheets-message", "Укажите Google OAuth Client ID и сохраните настройки.", "bad");
      return;
    }
    if (!window.google || !google.accounts || !google.accounts.oauth2) {
      notify("sheets-message", "Google Identity Services не загрузился. Проверьте интернет и блокировщики.", "bad");
      return;
    }
    state.tokenClient = google.accounts.oauth2.initTokenClient({
      client_id: clientId,
      scope: "https://www.googleapis.com/auth/spreadsheets",
      callback: async (response) => {
        if (response.error) {
          notify("sheets-message", "Ошибка Google OAuth: " + response.error, "bad");
          return;
        }
        try {
          state.sheetsToken = response.access_token;
          const title = await verifySheets();
          notify("sheets-message", "Google Sheets подключён: " + title + ". Токен хранится только в памяти вкладки.", "good");
          if (state.lastRun) $("save-sheets").disabled = false;
        } catch (error) {
          state.sheetsToken = null;
          notify("sheets-message", error.message, "bad");
        }
      },
      error_callback: (error) => notify("sheets-message", "Ошибка OAuth: " + (error.message || error.type), "bad"),
    });
    state.tokenClient.requestAccessToken({ prompt: "consent" });
  };

  async function verifySheets() {
    const sheetId = $("sheet-id").value.trim();
    if (!sheetId) throw new Error("Укажите ID Google-таблицы.");
    const response = await fetch(
      "https://sheets.googleapis.com/v4/spreadsheets/" + encodeURIComponent(sheetId) +
      "?fields=properties(title),sheets(properties(title))",
      { headers: { Authorization: "Bearer " + state.sheetsToken } }
    );
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error("Google Sheets API: " + ((body.error && body.error.message) || response.status));
    const names = (body.sheets || []).map((item) => item.properties.title);
    const required = ["Documents", "Requirements", "RequirementVersions", "VerificationRuns", "VerificationResults", "Issues", "AuditLog"];
    const missing = required.filter((name) => !names.includes(name));
    if (missing.length) throw new Error("В таблице отсутствуют листы: " + missing.join(", "));
    return body.properties.title;
  }

  async function sheetsNextRow(sheet) {
    const sheetId = $("sheet-id").value.trim();
    const response = await fetch(
      "https://sheets.googleapis.com/v4/spreadsheets/" + encodeURIComponent(sheetId) +
      "/values/" + encodeURIComponent(sheet + "!A:A"),
      { headers: { Authorization: "Bearer " + state.sheetsToken } }
    );
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error("Не удалось прочитать лист " + sheet + ".");
    return Math.max(2, (body.values || []).length + 1);
  }

  $("save-sheets").onclick = async () => {
    if (!state.lastRun || !state.sheetsToken) {
      notify("check-message", "Сначала выполните проверку и подключите Google Sheets.", "bad");
      return;
    }
    if (state.savedSheetRunId === state.lastRun.run_id) {
      notify("check-message", "Этот запуск уже сохранён в Google Sheets.", "good");
      return;
    }
    const button = $("save-sheets");
    button.disabled = true;
    notify("check-message", "Сохраняю реестр в Google Sheets…");
    try {
      const run = state.lastRun;
      const now = new Date().toISOString();
      const assignmentId = "TA-" + run.run_id;
      const issues = (run.results || []).filter((item) => !["COMPLIES", "NOT_APPLICABLE"].includes(item.status));
      const tzUrl = (run.results || []).find((item) => item.tz_url) ? (run.results || []).find((item) => item.tz_url).tz_url : "";
      const docRows = [[
        "DOC-" + run.run_id + "-TZ", run.tz_filename, run.tz_filename, "ТЗ", "", "", "", "ANALYSIS_SOURCE",
        run.context || "", tzUrl, "", "", "", now, "Исходный файл обработан локально; файл не хранится в Google Sheets.",
      ]];
      (run.standard_sources || []).forEach((doc) => {
        docRows.push([
          "DOC-" + run.run_id + "-" + doc.source_id, doc.filename, doc.filename, "СТО", "", "", "", "ANALYSIS_SOURCE",
          run.context || "", doc.source_url || "", "", "", "", now, "Источник использован в запуске " + run.run_id + ".",
        ]);
      });
      const runRow = [
        run.run_id, assignmentId, run.title || run.tz_filename, "", run.created_at, now, run.status,
        JSON.stringify({ context: run.context, coverage: run.coverage, warnings: run.warnings, standard_sources: run.standard_sources }),
        run.results.length, run.results.length, issues.length, "local-user", "0.3.0",
      ];
      const resultRows = (run.results || []).map((item) => [
        run.run_id + "-" + item.result_id, run.run_id, "AI-" + item.result_id, assignmentId, item.status,
        item.tz_quote || "", item.tz_locator || "",
        JSON.stringify({ operator: item.operator, value: item.expected_value, unit: item.expected_unit }),
        JSON.stringify({ value: item.actual_value, unit: item.actual_unit }),
        (item.explanation || "") +
          (item.semantic_reasoning ? "\nСопоставление: " + item.semantic_reasoning : "") +
          (item.technical_assessment ? "\nТехническая оценка: " + item.technical_assessment : "") +
          (item.recommendation ? "\nРекомендация: " + item.recommendation : ""),
        JSON.stringify({
          finding_type: item.finding_type, severity: item.severity, confidence: item.confidence,
          applicability_assessment: item.applicability_assessment, tz_filename: item.tz_filename,
          tz_url: item.tz_url, tz_quote: item.tz_quote, standard_source_id: item.standard_source_id,
          standard_filename: item.standard_filename, standard_url: item.standard_url,
          standard_locator: item.standard_locator, standard_quote: item.standard_quote,
          evidence_verified: item.evidence_verified, review_note: item.review_note,
        }),
        "", "PENDING_REVIEW", now,
      ]);
      const issueRows = issues.map((item, index) => [
        run.run_id + "-ISS-" + String(index + 1).padStart(3, "0"), run.run_id, run.run_id + "-" + item.result_id,
        String(index + 1), item.status === "NONCOMPLIANCE" ? "HIGH" : "REVIEW", item.category || "", "",
        item.tz_locator || "", item.tz_quote || "", item.standard_quote || "", item.standard_source_id || "",
        item.standard_locator || "", item.explanation || "", item.recommendation || "", "", "OPEN", "", "", now, now,
      ]);
      const data = [];
      let row = await sheetsNextRow("Documents");
      data.push({ range: "Documents!A" + row + ":O" + (row + docRows.length - 1), values: docRows });
      row = await sheetsNextRow("VerificationRuns");
      data.push({ range: "VerificationRuns!A" + row + ":M" + row, values: [runRow] });
      if (resultRows.length) {
        row = await sheetsNextRow("VerificationResults");
        data.push({ range: "VerificationResults!A" + row + ":N" + (row + resultRows.length - 1), values: resultRows });
      }
      if (issueRows.length) {
        row = await sheetsNextRow("Issues");
        data.push({ range: "Issues!A" + row + ":T" + (row + issueRows.length - 1), values: issueRows });
      }
      row = await sheetsNextRow("AuditLog");
      data.push({
        range: "AuditLog!A" + row + ":J" + row,
        values: [[
          run.run_id + "-AUDIT", now, "local-user", "SAVE_VERIFICATION_RESULTS", "VerificationRun", run.run_id, "{}",
          JSON.stringify({ results: resultRows.length, issues: issueRows.length }),
          "Результаты сохранены из TZ Checker", "TZ Checker",
        ]],
      });
      const sheetId = $("sheet-id").value.trim();
      const response = await fetch(
        "https://sheets.googleapis.com/v4/spreadsheets/" + encodeURIComponent(sheetId) + "/values:batchUpdate",
        {
          method: "POST",
          headers: { Authorization: "Bearer " + state.sheetsToken, "Content-Type": "application/json" },
          body: JSON.stringify({ valueInputOption: "RAW", data: data }),
        }
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error("Google Sheets не принял записи: " + ((body.error && body.error.message) || response.status));
      state.savedSheetRunId = run.run_id;
      notify("check-message", "Сохранено в Google Sheets: " + resultRows.length + " результатов, " + issueRows.length + " замечаний.", "good");
    } catch (error) {
      button.disabled = false;
      notify("check-message", "Не удалось сохранить в Google Sheets: " + error.message, "bad");
    }
  };

  /* -------------------------------- запуск ------------------------------ */

  (async function init() {
    const params = new URLSearchParams(location.search);
    const tokenFromUrl = params.get("token");
    if (tokenFromUrl) {
      state.token = tokenFromUrl;
      sessionStorage.setItem("tz-token", tokenFromUrl);
      params.delete("token");
      const query = params.toString();
      history.replaceState(null, "", location.pathname + (query ? "?" + query : ""));
    }
    $("auth-token").value = state.token;
    refreshLogLink();
    await checkServer();
    await loadSettings();
    await loadTemplates();
    await loadStandardScope();
  })();
})();
