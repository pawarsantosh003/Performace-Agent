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
  configJson: document.querySelector("#configJson"),
  configState: document.querySelector("#configState"),
  engineNotice: document.querySelector("#engineNotice"),
  validationList: document.querySelector("#validationList"),
  runSearch: document.querySelector("#runSearch"),
  loginUsername: document.querySelector("#loginUsername"),
  loginPassword: document.querySelector("#loginPassword"),
  loginBtn: document.querySelector("#loginBtn"),
  logoutBtn: document.querySelector("#logoutBtn"),
  userBadge: document.querySelector("#userBadge"),
  loginOverlay: document.querySelector("#loginOverlay"),
  loginForm: document.querySelector("#loginForm"),
  signupForm: document.querySelector("#signupForm"),
  toggleSignupBtn: document.querySelector("#toggleSignupBtn"),
  toggleLoginBtn: document.querySelector("#toggleLoginBtn"),
  signupEmail: document.querySelector("#signupEmail"),
  signupPassword: document.querySelector("#signupPassword"),
  signupPasswordConfirm: document.querySelector("#signupPasswordConfirm"),
  signupBtn: document.querySelector("#signupBtn"),
  signupToggle: document.querySelector("#signupToggle"),
  signupIntro: document.querySelector("#signupIntro"),
  signupNote: document.querySelector("#signupNote"),
  approvalPanel: document.querySelector("#approvalPanel"),
  approvalComment: document.querySelector("#approvalComment"),
  requestApprovalBtn: document.querySelector("#requestApprovalBtn"),
  refreshApprovalsBtn: document.querySelector("#refreshApprovalsBtn"),
  approvalList: document.querySelector("#approvalList"),
  riskyApprovalState: document.querySelector("#riskyApprovalState"),
  auditPanel: document.querySelector("#auditPanel"),
  refreshAuditBtn: document.querySelector("#refreshAuditBtn"),
  auditList: document.querySelector("#auditList")
};

let mode = "builder";
let currentConfig = null;
let progressTimer = null;
let currentApprovalId = "";
const auth = { currentUser: null, signupEnabled: false, signupDomain: "" };

async function loadSession() {
  try {
    const response = await fetch("/api/session");
    if (!response.ok) {
      auth.currentUser = null;
    } else {
      const payload = await response.json();
      auth.currentUser = payload.authenticated ? payload : null;
      auth.signupEnabled = Boolean(payload.signup_enabled);
      auth.signupDomain = payload.signup_domain || "";
    }
  } catch (error) {
    auth.currentUser = null;
  }
  updateAuthUI();
}

function updateAuthUI() {
  const signedIn = auth.currentUser && auth.currentUser.authenticated;
  fields.loginOverlay.classList.toggle("hidden", signedIn);
  document.getElementById("appShell").classList.toggle("hidden", !signedIn);
  fields.logoutBtn.classList.toggle("hidden", !signedIn);
  fields.userBadge.classList.toggle("hidden", !signedIn);
  if (signedIn) {
    fields.userBadge.textContent = `${auth.currentUser.username} (${auth.currentUser.role})`;
    const canTest = ["tester", "approver", "admin"].includes(auth.currentUser.role);
    fields.approvalPanel.classList.toggle("hidden", !canTest);
    fields.auditPanel.classList.toggle("hidden", auth.currentUser.role !== "admin");
    document.querySelector("#runBtn").disabled = !canTest;
    fields.requestApprovalBtn.classList.toggle("hidden", !canTest);
    loadApprovals();
    loadRunHistory();
    if (auth.currentUser.role === "admin") loadAudit();
  } else {
    fields.approvalPanel.classList.add("hidden");
    fields.auditPanel.classList.add("hidden");
  }
  fields.signupToggle.classList.toggle("hidden", signedIn || !auth.signupEnabled);
  const domain = auth.signupDomain;
  fields.signupIntro.textContent = domain
    ? `Sign up with your @${domain} organization email.`
    : "Self-service signup is disabled.";
  fields.signupNote.textContent = domain
    ? `Accounts are limited to @${domain} email addresses.`
    : "Ask an administrator to provision your account.";
}

async function login() {
  clearError();
  const username = fields.loginUsername.value.trim();
  const password = fields.loginPassword.value;
  if (!username || !password) {
    showError("Enter both username and password.");
    return;
  }

  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Login failed.");
    }
    auth.currentUser = payload;
    fields.loginPassword.value = "";
    loadSession();
  } catch (error) {
    showError(error.message);
  }
}

async function signup() {
  clearError();
  const email = fields.signupEmail.value.trim();
  const password = fields.signupPassword.value;
  const passwordConfirm = fields.signupPasswordConfirm.value;

  if (!email || !password) {
    showError("Email and password are required.");
    return;
  }

  if (password !== passwordConfirm) {
    showError("Passwords do not match.");
    return;
  }

  if (password.length < 12) {
    showError("Password must be at least 12 characters long.");
    return;
  }

  if (!email.includes("@")) {
    showError("Please enter a valid email address.");
    return;
  }

  try {
    const response = await fetch("/api/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, password_confirm: passwordConfirm }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Signup failed.");
    }
    auth.currentUser = payload;
    fields.signupEmail.value = "";
    fields.signupPassword.value = "";
    fields.signupPasswordConfirm.value = "";
    loadSession();
  } catch (error) {
    showError(error.message);
  }
}

function toggleAuthForm(form) {
  const isLogin = form === "login";
  fields.loginForm.classList.toggle("hidden", !isLogin);
  fields.signupForm.classList.toggle("hidden", isLogin);
  clearError();
  fields.loginUsername.value = "";
  fields.loginPassword.value = "";
  fields.signupEmail.value = "";
  fields.signupPassword.value = "";
  fields.signupPasswordConfirm.value = "";
}

async function logout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch {
    // ignore network errors during logout
  }
  auth.currentUser = null;
  updateAuthUI();
}

document.querySelector("#quickModeBtn").addEventListener("click", () => setMode("builder"));
document.querySelector("#releaseModeBtn").addEventListener("click", () => setMode("json"));
document.querySelector("#loadSampleBtn").addEventListener("click", loadSample);
document.querySelector("#runBtn").addEventListener("click", runAssessment);
document.querySelector("#syncJsonBtn").addEventListener("click", () => writeConfig(buildBuilderConfig()));
document.querySelector("#refreshRunsBtn").addEventListener("click", loadRunHistory);
fields.loginBtn.addEventListener("click", login);
fields.signupBtn.addEventListener("click", signup);
fields.logoutBtn.addEventListener("click", logout);
fields.requestApprovalBtn.addEventListener("click", requestApproval);
fields.refreshApprovalsBtn.addEventListener("click", loadApprovals);
fields.refreshAuditBtn.addEventListener("click", loadAudit);
fields.toggleSignupBtn.addEventListener("click", () => toggleAuthForm("signup"));
fields.toggleLoginBtn.addEventListener("click", () => toggleAuthForm("login"));
fields.runSearch.addEventListener("input", debounce(loadRunHistory, 250));
fields.testEngine.addEventListener("change", () => {
  fields.engineNotice.classList.toggle("hidden", fields.testEngine.value === "synthetic");
  if (mode === "builder") writeConfig(buildBuilderConfig());
});
fields.configJson.addEventListener("input", () => {
  clearSelectedApproval();
  setConfigState("Edited");
});

for (const key of ["quickUrl", "applicationName", "releaseId", "environmentName", "testType", "testEngine", "concurrentUsers", "durationSeconds", "p95Sla", "errorRate"]) {
  fields[key].addEventListener("input", () => {
    clearSelectedApproval();
    validateBuilder(false);
    if (mode === "builder") writeConfig(buildBuilderConfig());
  });
}

document.querySelectorAll(".result-tab").forEach((button) => {
  button.addEventListener("click", () => showResultView(button.dataset.view));
});

setDefaults();
writeConfig(buildBuilderConfig());
loadSession();

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
  clearSelectedApproval();
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

function validateBuilder(showMessages = true, requireApproval = true) {
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
  if (requireApproval && ["stress", "spike", "endurance"].includes(testType) && !currentApprovalId) {
    errors.push("Select an approved request before running a risky test.");
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
  if (!auth.currentUser || !auth.currentUser.authenticated) {
    showError("Sign in before running an assessment.");
    return;
  }
  const role = auth.currentUser.role;
  if (role === "viewer") {
    showError("Viewer role cannot start assessments. Contact an Administrator.");
    return;
  }
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
        approval_id: currentApprovalId || null,
        use_k6: fields.testEngine.value === "k6"
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Assessment failed.");
    await loadRunDetails(payload.run_id, payload);
    clearSelectedApproval();
    loadApprovals();
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

async function requestApproval() {
  clearError();
  if (!auth.currentUser || auth.currentUser.role === "viewer") {
    showError("Tester, Approver, or Admin role is required to request approval.");
    return;
  }
  const errors = mode === "builder" ? validateBuilder(true, false) : [];
  if (errors.length) return;
  const config = mode === "builder" ? buildBuilderConfig() : parseConfigJson();
  if (!config) return;
  if (!config.scenarios?.some((scenario) => ["stress", "spike", "endurance"].includes(scenario.test_type))) {
    showError("Approval is only needed for stress, spike, or endurance scenarios.");
    return;
  }
  try {
    const response = await fetch("/api/approval-request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config, comment: fields.approvalComment.value.trim() })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Approval request failed.");
    fields.approvalComment.value = "";
    clearSelectedApproval();
    setConfigState("Approval Pending");
    await loadApprovals();
  } catch (error) {
    showError(error.message);
  }
}

async function loadApprovals() {
  if (!auth.currentUser) return;
  try {
    const response = await fetch("/api/approvals");
    const approvals = await response.json();
    if (!response.ok) throw new Error(approvals.error || "Could not load approvals.");
    renderApprovals(approvals);
  } catch (error) {
    fields.approvalList.innerHTML = "";
    fields.approvalList.appendChild(emptyLine(error.message));
  }
}

function renderApprovals(approvals) {
  fields.approvalList.innerHTML = "";
  if (!approvals.length) {
    fields.approvalList.appendChild(emptyLine("No approval requests."));
    return;
  }
  approvals.slice(0, 30).forEach((approval) => {
    const item = document.createElement("article");
    item.className = "approval-card";
    const canDecide = ["approver", "admin"].includes(auth.currentUser.role) && approval.status === "pending";
    const canUse = approval.status === "approved" && approval.requested_by === auth.currentUser.username;
    item.innerHTML = `
      <div class="approval-header">
        <strong>${escapeHtml(approval.application_name)} / ${escapeHtml(approval.release_id)}</strong>
        <span class="status-pill ${approvalStatusClass(approval.status)}">${escapeHtml(titleCase(approval.status))}</span>
      </div>
      <p>${escapeHtml((approval.scenario_names || []).join(", "))}</p>
      <small>Requested by ${escapeHtml(approval.requested_by)} for ${escapeHtml(approval.environment)}</small>
      <div class="approval-buttons"></div>
    `;
    const actions = item.querySelector(".approval-buttons");
    if (canUse) {
      actions.appendChild(actionButton("Use Approval", () => selectApproval(approval)));
    }
    if (canDecide) {
      actions.appendChild(actionButton("Approve", () => decideApproval(approval.approval_id, "approve"), "primary"));
      actions.appendChild(actionButton("Reject", () => decideApproval(approval.approval_id, "reject")));
    }
    fields.approvalList.appendChild(item);
  });
}

function actionButton(label, handler, variant = "secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${variant} small`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

async function decideApproval(approvalId, decision) {
  try {
    const response = await fetch(`/api/${decision}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_id: approvalId, comment: fields.approvalComment.value.trim() })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Could not ${decision} request.`);
    fields.approvalComment.value = "";
    await loadApprovals();
  } catch (error) {
    showError(error.message);
  }
}

function selectApproval(approval) {
  currentApprovalId = approval.approval_id;
  fields.riskyApprovalState.textContent = `Approved by ${approval.approved_by}. This approval is one-time and only valid for the unchanged configuration.`;
  fields.riskyApprovalState.classList.remove("hidden");
  setConfigState("Approved");
}

function clearSelectedApproval() {
  currentApprovalId = "";
  fields.riskyApprovalState.textContent = "";
  fields.riskyApprovalState.classList.add("hidden");
}

function approvalStatusClass(status) {
  if (status === "approved") return "green";
  if (status === "pending") return "amber";
  return "red";
}

async function loadAudit() {
  if (auth.currentUser?.role !== "admin") return;
  try {
    const response = await fetch("/api/audit");
    const events = await response.json();
    if (!response.ok) throw new Error(events.error || "Could not load audit trail.");
    fields.auditList.innerHTML = "";
    events.slice().reverse().slice(0, 50).forEach((event) => {
      const item = document.createElement("article");
      item.className = "approval-card";
      item.innerHTML = `
        <div class="approval-header">
          <strong>${escapeHtml(event.event_type)}</strong>
          <small>${escapeHtml(event.timestamp)}</small>
        </div>
        <p>${escapeHtml(event.username || "system")} (${escapeHtml(event.role || "n/a")})</p>
        <small>${escapeHtml(JSON.stringify(event.details || {}))}</small>
      `;
      fields.auditList.appendChild(item);
    });
  } catch (error) {
    fields.auditList.innerHTML = "";
    fields.auditList.appendChild(emptyLine(error.message));
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
  if (!auth.currentUser) return;
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
  renderEvidence(payload);
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
    gate: "Release Gate",
    raw: "Raw Results",
    connectors: "Connectors"
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

function renderEvidence(payload) {
  const findings = payload.findings || [];
  const apiFindings = findings.filter((finding) => finding.category === "api");
  const dbFindings = findings.filter((finding) => finding.category === "database");
  const connectors = payload.connector_annotations || {};

  renderSummaryEvidence(apiFindings, dbFindings, connectors);
  renderFailingEndpoints(apiFindings);
  renderDatabaseEvidence(dbFindings);
  renderConnectorEvidence(connectors);
}

function renderSummaryEvidence(apiFindings, dbFindings, connectors) {
  const grid = document.querySelector("#summaryEvidenceGrid");
  grid.innerHTML = "";
  const connectorCount = Object.values(connectors).reduce((sum, items) => sum + (Array.isArray(items) ? items.length : 0), 0);
  grid.appendChild(summaryCard("Failing endpoints", String(apiFindings.length), "Open Evidence for endpoint-specific fixes."));
  grid.appendChild(summaryCard("DB bottlenecks", String(dbFindings.length), "Imported slow queries and plan evidence."));
  grid.appendChild(summaryCard("Observability links", String(connectorCount), "Grafana, traces, and connector status."));
}

function renderFailingEndpoints(apiFindings) {
  const list = document.querySelector("#failingEndpointList");
  list.innerHTML = "";
  if (!apiFindings.length) {
    list.appendChild(emptyLine("No endpoint SLA or error-rate failures detected."));
    return;
  }
  apiFindings.forEach((finding) => {
    list.appendChild(evidenceCard(finding.title, finding.recommendation, finding.evidence || [], finding.solution_steps || []));
  });
}

function renderDatabaseEvidence(dbFindings) {
  const list = document.querySelector("#databaseEvidenceList");
  list.innerHTML = "";
  if (!dbFindings.length) {
    list.appendChild(emptyLine("No database bottleneck evidence imported for this run."));
    return;
  }
  dbFindings.forEach((finding) => {
    list.appendChild(evidenceCard(finding.title, finding.recommendation, finding.evidence || [], finding.solution_steps || []));
  });
}

function renderConnectorEvidence(connectors) {
  const list = document.querySelector("#connectorEvidenceList");
  list.innerHTML = "";
  const rows = [];
  for (const item of connectors.connector_status || []) {
    rows.push({
      title: `${item.name} (${item.type})`,
      body: item.status,
      links: []
    });
  }
  for (const item of connectors.grafana_dashboards || []) {
    rows.push({
      title: `Grafana: ${item.name}`,
      body: "Dashboard linked for the test window and release run.",
      links: [item.url]
    });
  }
  for (const item of connectors.trace_links || []) {
    rows.push({
      title: `Trace correlation: ${item.name}`,
      body: "Trace search uses the run ID so engineers can inspect slow requests.",
      links: [item.url]
    });
  }
  for (const item of connectors.external_connectors || []) {
    rows.push({
      title: `${item.name} (${item.type})`,
      body: item.note,
      links: []
    });
  }
  if (!rows.length) {
    list.appendChild(emptyLine("No observability connectors configured for this run."));
    return;
  }
  rows.forEach((row) => list.appendChild(connectorCard(row)));
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

function summaryCard(label, value, note) {
  const item = document.createElement("div");
  item.className = "summary-card";
  item.innerHTML = `<strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span><p>${escapeHtml(note)}</p>`;
  return item;
}

function evidenceCard(title, recommendation, evidence, solutionSteps) {
  const item = document.createElement("article");
  item.className = "evidence-card";
  item.innerHTML = `
    <h4>${escapeHtml(title)}</h4>
    <p>${escapeHtml(recommendation || "Review the supporting telemetry and validate the suspected bottleneck.")}</p>
    ${detailBlock("Evidence", evidence)}
    ${detailBlock("Relevant solutions", solutionSteps)}
  `;
  return item;
}

function connectorCard(row) {
  const item = document.createElement("article");
  item.className = "evidence-card";
  item.innerHTML = `
    <h4>${escapeHtml(row.title)}</h4>
    <p>${escapeHtml(row.body || "Configured")}</p>
    ${linkBlock("Open", row.links || [])}
  `;
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
      ${aiRcaBlock(finding)}
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

function aiRcaBlock(finding) {
  if (!finding.ai_rca_summary && !finding.ai_confidence_pct) return "";
  return `
    <section class="ai-rca-block">
      <h4>AI RCA</h4>
      <ul>
        <li>Template: ${escapeHtml(finding.ai_prompt_template || "not recorded")}</li>
        <li>Confidence: ${escapeHtml(String(Number(finding.ai_confidence_pct || 0).toFixed(1)))}%</li>
        <li>${escapeHtml(finding.ai_rca_summary || "No AI RCA available.")}</li>
        <li>Recommendation: ${escapeHtml(finding.ai_recommendation || finding.recommendation || "")}</li>
        <li>Validation: ${escapeHtml(finding.ai_validation_plan || finding.validation_plan || "")}</li>
      </ul>
      ${detailBlock("AI evidence citations", finding.ai_evidence_citations || [])}
      ${detailBlock("Guardrail notes", finding.ai_guardrail_failures || [])}
    </section>
  `;
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
  const canTest = auth.currentUser && ["tester", "approver", "admin"].includes(auth.currentUser.role);
  document.querySelector("#runBtn").disabled = isRunning || !canTest;
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
