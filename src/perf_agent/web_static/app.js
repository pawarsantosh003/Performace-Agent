const fields = {
  quickUrl: document.querySelector("#quickUrl"),
  applicationName: document.querySelector("#applicationName"),
  releaseId: document.querySelector("#releaseId"),
  environmentName: document.querySelector("#environmentName"),
  testType: document.querySelector("#testType"),
  testEngine: document.querySelector("#testEngine"),
  concurrentUsers: document.querySelector("#concurrentUsers"),
  durationSeconds: document.querySelector("#durationSeconds"),
  p95Sla: document.querySelector("#p95Sla"),
  errorRate: document.querySelector("#errorRate"),
  approveRisky: document.querySelector("#approveRisky"),
  configJson: document.querySelector("#configJson"),
  configState: document.querySelector("#configState"),
  engineNotice: document.querySelector("#engineNotice"),
  validationList: document.querySelector("#validationList"),
  runSearch: document.querySelector("#runSearch")
};

let mode = "builder";
let currentConfig = null;
let progressTimer = null;

document.querySelector("#quickModeBtn").addEventListener("click", () => setMode("builder"));
document.querySelector("#releaseModeBtn").addEventListener("click", () => setMode("json"));
document.querySelector("#loadSampleBtn").addEventListener("click", loadSample);
document.querySelector("#runBtn").addEventListener("click", runAssessment);
document.querySelector("#syncJsonBtn").addEventListener("click", () => writeConfig(buildBuilderConfig()));
document.querySelector("#refreshRunsBtn").addEventListener("click", loadRunHistory);
fields.runSearch.addEventListener("input", debounce(loadRunHistory, 250));
fields.testEngine.addEventListener("change", () => {
  fields.engineNotice.classList.toggle("hidden", fields.testEngine.value === "synthetic");
  if (mode === "builder") writeConfig(buildBuilderConfig());
});
fields.configJson.addEventListener("input", () => setConfigState("Edited"));

for (const key of ["quickUrl", "applicationName", "releaseId", "environmentName", "testType", "testEngine", "concurrentUsers", "durationSeconds", "p95Sla", "errorRate"]) {
  fields[key].addEventListener("input", () => {
    validateBuilder(false);
    if (mode === "builder") writeConfig(buildBuilderConfig());
  });
}

document.querySelectorAll(".result-tab").forEach((button) => {
  button.addEventListener("click", () => showResultView(button.dataset.view));
});

setDefaults();
writeConfig(buildBuilderConfig());
loadRunHistory();

function setDefaults() {
  fields.quickUrl.value = "https://example.com";
  fields.applicationName.value = "My Web Application";
  fields.releaseId.value = new Date().toISOString().slice(0, 10);
  fields.environmentName.value = "pre-prod";
  fields.testType.value = "smoke";
  fields.testEngine.value = "synthetic";
  fields.concurrentUsers.value = 25;
  fields.durationSeconds.value = 30;
  fields.p95Sla.value = 1200;
  fields.errorRate.value = 0.5;
  fields.approveRisky.checked = false;
}

function setMode(nextMode) {
  mode = nextMode;
  document.querySelector("#quickModeBtn").classList.toggle("active", mode === "builder");
  document.querySelector("#releaseModeBtn").classList.toggle("active", mode === "json");
  document.querySelector("#builderForm").classList.toggle("hidden", mode !== "builder");
  document.querySelector("#releaseForm").classList.toggle("hidden", mode !== "json");
  setConfigState(mode === "builder" ? "Ready" : "Advanced");
}

async function loadSample() {
  setConfigState("Loading");
  clearError();
  const response = await fetch("/api/sample-config");
  currentConfig = await response.json();
  populateFormFromConfig(currentConfig);
  writeConfig(currentConfig);
  setMode("json");
  fields.approveRisky.checked = true;
  setConfigState("Demo Loaded");
}

function populateFormFromConfig(config) {
  const scenario = config.scenarios?.[0] || {};
  const page = scenario.pages?.[0] || {};
  const endpoint = scenario.endpoints?.[0] || {};
  fields.quickUrl.value = page.url || endpoint.url || config.environment?.base_url || "";
  fields.applicationName.value = config.application_name || "";
  fields.releaseId.value = config.release_id || "";
  fields.environmentName.value = config.environment?.name || "pre-prod";
  fields.testType.value = scenario.test_type || "smoke";
  fields.testEngine.value = config.test_engine || "synthetic";
  fields.concurrentUsers.value = scenario.workload?.concurrent_users || 25;
  fields.durationSeconds.value = String(Math.min(Number(scenario.workload?.duration_seconds || 30), 300));
  fields.p95Sla.value = endpoint.sla?.p95_ms || 1200;
  fields.errorRate.value = endpoint.sla?.error_rate_pct || 0.5;
}

function buildBuilderConfig() {
  const url = normalizedUrl(fields.quickUrl.value.trim());
  const testType = fields.testType.value;
  const duration = Number(fields.durationSeconds.value || 30);
  const users = Number(fields.concurrentUsers.value || 1);
  const appName = fields.applicationName.value.trim() || "Web Application";
  const releaseId = fields.releaseId.value.trim() || new Date().toISOString().slice(0, 10);
  const targetTps = Math.max(1, Math.round(users * 0.8));
  const p95 = Number(fields.p95Sla.value || 1200);
  const errorRate = Number(fields.errorRate.value || 0.5);

  return {
    application_name: appName,
    release_id: releaseId,
    test_engine: fields.testEngine.value,
    environment: {
      name: fields.environmentName.value,
      base_url: url,
      allow_risky_tests: false,
      max_concurrent_users: 1000,
      max_duration_seconds: 900
    },
    web_vitals: {
      lcp_p75_ms: 2500,
      inp_p75_ms: 200,
      cls_p75: 0.1,
      fcp_p75_ms: 1800,
      ttfb_p75_ms: 800
    },
    scenarios: [
      {
        name: `${testType} web assessment`,
        test_type: testType,
        requires_approval: ["stress", "spike", "endurance"].includes(testType),
        workload: {
          concurrent_users: users,
          duration_seconds: duration,
          ramp_up_seconds: Math.min(30, duration),
          target_tps: targetTps
        },
        endpoints: [
          {
            name: "website availability",
            method: "GET",
            url,
            business_criticality: 4,
            sla: {
              p95_ms: p95,
              p99_ms: Math.max(p95 * 2, 1000),
              error_rate_pct: errorRate,
              throughput_rps: targetTps
            }
          }
        ],
        pages: [
          {
            name: "website page",
            url,
            business_criticality: 4
          }
        ]
      }
    ]
  };
}

function validateBuilder(showMessages = true) {
  const errors = [];
  const url = fields.quickUrl.value.trim();
  const users = Number(fields.concurrentUsers.value);
  const p95 = Number(fields.p95Sla.value);
  const errorRate = Number(fields.errorRate.value);
  const testType = fields.testType.value;

  if (!url) errors.push("Enter a website or API URL.");
  if (url && !isValidHttpUrl(normalizedUrl(url))) errors.push("URL must start with http:// or https:// and be valid.");
  if (!fields.applicationName.value.trim()) errors.push("Application name is required.");
  if (!fields.releaseId.value.trim()) errors.push("Release is required.");
  if (!Number.isFinite(users) || users < 1 || users > 1000) errors.push("Users must be between 1 and 1000.");
  if (!Number.isFinite(p95) || p95 < 100) errors.push("p95 SLA must be at least 100 ms.");
  if (!Number.isFinite(errorRate) || errorRate < 0 || errorRate > 100) errors.push("Error budget must be between 0 and 100%.");
  if (["stress", "spike", "endurance"].includes(testType) && !fields.approveRisky.checked) {
    errors.push("Risky test types need approval.");
  }

  renderValidation(errors, showMessages);
  return errors;
}

function renderValidation(errors, showMessages) {
  fields.validationList.innerHTML = "";
  fields.validationList.classList.toggle("hidden", !showMessages || !errors.length);
  errors.forEach((error) => {
    const item = document.createElement("div");
    item.textContent = error;
    fields.validationList.appendChild(item);
  });
}

function parseConfigJson() {
  try {
    const config = JSON.parse(fields.configJson.value);
    currentConfig = config;
    clearError();
    return config;
  } catch (error) {
    showError(`Configuration JSON is not valid: ${error.message}`);
    setConfigState("Fix JSON");
    return null;
  }
}

function writeConfig(config) {
  currentConfig = config;
  fields.configJson.value = JSON.stringify(config, null, 2);
}

async function runAssessment() {
  const errors = mode === "builder" ? validateBuilder(true) : [];
  if (errors.length) return;
  const config = mode === "builder" ? buildBuilderConfig() : parseConfigJson();
  if (!config) return;
  if (mode === "builder") writeConfig(config);

  setRunning(true);
  clearError();
  showOnly("loadingState");
  startProgress(fields.testEngine.value);

  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        config,
        approve_risky: fields.approveRisky.checked,
        use_k6: fields.testEngine.value === "k6"
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Assessment failed.");
    await loadRunDetails(payload.run_id, payload);
    setConfigState("Complete");
    loadRunHistory();
  } catch (error) {
    showOnly("emptyState");
    showError(error.message);
    setConfigState("Failed");
  } finally {
    stopProgress();
    setRunning(false);
  }
}

function startProgress(engine) {
  const realEngine = engine !== "synthetic";
  const steps = [
    "Validating inputs",
    "Preparing scenario",
    realEngine ? `Executing ${engineLabel(engine)}` : "Running fast URL probe",
    "Analyzing Web/API signals",
    "Scoring release readiness",
    "Writing report and artifacts"
  ];
  let index = 0;
  renderProgress(steps, index);
  progressTimer = setInterval(() => {
    index = Math.min(index + 1, steps.length - 1);
    renderProgress(steps, index);
  }, realEngine ? 1800 : 450);
}

function renderProgress(steps, activeIndex) {
  document.querySelector("#progressTitle").textContent = steps[activeIndex];
  document.querySelector("#loadingText").textContent = activeIndex >= steps.length - 2
    ? "Almost done. Preparing the result view."
    : "The agent is working through the assessment stages.";
  document.querySelector("#progressFill").style.width = `${Math.round(((activeIndex + 1) / steps.length) * 100)}%`;
  const list = document.querySelector("#progressSteps");
  list.innerHTML = "";
  steps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = index < activeIndex ? "done" : index === activeIndex ? "active" : "";
    item.textContent = step;
    list.appendChild(item);
  });
}

function stopProgress() {
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = null;
}

async function loadRunHistory() {
  const q = encodeURIComponent(fields.runSearch.value.trim());
  const response = await fetch(`/api/runs${q ? `?q=${q}` : ""}`);
  const runs = await response.json();
  renderRunHistory(runs);
}

function renderRunHistory(runs) {
  const list = document.querySelector("#runHistoryList");
  list.innerHTML = "";
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "No saved runs found.";
    list.appendChild(empty);
    return;
  }
  runs.slice(0, 30).forEach((run) => {
    const button = document.createElement("button");
    const readiness = run.readiness || {};
    button.type = "button";
    button.className = "run-history-item";
    button.innerHTML = `
      <span class="history-title">${escapeHtml(run.application_name || run.run_id)}</span>
      <span class="history-meta">${escapeHtml(run.release_id || "")} | ${escapeHtml(run.test_type || "")} | ${escapeHtml(run.test_engine || "")} | ${escapeHtml(run.primary_url || "")}</span>
      <span class="status-row">
        <span class="status-pill ${escapeHtml(readiness.status || "unknown")}">${escapeHtml(statusLabel(readiness.status))}</span>
        <span>${escapeHtml(String(readiness.score ?? "n/a"))}/100</span>
        <span>${escapeHtml(String(run.finding_count ?? "n/a"))} findings</span>
      </span>
    `;
    button.addEventListener("click", () => loadRunDetails(run.run_id));
    list.appendChild(button);
  });
}

function engineLabel(engine) {
  return {
    synthetic: "Fast Assessment",
    k6: "k6",
    lighthouse: "Lighthouse",
    k6_lighthouse: "k6 + Lighthouse",
    pagespeed: "PageSpeed Insights",
    webpagetest: "WebPageTest",
    jmeter: "JMeter"
  }[engine] || engine;
}

async function loadRunDetails(runId, knownPayload = null) {
  let payload = knownPayload;
  if (!payload || !payload.report_text) {
    const response = await fetch(`/api/run-details/${encodeURIComponent(runId)}`);
    payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not open run.");
  }
  renderResults(payload);
  showOnly("resultsState");
}

function renderResults(payload) {
  const readiness = payload.readiness;
  const manifest = payload.manifest || {};
  const status = readiness.status || "unknown";
  const band = document.querySelector(".readiness-band");
  band.className = `readiness-band ${status}`;
  document.querySelector("#readinessStatus").textContent = statusLabel(status);
  document.querySelector("#readinessScore").textContent = Math.round(readiness.score || 0);
  document.querySelector("#runSubtitle").textContent = [
    manifest.application_name,
    manifest.release_id,
    manifest.primary_url
  ].filter(Boolean).join(" | ");

  renderDownloads(payload.artifacts || {});
  renderDimensions(readiness.dimensions || {});
  renderFindings(payload.findings || []);
  renderScenarios(payload.scenario_results || []);
  document.querySelector("#reportText").textContent = payload.report_text || "Open the report artifact to view report text.";
  showResultView("summaryView");
}

function renderDownloads(artifacts) {
  const bar = document.querySelector("#downloadBar");
  bar.innerHTML = "";
  const labels = {
    report: "Report",
    baseline: "Baseline",
    backlog: "Backlog",
    readiness: "Readiness JSON",
    raw: "Raw Results"
  };
  for (const [name, label] of Object.entries(labels)) {
    if (!artifacts[name]) continue;
    const link = document.createElement("a");
    link.href = artifacts[name];
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = label;
    bar.appendChild(link);
  }
}

function renderDimensions(dimensions) {
  const grid = document.querySelector("#dimensionGrid");
  grid.innerHTML = "";
  for (const [name, score] of Object.entries(dimensions)) {
    grid.appendChild(tile(titleCase(name), Number(score).toFixed(1)));
  }
}

function renderFindings(findings) {
  const list = document.querySelector("#findingsList");
  list.innerHTML = "";
  if (!findings.length) {
    list.appendChild(emptyLine("No threshold breaches found for this run."));
  }
  findings.slice(0, 20).forEach((finding, index) => {
    list.appendChild(findingCard(finding, index + 1));
  });
}

function renderScenarios(scenarios) {
  const list = document.querySelector("#scenarioList");
  list.innerHTML = "";
  scenarios.forEach((scenario) => list.appendChild(scenarioCard(scenario)));
}

function showResultView(id) {
  document.querySelectorAll(".result-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === id);
  });
  document.querySelectorAll(".result-view").forEach((view) => {
    view.classList.toggle("hidden", view.id !== id);
  });
}

function tile(label, value) {
  const item = document.createElement("div");
  item.className = "metric-tile";
  item.innerHTML = `<strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span>`;
  return item;
}

function findingCard(finding, priority) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "finding";
  item.setAttribute("aria-expanded", "false");
  const evidence = (finding.evidence || []).slice(0, 3).map(escapeHtml).join(" | ");
  item.innerHTML = `
    <div class="finding-header">
      <div>
        <h3>P${priority}: ${escapeHtml(finding.title)}</h3>
        <span class="click-hint">Click for root cause, solution steps, owners, and validation</span>
      </div>
      <div class="finding-right">
        <span class="severity ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
        <span class="chevron" aria-hidden="true">v</span>
      </div>
    </div>
    <p>${escapeHtml(finding.recommendation)}</p>
    <div class="finding-meta">
      <span>Score ${escapeHtml(String(finding.score))}</span>
      <span>${escapeHtml(finding.category)}</span>
      <span>${evidence}</span>
    </div>
    <div class="finding-detail" hidden>
      ${detailBlock("Likely root cause", [finding.likely_cause || "More telemetry is needed to isolate the exact root cause."])}
      ${detailBlock("Recommended solution steps", finding.solution_steps || [])}
      ${detailBlock("Owner actions", finding.owner_actions || [])}
      ${detailBlock("Validation plan", [finding.validation_plan])}
      ${linkBlock("Reference links", finding.documentation_links || [])}
      ${detailBlock("Evidence", finding.evidence || [])}
    </div>
  `;
  item.addEventListener("click", (event) => {
    if (event.target.tagName === "A") return;
    const expanded = item.getAttribute("aria-expanded") === "true";
    item.setAttribute("aria-expanded", String(!expanded));
    item.querySelector(".finding-detail").hidden = expanded;
  });
  return item;
}

function detailBlock(title, items) {
  const usable = items.filter(Boolean);
  if (!usable.length) return "";
  return `<section><h4>${escapeHtml(title)}</h4><ul>${usable.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
}

function linkBlock(title, links) {
  const usable = links.filter(Boolean);
  if (!usable.length) return "";
  return `<section><h4>${escapeHtml(title)}</h4><div class="reference-links">${usable.map((link) => `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer">${escapeHtml(shortLink(link))}</a>`).join("")}</div></section>`;
}

function scenarioCard(scenario) {
  const item = document.createElement("article");
  item.className = "scenario";
  const endpointRows = (scenario.endpoint_results || []).map((endpoint) => `
    <tr><td>${escapeHtml(endpoint.name)}</td><td>${escapeHtml(String(endpoint.p95_ms))}</td><td>${escapeHtml(String(endpoint.p99_ms))}</td><td>${escapeHtml(String(endpoint.throughput_rps))}</td><td>${escapeHtml(String(endpoint.error_rate_pct))}</td></tr>
  `).join("");
  const webRows = (scenario.web_vitals_results || []).map((page) => `
    <tr><td>${escapeHtml(page.page_name)}</td><td>${escapeHtml(String(page.lcp_p75_ms))}</td><td>${escapeHtml(String(page.inp_p75_ms))}</td><td>${escapeHtml(String(page.cls_p75))}</td><td>${escapeHtml(String(page.ttfb_p75_ms))}</td><td>${escapeHtml(page.source || "synthetic")}</td></tr>
  `).join("");
  item.innerHTML = `
    <h3>${escapeHtml(scenario.scenario_name)} (${escapeHtml(scenario.test_type)})</h3>
    ${endpointRows ? `<table><thead><tr><th>Endpoint</th><th>p95</th><th>p99</th><th>RPS</th><th>Error %</th></tr></thead><tbody>${endpointRows}</tbody></table>` : ""}
    ${webRows ? `<table><thead><tr><th>Page</th><th>LCP</th><th>INP</th><th>CLS</th><th>TTFB</th><th>Source</th></tr></thead><tbody>${webRows}</tbody></table>` : ""}
  `;
  return item;
}

function emptyLine(text) {
  const item = document.createElement("div");
  item.className = "empty-line";
  item.textContent = text;
  return item;
}

function statusLabel(status) {
  const map = {
    green: "Passed",
    amber: "Amber",
    red: "Failed",
    blocked: "Blocked",
    unknown: "Unknown"
  };
  return map[status] || titleCase(String(status || "unknown"));
}

function normalizedUrl(value) {
  if (!value) return "";
  if (value.startsWith("http://") || value.startsWith("https://")) return value;
  return `https://${value}`;
}

function isValidHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function showOnly(id) {
  for (const name of ["emptyState", "loadingState", "resultsState"]) {
    document.querySelector(`#${name}`).classList.toggle("hidden", name !== id);
  }
}

function showError(message) {
  const error = document.querySelector("#errorState");
  error.textContent = message;
  error.classList.remove("hidden");
}

function clearError() {
  const error = document.querySelector("#errorState");
  error.textContent = "";
  error.classList.add("hidden");
}

function setRunning(isRunning) {
  document.querySelector("#runBtn").disabled = isRunning;
  document.querySelector("#loadSampleBtn").disabled = isRunning;
}

function setConfigState(value) {
  fields.configState.textContent = value;
}

function titleCase(value) {
  return String(value).replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function shortLink(value) {
  try {
    const url = new URL(value);
    return `${url.hostname}${url.pathname}`;
  } catch {
    return value;
  }
}

function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
