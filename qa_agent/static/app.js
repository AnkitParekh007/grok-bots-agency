import {
  $,
  esc,
  icon,
  icons,
  api,
  update,
  empty,
  status,
  date,
  markdown,
  fileURL,
  announce,
} from "./ui.js";
import {
  providerName,
  usable,
  welcome,
  conversation,
  criteria,
  cases,
  gaps,
  planPanel,
  executionPanel,
  evidencePanel,
  modelCard,
  approvalRecord,
  executionEnvelope,
} from "./components.js";
const state = {
  me: null,
  cfg: null,
  models: [],
  projects: [],
  envs: [],
  runs: [],
  run: null,
  current: null,
  section: "session",
  tab: "context",
  timer: null,
  epoch: 0,
  review: null,
  sheet: null,
  sheetReturn: null,
  busy: false,
  errors: new Map(),
  connections: [],
  discoveries: new Map(),
};
const nav = [
  ["sessions", "Sessions", "sessions"],
  ["projects", "Projects", "projects"],
  ["runs", "Test Runs", "runs"],
  ["reports", "Reports", "reports"],
  ["models", "Models", "cpu"],
  ["settings", "Settings", "settings"],
];
const terminal = new Set([
  "completed",
  "failed",
  "rejected",
  "blocked",
  "interrupted",
  "cancelled",
]);
const selected = () =>
  $("#model-choice").value === ""
    ? undefined
    : state.models[Number($("#model-choice").value)];
const canRequest = () =>
  state.me?.mode === "demo" ||
  ["requester", "approver"].includes(state.me?.role);
const canCancel = (r) =>
  canRequest() &&
  !terminal.has(r.status) &&
  (state.me.mode === "demo" ||
    state.me.role === "approver" ||
    r.requested_by === state.me.user);
const canReview = (r) =>
  r.status === "awaiting_approval" &&
  (state.me.mode === "demo" ||
    (state.me.role === "approver" && r.requested_by !== state.me.user));
function notice(error) {
  if (error.status === 401) {
    lock();
    return;
  }
  $("#notice").hidden = false;
  $("#notice").innerHTML =
    "<span>" +
    esc(
      error.message ||
        "The request failed. Check the connection and try again.",
    ) +
    '</span><button data-action="refresh">Refresh workspace</button>';
  announce(error.message);
}
async function attempt(fn) {
  try {
    await fn();
  } catch (e) {
    notice(e);
  }
}
function setBusy(busy) {
  state.busy = busy;
  $("#send").disabled = busy || !canRequest() || !usable(selected());
  $("#msg").disabled = !canRequest();
  $("#send").setAttribute(
    "aria-label",
    busy ? "Submitting request" : "Send QA request",
  );
}
function account() {
  const me = state.me;
  $("#auth-status").textContent =
    me.mode === "demo" ? "Demo · unauthenticated" : me.role;
  $("#auth").innerHTML =
    '<span class="avatar">' +
    esc((me.user || "D").slice(0, 1).toUpperCase()) +
    '</span><span class="nav-label">' +
    esc(me.user || "Local demo") +
    "<small>" +
    esc(me.mode === "demo" ? "Unauthenticated identity" : me.role) +
    "</small></span>" +
    (me.authenticated
      ? '<button data-action="logout" aria-label="Sign out">' +
        icon("logout") +
        "</button>"
      : "");
}
function lock() {
  clearTimeout(state.timer);
  state.epoch++;
  state.current = null;
  state.run = null;
  state.review = null;
  state.runs = [];
  state.models = [];
  state.projects = [];
  state.connections = [];
  state.discoveries.clear();
  state.envs = [];
  state.cfg = null;
  state.section = "session";
  state.me = { mode: state.me?.mode || "local", authenticated: false };
  closeSheets(false);
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  $("#conversation").hidden = false;
  $("#page-view").hidden = true;
  $("#composer-area").hidden = false;
  ["proj", "story", "repo", "env"].forEach(
    (id) => ($("#" + id).innerHTML = ""),
  );
  $("#story-preview").textContent = "";
  $("#context-model").textContent = "";
  $("#composer-model").textContent = "Sign in required";
  $("#msg").value = "";
  $("#auth").textContent = "Signed out";
  $("#auth-status").textContent = "Sign in required";
  document
    .querySelectorAll("[data-html]")
    .forEach((el) => delete el.dataset.html);
  $("#review-content").innerHTML = "";
  $("#review-identity").innerHTML = "";
  $("#review-comment").value = "";
  $("#review-error").textContent = "";
  $("#notice").hidden = true;
  $("#notice").innerHTML = "";
  $("#evidence-image").removeAttribute("src");
  $("#page-view").innerHTML = "";
  $("#model-choice").innerHTML = "";
  $("#chat").innerHTML = empty(
    "Sign in to continue",
    "Use your local access token to open the workspace.",
  );
  [
    "run-context",
    "plan-content",
    "execution-content",
    "evidence-content",
    "runs",
  ].forEach((id) => ($("#" + id).innerHTML = ""));
  $("#send").disabled = true;
  $("#msg").disabled = true;
  const sso = state.me?.mode === "oidc";
  $("#tok").hidden = sso;
  $("#tok").required = !sso;
  document.querySelector('label[for="tok"]').hidden = sso;
  $("#login-help").textContent = sso ? "Continue with your organization's identity provider." : "Sign in with your local access token.";
  $("#login").textContent = sso ? "Continue with organization sign-in" : "Sign in";
  $("#login-session-note").textContent = sso ? "Your organization controls sign-in and access. This workspace uses a revocable session." : "Your access token is exchanged for a revocable session. It is never saved in browser storage.";
  $("#login-dialog").showModal();
}
async function boot() {
  const epoch = ++state.epoch;
  state.me = await api("/api/me");
  if (state.me.mode !== "demo" && !state.me.authenticated) {
    lock();
    return;
  }
  const [cfg, envs, projects, models, runs, connections] = await Promise.all(
    [
      "/api/config",
      "/api/environments",
      "/api/projects",
      "/api/models",
      "/api/runs",
      "/api/connections",
    ].map((u) => api(u)),
  );
  if (epoch !== state.epoch) return;
  state.cfg = cfg;
  state.envs = envs;
  state.projects = projects;
  state.models = models;
  state.runs = runs;
  state.connections = connections;
  state.discoveries.clear();
  $("#notice").hidden = true;
  account();
  $("#proj").innerHTML =
    '<option value="">No project selected</option>' +
    projects
      .map(
        (p) =>
          '<option value="' +
          esc(p.key) +
          '">' +
          esc(p.key + " · " + p.name) +
          "</option>",
      )
      .join("");
  $("#env").innerHTML = cfg.environments
    .map((e) => "<option>" + esc(e) + "</option>")
    .join("");
  $("#story").innerHTML = '<option value="">Identify from message</option>';
  $("#repo").innerHTML = '<option value="">No repository selected</option>';
  fillModels();
  renderNav();
  renderRecent();
  setBusy(false);
  if (state.current) await refresh();
  else render();
}
function fillModels() {
  const old = selected();
  const previous = old ? old.provider + "|" + old.model : null;
  $("#model-choice").innerHTML =
    '<option value="">Choose an approved model</option>' +
    state.models
      .map(
        (m, i) =>
          '<option value="' +
          i +
          '" ' +
          (!usable(m) ? "disabled" : "") +
          ">" +
          esc(providerName(m.provider) + " / " + m.model) +
          (m.provider === "nvidia"
            ? " · unsupported"
            : !m.permitted
              ? " · not permitted"
              : m.available === false
                ? " · unavailable"
                : "") +
          "</option>",
      )
      .join("");
  const chosen = state.models.findIndex((m) =>
    previous
      ? m.provider + "|" + m.model === previous
      : m.provider === state.cfg.model_provider,
  );
  $("#model-choice").value = chosen >= 0 ? String(chosen) : "";
  modelChanged();
}
function modelChanged() {
  const m = selected();
  $("#composer-model").textContent = m
    ? providerName(m.provider) + " · " + m.model
    : "Select a model";
  $("#context-model").innerHTML = m
    ? "<strong>" +
      esc(
        m.provider === "replay"
          ? "Recorded replay"
          : m.classification === "local"
            ? "Local inference"
            : "Hosted inference",
      ) +
      "</strong><br>" +
      esc(
        m.provider === "replay"
          ? "Uses saved plans; no real model reasoning."
          : m.available === false
            ? "Unavailable. Check the connection or select another permitted model."
            : m.health === "not_checked"
              ? "Connection has not been checked."
              : m.health.replaceAll("_", " "),
      ) +
      "<br>Approval: " +
      (m.approved ? "approved" : "not approved") +
      " · Context: " +
      esc(m.context_window ?? "unknown")
    : "Choose an administrator-permitted model.";
  setBusy(state.busy);
  if (!state.current && state.section === "session") render();
}
function renderNav() {
  $("#navigation").innerHTML = nav
    .map(
      ([id, name, ico]) =>
        '<button class="nav-button" data-action="' +
        id +
        '" aria-label="' +
        name +
        '" title="' +
        name +
        '" ' +
        (state.section === id ||
        (id === "sessions" && state.section === "session" && state.current)
          ? 'aria-current="page"'
          : "") +
        ">" +
        icon(ico) +
        '<span class="nav-label">' +
        name +
        "</span></button>",
    )
    .join("");
}
function renderRecent() {
  $("#runs").innerHTML = state.runs.length
    ? state.runs
        .slice(0, 8)
        .map(
          (r) =>
            '<button class="run-item ' +
            (r.id === state.current ? "active" : "") +
            '" data-action="open-run" data-run="' +
            esc(r.id) +
            '" title="' +
            esc(r.request) +
            '"><strong>' +
            esc(r.story_key || "QA session") +
            " · " +
            esc(r.request) +
            "</strong><small>" +
            esc(r.status.replaceAll("_", " ")) +
            " · " +
            date(r.created) +
            "</small></button>",
        )
        .join("")
    : '<p class="small muted nav-label" style="padding:0 12px">No sessions yet</p>';
}
function render() {
  const isSession = state.section === "session";
  $("#msg").placeholder = state.current
    ? "Start another QA request…"
    : "Describe what you want to test…";
  $(".composer-hint span").textContent = state.current
    ? "A new request creates a new session."
    : "Review every plan before execution.";
  $("#conversation").hidden = !isSession;
  $("#page-view").hidden = isSession;
  $("#composer-area").hidden = !isSession;
  $("#page-title").textContent = isSession
    ? state.run?.story_key || "New session"
    : nav.find((n) => n[0] === state.section)?.[1] || "Workspace";
  renderNav();
  if (isSession) {
    update(
      $("#chat"),
      state.run
        ? conversation(state.run, canCancel(state.run))
        : welcome(
            selected(),
            $("#env").value,
            canRequest(),
            state.runs.some((r) =>
              ["completed", "executing"].includes(r.status),
            ),
          ),
    );
  } else renderPage();
  renderPanels();
}
function renderPanels() {
  const r = state.run;
  update(
    $("#run-context"),
    r
      ? '<div class="context-snapshot"><div class="eyebrow">This run</div><dl>' +
          [
            ["Project", r.project || "No project"],
            ["Story", r.story_key || "Resolving"],
            ["Repository", r.repository || "None selected"],
            ["Environment", r.environment],
            [
              "Model",
              r.model_provider + " / " + (r.model_id || "Not reported"),
            ],
            [
              "Data classification",
              r.events
                .find((e) => e.step === "data_class")
                ?.message.split(": ")
                .pop() || "Not reported",
            ],
          ]
            .map(([k, v]) => "<dt>" + k + "</dt><dd>" + esc(v) + "</dd>")
            .join("") +
          "</dl></div>"
      : "",
  );
  update($("#plan-content"), planPanel(r));
  update($("#execution-content"), executionPanel(r));
  update($("#evidence-content"), evidencePanel(r));
}
function renderPage() {
  const heading =
    '<div class="page-heading"><div><h1>' +
    esc(nav.find((n) => n[0] === state.section)?.[1]) +
    "</h1><p>" +
    esc(
      {
        sessions:
          "Your saved QA requests. Reopen a session to continue reviewing its run.",
        runs: "Follow planning, approval, execution, and recorded outcomes.",
        reports: "Open reports and evidence from saved executions.",
        projects:
          "Choose a project and its linked synthetic stories and repositories.",
        models:
          "Select a permitted model for your next request. Availability is checked on demand.",
        settings: "Workspace preferences and server-managed limits.",
      }[state.section],
    ) +
    "</p></div>" +
    (state.section === "models"
      ? '<button class="button" data-action="refresh-models">Refresh status</button>'
      : "") +
    "</div>";
  let body = "";
  if (["sessions", "runs", "reports"].includes(state.section)) {
    const rs =
      state.section === "reports"
        ? state.runs.filter((r) =>
            ["completed", "cancelled"].includes(r.status),
          )
        : state.runs;
    body = rs.length
      ? rs
          .map(
            (r) =>
              '<article class="list-card"><div class="list-content">' +
              status(r.status) +
              "<h2>" +
              esc(r.story_key || "QA session") +
              "</h2><p>" +
              esc(r.request) +
              "</p><p>" +
              date(r.created) +
              " · " +
              esc(r.environment) +
              '</p></div><button class="button" data-action="open-run" data-run="' +
              esc(r.id) +
              '" ' +
              (state.section === "reports"
                ? 'data-tab-target="evidence"'
                : "") +
              ">Open " +
              (state.section === "reports" ? "evidence" : "session") +
              "</button></article>",
          )
          .join("")
      : empty(
          "Nothing here yet",
          state.section === "reports"
            ? "Reports become available after an approved execution."
            : "Start a new QA session to generate a test plan.",
        );
  }
  if (state.section === "projects")
    body =
      state.projects
        .map(
          (p) =>
            '<article class="list-card"><div class="list-content"><span class="chip">' +
            esc(p.key) +
            "</span><h2>" +
            esc(p.name) +
            "</h2><p>" +
            p.repositories.length +
            " linked repositories · " +
            esc(p.tracker) +
            '</p></div><button class="button" data-action="choose-project" data-project="' +
            esc(p.key) +
            '">Use project</button></article>',
        )
        .join("") ||
      empty(
        "No projects configured",
        "Ask an administrator to configure a permitted project.",
      );
  if (state.section === "models")
    body =
      '<section id="model-settings" class="model-grid">' +
      state.models
        .map(
          (m, i) =>
            modelCard(m, i, selected() === m, canRequest()) +
            (state.errors.has(i)
              ? '<p class="callout" role="alert">' +
                esc(state.errors.get(i)) +
                "</p>"
              : ""),
        )
        .join("") +
      "</section>";
  if (state.section === "settings")
    body =
      '<section class="settings-card"><h2>Appearance</h2><p>Choose the theme that works for your environment. Reduced-motion preferences are respected.</p><button class="button" data-action="theme">Switch to ' +
      (document.documentElement.dataset.theme === "dark" ? "light" : "dark") +
      ' theme</button></section><section class="settings-card"><h2>Access & approval</h2><p>' +
      esc(
        state.me.mode === "demo"
          ? "Demo mode uses unauthenticated reviewer labels. Use local authenticated mode for real evaluation."
          : "Signed in as " +
              (state.me.display_name || state.me.user) +
              " · " +
              state.me.role +
              ". The backend enforces roles, ownership, expiry, and exact-plan approval.",
      ) +
      "</p>" +
      (state.me.authenticated
        ? '<button class="button" data-action="logout">Sign out</button>'
        : "") +
      '</section><section class="settings-card"><h2>Budget & registered skills</h2><p>Per run: $' +
      esc(state.cfg.budget.per_run_usd) +
      " · Per day: $" +
      esc(state.cfg.budget.daily_usd) +
      "</p><p>" +
      state.cfg.skills.map((s) => esc(s.name)).join(" · ") +
      '</p><p class="small">Provider credentials, policy changes, and integration configuration are managed by the server administrator.</p></section>';
  if (state.section === "settings")
    body += connectionSettings();
  update($("#page-view"), heading + body);
}
function connectionSettings() {
  if (!state.connections.length) return '<section class="settings-card"><h2>Organization connections</h2><p>No connections are assigned to you for administration. An administrator configures the Jira or Bitbucket destination and server-side token or MCP credentials, then grants connection access.</p></section>';
  return state.connections.map((c) => {
    const d = state.discoveries.get(c.id);
    const selected = new Set(c.selected_resources);
    return '<section class="settings-card" data-connection-card="' + esc(c.id) + '"><h2>' + esc(c.label) + '</h2><p>' + esc(c.target) + ' · ' + esc(c.route === 'mcp' ? 'MCP' : 'Token') + '</p><p>Selected resources: ' + esc(c.selected_resources.join(', ') || 'None') + '</p><p class="small">Discovery reads resource names using the configured credential. Saving a selection grants this connection access only to those resources. Project memberships and model-data approvals apply separately.</p><button class="button" data-action="discover-connection" data-connection="' + esc(c.id) + '" ' + (!c.enabled || !c.discovery_approved ? 'disabled' : '') + '>Discover available resources</button>' +
      (c.revision ? '<button class="button" data-action="revoke-connection-scope" data-connection="' + esc(c.id) + '">Revoke resource access</button>' : '') +
      (d ? '<fieldset><legend>Choose resources for this connection</legend>' + (!d.complete ? '<p role="alert">Discovery is incomplete. Ask an administrator to adjust the approved discovery limits before saving a selection.</p>' : '') +
        d.resources.map((r) => '<label class="connection-resource"><input type="checkbox" data-connection-resource="' + esc(c.id) + '" value="' + esc(r.id) + '" ' + (selected.has(r.id) ? 'checked' : '') + '> ' + esc(r.name) + ' (' + esc(r.id) + ')</label>').join('') +
        '<button class="button" data-action="save-connection-scope" data-connection="' + esc(c.id) + '" ' + (!d.complete || !d.resources.length ? 'disabled' : '') + '>Save selected access</button></fieldset>' : '') + '</section>';
  }).join('');
}
async function refresh() {
  clearTimeout(state.timer);
  const rid = state.current,
    epoch = state.epoch;
  if (!rid) return;
  try {
    const r = await api("/api/runs/" + encodeURIComponent(rid));
    if (rid !== state.current || epoch !== state.epoch) return;
    const changed = state.run?.status !== r.status;
    state.run = r;
    if (changed)
      announce((r.story_key || r.id) + ": " + r.status.replaceAll("_", " "));
    const rs = await api("/api/runs");
    if (rid !== state.current || epoch !== state.epoch) return;
    state.runs = rs;
    renderRecent();
    render();
    if (!terminal.has(r.status)) state.timer = setTimeout(refresh, 1400);
  } catch (e) {
    if (rid !== state.current || epoch !== state.epoch) return;
    notice(e);
    if (e.status !== 401) state.timer = setTimeout(refresh, 5000);
  }
}
async function openRun(rid) {
  clearTimeout(state.timer);
  state.current = rid;
  state.run = null;
  state.section = "session";
  closeSheets(false);
  $("#chat").innerHTML = empty(
    "Opening session",
    "Loading the saved run from the workspace.",
  );
  await refresh();
}
function newSession() {
  clearTimeout(state.timer);
  state.current = null;
  state.run = null;
  state.section = "session";
  closeSheets(false);
  $("#msg").value = "";
  $("#notice").hidden = true;
  render();
  $("#msg").focus();
}
function navigate(section) {
  state.section = section;
  closeSheets(false);
  render();
  $("#page-view").focus({ preventScroll: true });
}
function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const active = b.dataset.tab === tab;
    b.setAttribute("aria-selected", String(active));
    b.tabIndex = active ? 0 : -1;
    $("#pane-" + b.dataset.tab).hidden = !active;
  });
}
function closeSheets(restore = true) {
  const target = state.sheetReturn;
  state.sheet = null;
  state.sheetReturn = null;
  $("#shell").classList.remove("nav-open");
  if (innerWidth < 1440) $("#shell").classList.add("context-hidden");
  $("#sheet-backdrop").hidden = true;
  [$("#main"), $("#sidebar"), $("#context-panel")].forEach((el) => {
    el.inert = false;
    el.removeAttribute("aria-modal");
    el.removeAttribute("role");
  });
  $('[data-action="nav"]').setAttribute("aria-expanded", "false");
  $("#context-toggle").setAttribute(
    "aria-expanded",
    String(!$("#shell").classList.contains("context-hidden")),
  );
  if (restore && target?.isConnected) target.focus({ preventScroll: true });
}
function openSheet(kind) {
  const target = document.activeElement;
  closeSheets(false);
  state.sheetReturn = target;
  if (kind === "nav") {
    $("#shell").classList.add("nav-open");
    state.sheet = "nav";
    $("#main").inert = true;
    $("#context-panel").inert = true;
    $('[data-action="nav"]').setAttribute("aria-expanded", "true");
  } else {
    $("#shell").classList.remove("context-hidden");
    $("#context-toggle").setAttribute("aria-expanded", "true");
    if (innerWidth >= 1440) return;
    state.sheet = "context";
    $("#main").inert = true;
    $("#sidebar").inert = true;
  }
  const sheet = kind === "nav" ? $("#sidebar") : $("#context-panel");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  $("#sheet-backdrop").hidden = false;
  sheet.querySelector("button").focus();
}
async function projectChanged() {
  const key = $("#proj").value;
  const epoch = state.epoch;
  $("#story").innerHTML = '<option value="">Identify from message</option>';
  $("#repo").innerHTML = '<option value="">No repository selected</option>';
  $("#story-preview").textContent = "";
  if (!key) return;
  const [stories, repos] = await Promise.all([
    api("/api/projects/" + encodeURIComponent(key) + "/stories"),
    api("/api/projects/" + encodeURIComponent(key) + "/repositories"),
  ]);
  if (key !== $("#proj").value || epoch !== state.epoch) return;
  $("#story").innerHTML += stories
    .map(
      (s) =>
        '<option value="' +
        esc(s.key) +
        '">' +
        esc(s.key + " · " + s.summary) +
        "</option>",
    )
    .join("");
  $("#repo").innerHTML += repos
    .map((r) => "<option>" + esc(r.full_name) + "</option>")
    .join("");
}
async function storyChanged() {
  const project = $("#proj").value,
    key = $("#story").value;
  if (!project || !key) {
    $("#story-preview").textContent = "";
    return;
  }
  const s = await api(
    "/api/projects/" +
      encodeURIComponent(project) +
      "/stories/" +
      encodeURIComponent(key),
  );
  if (key !== $("#story").value) return;
  $("#story-preview").textContent = s.summary;
}
async function submit(event) {
  event.preventDefault();
  if (state.busy || !canRequest() || !usable(selected())) return;
  const message = $("#msg").value.trim();
  if (!message) {
    $("#msg").focus();
    return;
  }
  setBusy(true);
  $("#notice").hidden = true;
  const m = selected();
  try {
    const r = await api("/api/chat", {
      message,
      environment: $("#env").value,
      project: $("#proj").value || null,
      story_key: $("#story").value || null,
      repository: $("#repo").value || null,
      model_provider: m.provider,
      model_id: m.model,
    });
    $("#msg").value = "";
    await openRun(r.run_id);
  } finally {
    setBusy(false);
  }
}
async function showReview() {
  if (!state.current) return;
  state.reviewReturn = document.activeElement;
  const r = await api("/api/runs/" + encodeURIComponent(state.current));
  state.run = r;
  state.review = { id: r.id, hash: r.plan_hash, status: r.status };
  closeSheets(false);
  const p = r.planning_json;
  if (!p) {
    notice(new Error("This run has no validated plan to review."));
    return;
  }
  $("#review-content").innerHTML =
    '<p class="story-summary">' +
    esc(r.story_json?.fields.summary || r.story_key) +
    '</p><div class="welcome-context">' +
    status(r.status) +
    '<span class="chip">' +
    esc(r.environment) +
    '</span><span class="chip">' +
    esc(r.model_label) +
    "</span></div>" +
    approvalRecord(r) +
    '<div class="callout">' +
    p.coverage_gaps.length +
    " coverage gap(s). Review unsupported or ambiguous requirements before authorizing execution.</div><h2>Acceptance criteria & source quotations</h2>" +
    criteria(r) +
    "<h2>Behavior coverage</h2>" +
    p.behaviors
      .map(
        (b) =>
          '<div class="criterion"><strong>' +
          esc(b.id) +
          " · " +
          esc(b.ac_refs.join(", ")) +
          "</strong><p>" +
          esc(b.description || b.text || b.title || "") +
          "</p></div>",
      )
      .join("") +
    "<h2>Coverage gaps</h2>" +
    gaps(r) +
    "<h2>Proposed test cases</h2>" +
    cases(r, true) +
    executionEnvelope(r) +
    '<p class="small muted">' +
    esc(r.id) +
    " · Created " +
    date(r.plan_created_at) +
    ". The server checks expiry and every bound execution input at approval and dispatch.</p>";
  $("#review-identity").innerHTML =
    state.me.mode === "demo"
      ? '<div class="callout">Demo review · reviewer identity is NOT authenticated.</div><label for="who">Reviewer label</label><input id="who" required placeholder="Your reviewer label">'
      : '<p class="small">Signed in as: <strong>' +
        esc(state.me.user) +
        "</strong> · " +
        esc(state.me.role) +
        "</p>";
  $("#review-identity").insertAdjacentHTML(
    "beforeend",
    '<span class="small muted">Execution envelope · SHA-256</span><code id="review-hash" class="hash" aria-label="Execution envelope hash">' +
      esc(r.plan_hash) +
      "</code>",
  );
  if (state.me.mode === "demo") $("#who").value = "demo.reviewer";
  $("#approve").disabled = !canReview(r);
  $("#reject").disabled = !canReview(r);
  $("#review-comment").value = "";
  $("#review-error").textContent = canReview(r)
    ? ""
    : r.status !== "awaiting_approval"
      ? "This plan is no longer awaiting approval."
      : state.me.user === r.requested_by
        ? "The requester cannot approve their own run. Sign in as a distinct approver."
        : "An authenticated approver role is required.";
  $("#review-dialog").showModal();
}
async function decide(decision) {
  const snapshot = state.review;
  if (!snapshot) return;
  $("#approve").disabled = true;
  $("#reject").disabled = true;
  $("#review-error").textContent = "";
  try {
    const latest = await api("/api/runs/" + encodeURIComponent(snapshot.id));
    if (
      latest.plan_hash !== snapshot.hash ||
      latest.status !== "awaiting_approval"
    )
      throw new Error(
        "The run changed or was already reviewed. Close and reopen the plan; no approval was sent.",
      );
    await api("/api/runs/" + encodeURIComponent(snapshot.id) + "/approval", {
      approver: state.me.mode === "demo" ? $("#who").value : "",
      decision,
      comment: $("#review-comment").value,
      plan_hash: snapshot.hash,
    });
    $("#review-dialog").close();
    announce(
      decision === "approved"
        ? "Exact plan approved. Execution requested."
        : "Plan rejected.",
    );
    setTab(decision === "approved" ? "execution" : "plan");
    await refresh();
  } catch (e) {
    $("#review-error").textContent = e.message;
  } finally {
    if ($("#review-dialog").open) {
      $("#approve").disabled = !canReview(state.run);
      $("#reject").disabled = !canReview(state.run);
    }
  }
}
let modelRefresh = 0;
async function refreshModels() {
  const revision = ++modelRefresh;
  const epoch = state.epoch;
  const records = await api("/api/models");
  if (revision !== modelRefresh || epoch !== state.epoch) return;
  const previous = selected();
  state.models = records;
  $("#model-choice").innerHTML =
    '<option value="">Choose an approved model</option>' +
    state.models
      .map(
        (m, i) =>
          '<option value="' +
          i +
          '" ' +
          (!usable(m) ? "disabled" : "") +
          ">" +
          esc(providerName(m.provider) + " / " + m.model) +
          (usable(m) ? "" : " · unavailable / not permitted") +
          "</option>",
      )
      .join("");
  const index = state.models.findIndex(
    (m) => m.provider === previous?.provider && m.model === previous?.model,
  );
  $("#model-choice").value = index >= 0 ? String(index) : "";
  modelChanged();
  if (state.section === "models") renderPage();
}
async function probe(index, button) {
  const m = state.models[index];
  if (!m?.permitted || !canRequest()) return;
  button.disabled = true;
  button.textContent = "Checking…";
  try {
    const result = await api(
      "/api/models/" +
        encodeURIComponent(m.provider) +
        "/probe?model_id=" +
        encodeURIComponent(m.model),
      {},
    );
    if (result.error) state.errors.set(index, result.error);
    else state.errors.delete(index);
    await refreshModels();
    announce(
      result.available ? "Model connection available" : "Model unavailable",
    );
  } finally {
    button.disabled = false;
    button.textContent = "Check connection";
  }
}
async function handleAction(button) {
  const action = button.dataset.action;
  if (
    state.me?.mode === "local" &&
    !state.me.authenticated &&
    !["theme", "refresh"].includes(action)
  )
    return;
  if (nav.some((n) => n[0] === action)) {
    navigate(action);
    return;
  }
  if (action === "new") {
    newSession();
    return;
  }
  if (action === "refresh") {
    await boot();
    return;
  }
  if (action === "nav") {
    openSheet("nav");
    return;
  }
  if (action === "close-nav" || action === "close-context") {
    const useFallback = action === "close-context" && !state.sheetReturn;
    if (innerWidth >= 1440) $("#shell").classList.add("context-hidden");
    closeSheets();
    if (useFallback) $("#context-toggle").focus({ preventScroll: true });
    return;
  }
  if (action === "context") {
    if (!$("#shell").classList.contains("context-hidden")) {
      $("#shell").classList.add("context-hidden");
      closeSheets();
    } else openSheet("context");
    return;
  }
  if (action === "collapse") {
    $("#shell").classList.toggle("rail");
    button.setAttribute(
      "aria-expanded",
      String(!$("#shell").classList.contains("rail")),
    );
    button.setAttribute(
      "aria-label",
      $("#shell").classList.contains("rail")
        ? "Expand sidebar"
        : "Collapse sidebar",
    );
    return;
  }
  if (action === "theme") {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("qa-theme", next);
    } catch (_) {}
    if (state.section === "settings") renderPage();
    announce(next + " theme");
    return;
  }
  if (action === "logout") {
    await api("/api/logout", {});
    lock();
    return;
  }
  if (action === "discover-connection") {
    const epoch = state.epoch;
    button.disabled = true;
    try {
      const result = await api('/api/connections/' + encodeURIComponent(button.dataset.connection) + '/discover', {});
      if (epoch !== state.epoch) return;
      state.discoveries.set(button.dataset.connection, result);
      renderPage();
      announce(result.resources.length + ' resources discovered. Review the selection before saving access.');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
    return;
  }
  if (action === "save-connection-scope") {
    const epoch = state.epoch;
    const id = button.dataset.connection, discovery = state.discoveries.get(id);
    const resources = Array.from(document.querySelectorAll('[data-connection-resource]')).filter((el) => el.dataset.connectionResource === id && el.checked).map((el) => el.value);
    if (!resources.length) { announce('Select at least one resource.'); return; }
    button.disabled = true;
    try {
      await api('/api/connections/' + encodeURIComponent(id) + '/scope', {receipt: discovery.receipt, resources, previous_revision: discovery.scope_revision});
      const connections = await api('/api/connections');
      if (epoch !== state.epoch) return;
      state.connections = connections;
      state.discoveries.delete(id);
      renderPage();
      announce('Connection resource access saved.');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
    return;
  }
  if (action === "revoke-connection-scope") {
    const epoch = state.epoch, id = button.dataset.connection;
    const connection = state.connections.find((c) => c.id === id);
    button.disabled = true;
    try {
      await api('/api/connections/' + encodeURIComponent(id) + '/scope', {previous_revision: connection.revision}, 'DELETE');
      const connections = await api('/api/connections');
      if (epoch !== state.epoch) return;
      state.connections = connections;
      state.discoveries.delete(id);
      renderPage();
      announce('Connection resource access revoked.');
    } finally {
      if (button.isConnected) button.disabled = false;
    }
    return;
  }
  if (action === "open-run") {
    await openRun(button.dataset.run);
    if (button.dataset.tabTarget) {
      setTab(button.dataset.tabTarget);
      openSheet("context");
    }
    return;
  }
  if (action === "choose-project") {
    newSession();
    $("#proj").value = button.dataset.project;
    await projectChanged();
    setTab("context");
    openSheet("context");
    $("#story").focus();
    return;
  }
  if (action === "suggest-analyze" || action === "suggest-plan") {
    $("#msg").value =
      action === "suggest-analyze"
        ? "Analyze the Jira story, identify acceptance criteria and coverage gaps, and propose tests for review."
        : "Generate test cases for the selected story using approved skills and oracles, then wait for review.";
    setTab("context");
    openSheet("context");
    $("#proj").focus();
    return;
  }
  if (action === "choose-model") {
    $("#model-choice").value = button.dataset.model;
    modelChanged();
    renderPage();
    announce("Model selected for new requests. Existing runs are unchanged.");
    return;
  }
  if (action === "refresh-models") {
    await refreshModels();
    return;
  }
  if (action === "probe") {
    await probe(Number(button.dataset.model), button);
    return;
  }
  if (action === "review") {
    await showReview();
    return;
  }
  if (action === "close-review") {
    $("#review-dialog").close();
    return;
  }
  if (action === "cancel" && state.current) {
    button.disabled = true;
    try {
      await api(
        "/api/runs/" + encodeURIComponent(state.current) + "/cancel",
        {},
      );
      await refresh();
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "retry") {
    const request = state.run?.request || "";
    newSession();
    $("#msg").value = request;
    return;
  }
  if (action === "evidence") {
    setTab("evidence");
    openSheet("context");
    return;
  }
  if (action === "screenshot") {
    const result = state.run?.results.find(
      (r) => r.id === button.dataset.result,
    );
    if (!result?.screenshot) return;
    state.evidenceReturn = document.activeElement;
    closeSheets(false);
    $("#evidence-image").src = fileURL(state.run.id, result.screenshot);
    $("#evidence-image").alt = result.id + " · " + result.title;
    $("#evidence-caption").textContent = result.id + " · " + result.title;
    $("#evidence-dialog").showModal();
    return;
  }
  if (action === "close-evidence") {
    $("#evidence-dialog").close();
    return;
  }
  if (action === "steps") {
    const result = state.run?.results.find(
      (r) => r.id === button.dataset.result,
    );
    if (!result?.log) return;
    const response = await fetch(fileURL(state.run.id, result.log));
    if (!response.ok)
      throw new Error("Could not load test steps (" + response.status + ").");
    const pre = document.createElement("pre");
    pre.className = "step-data";
    pre.textContent = await response.text();
    const target = document.getElementById("steps-" + result.id);
    target.replaceChildren(pre);
    return;
  }
  if (action === "copy-answer" || action === "copy-code") {
    const text =
      action === "copy-answer"
        ? state.run?.planning_json?.understanding.summary ||
          state.run?.error ||
          state.run?.status
        : button.previousElementSibling.textContent;
    await navigator.clipboard.writeText(text || "");
    announce("Copied to clipboard.");
    return;
  }
}
$("#composer").addEventListener("submit", (e) => attempt(() => submit(e)));
$("#review-dialog").addEventListener("close", () => {
  if ($("#login-dialog").open) return;
  const target = state.reviewReturn?.getClientRects().length
    ? state.reviewReturn
    : $("#chat [data-action=review]") || $("#context-toggle");
  target?.focus({ preventScroll: true });
  state.reviewReturn = null;
});
$("#evidence-dialog").addEventListener("close", () => {
  if ($("#login-dialog").open) return;
  const target = state.evidenceReturn?.getClientRects().length
    ? state.evidenceReturn
    : $("#context-toggle");
  target?.focus({ preventScroll: true });
  state.evidenceReturn = null;
});
$("#msg").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#proj").addEventListener("change", () => attempt(projectChanged));
$("#story").addEventListener("change", () => attempt(storyChanged));
$("#env").addEventListener("change", () => {
  if (!state.current && state.section === "session") render();
});
$("#model-choice").addEventListener("change", modelChanged);
$("#review-form").addEventListener("submit", (e) => {
  e.preventDefault();
  attempt(() => decide("approved"));
});
$("#reject").addEventListener("click", () => attempt(() => decide("rejected")));
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login").disabled = true;
  try {
    if (state.me?.mode === "oidc") {
      window.location.assign("/api/oidc/start");
      return;
    }
    await api("/api/login", { token: $("#tok").value });
    $("#tok").value = "";
    $("#login-error").textContent = "";
    $("#login-dialog").close();
    await boot();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    $("#login").disabled = false;
  }
});
$("#login-dialog").addEventListener("cancel", () => {
  $("#notice").hidden = false;
  $("#notice").innerHTML =
    '<span>Sign in is required to access this workspace.</span><button data-action="refresh">Sign in</button>';
});
$("#sheet-backdrop").addEventListener("click", () => closeSheets());
// Delegation keeps polling updates free of inline handlers and untrusted script execution.
document.addEventListener("click", (e) => {
  const button = e.target.closest("[data-action]");
  if (button && !button.disabled) attempt(() => handleAction(button));
  const tab = e.target.closest("[data-tab]");
  if (tab) setTab(tab.dataset.tab);
});
document.addEventListener("keydown", (e) => {
  const tab = e.target.closest("[data-tab]");
  if (tab && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
    e.preventDefault();
    const tabs = [...document.querySelectorAll("[data-tab]")];
    let index = tabs.indexOf(tab);
    index =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? tabs.length - 1
          : (index + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) %
            tabs.length;
    setTab(tabs[index].dataset.tab);
    tabs[index].focus();
  }
  if (!state.sheet || document.querySelector("dialog[open]")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    closeSheets();
  }
  if (e.key === "Tab") {
    const sheet = state.sheet === "nav" ? $("#sidebar") : $("#context-panel");
    const items = [
      ...sheet.querySelectorAll(
        'button:not(:disabled),a[href],input,select,textarea,[tabindex="0"]',
      ),
    ].filter((el) => el.getClientRects().length);
    const first = items[0],
      last = items.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
});
const breakpoint = matchMedia("(min-width:1440px)");
function layout() {
  closeSheets(false);
  $("#shell").classList.toggle("context-hidden", !breakpoint.matches);
  $("#shell").classList.remove("rail");
  $("#context-toggle").setAttribute(
    "aria-expanded",
    String(breakpoint.matches),
  );
}
breakpoint.addEventListener("change", layout);
matchMedia("(min-width:1024px)").addEventListener("change", layout);
layout();
icons();
attempt(boot);
