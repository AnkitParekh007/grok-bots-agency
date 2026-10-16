import {
  esc,
  icon,
  status,
  date,
  time,
  fileURL,
  markdown,
  empty,
} from "./ui.js";
export function executionEnvelope(r) {
  const envelope = r.execution_envelope_json;
  if (!envelope) return '<div class="callout">This legacy plan requires a new reviewable execution package.</div>';
  return '<h3>Execution inputs bound to this review</h3><p class="small muted">Target: ' +
    esc(envelope.environment?.base_url || "") + " · " + esc(r.model_id || r.model_provider) +
    " · " + Object.keys(envelope.fixtures || {}).length +
    ' fixture(s). Source, expected results, fixture bytes, implementation, model, policy and configuration are fingerprinted.</p>' +
    '<details><summary>Inspect execution manifest</summary><pre class="small">' +
    esc(JSON.stringify(envelope, null, 2)) + "</pre></details>";
}
export const providerName = (p) =>
  ({
    ollama: "Ollama",
    lmstudio: "LM Studio",
    llamacpp: "llama.cpp",
    vllm: "vLLM",
    groq: "Groq",
    gemini: "Gemini",
    openrouter: "OpenRouter",
    replay: "Replay",
    compatible: "Compatible API",
    nvidia: "NVIDIA NIM",
  })[p] || p;
export const usable = (m) =>
  m && m.provider !== "nvidia" && m.permitted && m.available !== false;
const button = (text, action, kind = "") =>
  '<button class="button ' +
  kind +
  '" data-action="' +
  action +
  '">' +
  text +
  "</button>";
export function welcome(model, env, canRequest, hasApproved) {
  const task = (name, sub, ico, action, disabled = false) =>
    '<button class="task-card" data-action="' +
    action +
    '" ' +
    (disabled ? "disabled" : "") +
    ">" +
    icon(ico) +
    "<span><strong>" +
    name +
    "</strong><span>" +
    sub +
    "</span></span></button>";
  return (
    '<div class="welcome"><span class="welcome-symbol">' +
    icon("shield") +
    '</span><h1>From story to evidence.</h1><p>Your workspace for understanding requirements, reviewing test plans, and running approved QA checks.</p><div class="welcome-context"><span class="chip">' +
    icon("cpu") +
    esc(
      model
        ? providerName(model.provider) + " · " + model.model
        : "Choose a model",
    ) +
    '</span><span class="chip">' +
    icon("projects") +
    esc(env || "Select environment") +
    '</span></div><div class="task-grid">' +
    task(
      "Analyze a Jira Story",
      "Understand requirements and spot coverage gaps.",
      "file",
      "suggest-analyze",
      !canRequest,
    ) +
    task(
      "Generate Test Cases",
      "Build a plan using approved skills and oracles.",
      "sessions",
      "suggest-plan",
      !canRequest,
    ) +
    task(
      "Run Approved Browser Tests",
      hasApproved
        ? "Follow approved executions and inspect their outcomes."
        : "Review a generated plan before starting any tests.",
      "runs",
      "runs",
      !hasApproved,
    ) +
    task(
      "Inspect Test Results",
      "Open saved results, reports, and evidence.",
      "reports",
      "reports",
    ) +
    '</div><p class="welcome-footnote">' +
    (model?.provider === "replay"
      ? "Replay uses recorded plans. It does not demonstrate model reasoning."
      : "Plans are validated against the action catalog. You stay in control of execution.") +
    "</p></div>"
  );
}
export function activity(r) {
  return (
    '<details class="tool-activity" data-key="activity"><summary>Tool activity · ' +
    r.events.length +
    ' events</summary><ol class="activity-list">' +
    r.events
      .map(
        (e) =>
          '<li><time datetime="' +
          esc(e.ts) +
          '">' +
          time(e.ts) +
          "</time><p><strong>" +
          esc(e.step) +
          "</strong> · " +
          esc(e.message) +
          "</p></li>",
      )
      .join("") +
    '</ol><details data-key="usage"><summary>Run usage & model details</summary><dl>' +
    [
      ["Run", r.id],
      ["Model", r.model_provider + " / " + (r.model_id || "Unknown")],
      ["Version", r.model_version || "Not reported"],
      ["Calls", r.usage?.calls ?? "Unknown"],
      ["Tokens", r.usage?.total_tokens ?? "Unknown"],
      [
        "Cost (USD)",
        typeof r.usage?.cost_usd === "number"
          ? r.usage.cost_usd.toFixed(6)
          : "Unknown",
      ],
    ]
      .map(
        ([key, value]) =>
          "<dt>" + esc(key) + "</dt><dd>" + esc(value) + "</dd>",
      )
      .join("") +
    "</dl></details></details>"
  );
}
export function progress(r) {
  const total = r.plan_json?.test_cases.length || 0;
  const done = r.results.length;
  return (
    '<div class="progress-label"><span>' +
    done +
    " of " +
    total +
    " tests recorded</span><span>" +
    Math.round(total ? (done / total) * 100 : 0) +
    '%</span></div><progress aria-label="Tests recorded" max="' +
    (total || 1) +
    '" value="' +
    done +
    '"></progress>'
  );
}
export function conversation(r, canCancel) {
  const p = r.planning_json;
  let answer = "The request is being planned. Execution has not started.";
  if (p) answer = p.understanding.summary;
  if (r.analysis_json) answer = r.analysis_json.reason + " Source requirements were extracted without model inference. Execution is unavailable until the missing capabilities are reviewed and a new plan is generated.";
  if (
    r.status === "failed" ||
    r.status === "blocked" ||
    r.status === "interrupted"
  )
    answer =
      "This run could not continue. The recorded error is shown below; any partial evidence remains available.";
  if (r.status === "rejected")
    answer =
      "The plan was rejected. No tests were authorized by that decision.";
  if (r.status === "cancelled")
    answer =
      "This run was cancelled. Any completed tests and evidence are preserved.";
  const count = r.plan_json?.test_cases.length || 0,
    gaps = p?.coverage_gaps.length || 0;
  let summary =
    '<div class="run-summary"><div class="section-heading"><h2>' +
    (r.status === "awaiting_approval"
      ? "Your test plan is ready"
      : r.status === "executing"
        ? "Tests in progress"
        : r.status === "completed"
          ? "Execution complete"
          : "QA run") +
    '</h2><span id="status">' +
    status(r.status) +
    "</span></div>";
  if (count)
    summary +=
      "<p>" +
      count +
      " proposed tests" +
      (gaps
        ? " · " + gaps + " coverage gap" + (gaps === 1 ? "" : "s")
        : " · No coverage gaps reported") +
      "</p>";
  if (r.status === "executing" || r.summary_json) summary += progress(r);
  if (r.summary_json)
    summary +=
      '<p style="margin-top:12px">' +
      r.summary_json.passed +
      " passed · " +
      r.summary_json.failed +
      " failed · " +
      r.summary_json.error +
      " errors · " +
      r.summary_json.blocked +
      " blocked" +
      (r.results.some((x) => x.status === "not_run")
        ? " · " +
          r.results.filter((x) => x.status === "not_run").length +
          " not run"
        : "") +
      "</p>";
  summary +=
    '<div class="actions">' +
    (r.plan_json
      ? button(
          r.status === "awaiting_approval"
            ? "Review & approve plan"
            : "View test plan",
          "review",
          r.status === "awaiting_approval" ? "primary" : "",
        )
      : "") +
    (r.results.length || r.summary_json || r.analysis_json
      ? button(r.analysis_json ? "View source review" : "View results & evidence", "evidence")
      : "") +
    (canCancel ? button("Cancel run", "cancel") : "") +
    (r.status === "failed" ||
    r.status === "blocked" ||
    r.status === "interrupted"
      ? button("Start a fresh request", "retry")
      : "") +
    "</div></div>";
  return (
    '<article class="message"><div class="message-header"><span class="avatar">Y</span>You<time class="message-time">' +
    date(r.created) +
    '</time></div><div class="user-message markdown">' +
    markdown(r.request) +
    '</div></article><article class="message"><div class="message-header"><span class="agent-avatar">b.</span>QA Agent<span class="chip">' +
    esc(r.model_label) +
    '</span></div><div class="message-body"><div class="markdown">' +
    markdown(answer) +
    "</div>" +
    summary +
    (r.error
      ? '<div class="callout" role="alert">' + esc(r.error) + "</div>"
      : "") +
    activity(r) +
    '</div><div class="message-actions"><button data-action="copy-answer">' +
    icon("copy") +
    " Copy summary</button></div></article>"
  );
}
export function criteria(r) {
  return (r.ac_json?.acceptance_criteria || [])
    .map(
      (a) =>
        '<div class="criterion"><span class="chip">' +
        esc(a.id) +
        "</span><p>" +
        esc(a.text) +
        "</p><blockquote>" +
        esc(a.source_quote) +
        "</blockquote>" +
        (a.source_id ? '<p class="small muted">Source: ' + esc(a.source_id) + '</p>' : '') + "</div>",
    )
    .join("");
}
export function cases(r, full = false) {
  return (r.plan_json?.test_cases || [])
    .map(
      (t) =>
        '<article class="plan-card"><div class="test-meta"><strong>' +
        esc(t.id) +
        '</strong><span class="chip">' +
        esc(t.category) +
        '</span><span class="chip">' +
        esc(t.type) +
        '</span><span class="chip">' +
        esc((t.ac_refs || []).join(", ")) +
        "</span></div><h3>" +
        esc(t.title) +
        '</h3><details data-key="tc-' +
        esc(t.id) +
        '" ' +
        (full ? "open" : "") +
        "><summary>Skills, action & oracle</summary><dl><dt>Skill</dt><dd>" +
        esc(t.skill) +
        "</dd><dt>Action</dt><dd>" +
        esc(t.action) +
        "</dd><dt>Oracle</dt><dd>" +
        esc(t.oracle_ref) +
        "</dd><dt>Behavior</dt><dd>" +
        esc(t.behavior_ref) +
        "</dd><dt>Parameters</dt><dd>" +
        Object.entries(t.params || {})
          .map(([k, v]) => esc(k) + ": " + esc(v))
          .join("<br>") +
        "</dd></dl></details></article>",
    )
    .join("");
}
export function gaps(r) {
  const g = r.planning_json?.coverage_gaps || r.analysis_json?.coverage_gaps || [];
  return g.length
    ? '<ul class="gap-list">' +
        g
          .map(
            (x) =>
              "<li><strong>" +
              esc(x.ac_refs.join(", ") || "General") +
              "</strong> · " +
              esc(x.kind.replaceAll("_", " ")) +
              "<br>" +
              esc(x.reason) +
              "</li>",
          )
          .join("") +
        "</ul>"
    : '<p class="small muted">No coverage gaps reported by this plan.</p>';
}
export function approvalRecord(r) {
  const a = r.approval;
  if (!a) return "";
  return (
    '<div class="context-note approval-record" style="margin-top:16px">' +
    status(a.decision) +
    "<p>Reviewed by <strong>" +
    esc(a.identity || a.approver) +
    "</strong><br>" +
    esc(
      a.auth_mode === "local" && a.identity
        ? "Authenticated identity"
        : "Unauthenticated demo label",
    ) +
    " · " +
    date(a.ts) +
    "</p>" +
    (a.comment ? "<p>" + esc(a.comment) + "</p>" : "") +
    (a.plan_hash !== r.plan_hash
      ? "<p>Review does not match the current plan hash.</p>"
      : "") +
    "</div>"
  );
}
export function planPanel(r) {
  return !r?.plan_json
    ? empty(
        "No test plan yet",
        "Generate a plan to inspect its criteria, tests, and validation.",
      )
    : '<div id="plan"><div class="section-heading"><h2>Validated test plan</h2><span class="small muted">' +
        r.plan_json.test_cases.length +
        " tests · " +
        esc(r.story_key) +
        "</span></div>" +
        status(r.status) +
        approvalRecord(r) +
        button("Open full plan review", "review", "primary") +
        '<h3 style="margin-top:24px">Acceptance criteria</h3>' +
        criteria(r) +
        "<h3>Coverage gaps</h3>" +
        gaps(r) +
        '<h3 style="margin-top:24px">Proposed tests</h3>' +
        cases(r) +
        executionEnvelope(r) +
        '<h3 style="margin-top:20px">Execution envelope hash</h3><code class="hash">' +
        esc(r.plan_hash) +
        '</code><p class="small muted">Created ' +
        date(r.plan_created_at) +
        ". Expiry and all bound execution inputs are checked by the server.</p></div>";
}
export function executionPanel(r) {
  if (!r)
    return empty(
      "No execution selected",
      "Open a saved run or generate a new plan.",
    );
  const running = [...r.events]
    .reverse()
    .find((e) => e.step === "execute" && e.message.includes(" running:"));
  return (
    '<div class="section-heading"><h2>Execution timeline</h2><span class="small muted">Updated ' +
    date(r.updated) +
    "</span></div>" +
    status(r.status) +
    (r.plan_json ? progress(r) : "") +
    (r.status === "awaiting_approval"
      ? '<div class="callout">Execution is blocked until the exact plan is approved.</div>'
      : "") +
    (r.status === "executing" && running
      ? '<p class="small" style="margin-top:16px">' +
        esc(running.message) +
        "</p>"
      : "") +
    (r.plan_json?.test_cases || [])
      .map((t) => {
        const result = r.results.find((x) => x.id === t.id);
        const isRunning =
          r.status === "executing" && running?.message.startsWith(t.id + " ");
        return (
          '<div class="test-result"><div class="test-meta"><strong>' +
          esc(t.id) +
          "</strong>" +
          status(result?.status || (isRunning ? "running" : "pending")) +
          "</div><h3>" +
          esc(t.title) +
          '</h3><p class="small muted">' +
          (typeof result?.duration_ms === "number"
            ? (result.duration_ms / 1000).toFixed(2) + " seconds"
            : "Duration not recorded yet") +
          "</p></div>"
        );
      })
      .join("") +
    activity(r)
  );
}
export function evidencePanel(r) {
  if (r?.analysis_json) {
    const base = '/api/runs/' + encodeURIComponent(r.id);
    return '<h2>Source review · analysis only</h2><p>' + esc(r.analysis_json.reason) + '</p><p>Zero tests executed. No model inference. Human source review and execution-capability onboarding are required.</p><div id="links" class="actions"><a class="button" href="' + base + '/report.html" target="_blank" rel="noopener">HTML source review</a><a class="button" href="' + base + '/report.json">JSON source review</a></div>' + gaps(r);
  }
  if (!r?.results.length)
    return empty(
      "Evidence will appear here",
      "Screenshots, traces, and logs are collected during approved execution.",
    );
  const base = "/api/runs/" + encodeURIComponent(r.id);
  let h =
    '<div id="results"><div class="section-heading"><h2>Results & evidence</h2><span class="small muted">' +
    r.results.length +
    " recorded results</span></div>";
  if (r.summary_json && ["completed", "cancelled"].includes(r.status))
    h +=
      '<div id="links" class="actions"><a id="report-link" class="button" href="' +
      base +
      '/report.html" target="_blank" rel="noopener">HTML report</a><a class="button" href="' +
      base +
      '/report.json">JSON report</a><a href="' +
      base +
      '/audit.jsonl" target="_blank" rel="noopener">Audit log</a></div>';
  h += r.results
    .map(
      (x) =>
        '<article class="test-result"><div class="test-meta"><strong>' +
        esc(x.id) +
        "</strong>" +
        status(x.status) +
        "</div><h3>" +
        esc(x.title) +
        '</h3><p class="small muted">' +
        (typeof x.duration_ms === "number"
          ? (x.duration_ms / 1000).toFixed(2) + " s"
          : "Duration unavailable") +
        "</p>" +
        ((x.diffs || []).length
          ? x.diffs
              .map(
                (d) =>
                  '<p class="diff"><strong>' +
                  esc(d.field) +
                  "</strong><br>Expected " +
                  esc(JSON.stringify(d.expected)) +
                  " · Actual " +
                  esc(JSON.stringify(d.actual)) +
                  "</p>",
              )
              .join("")
          : x.error
            ? '<p class="diff">' + esc(x.error) + "</p>"
            : '<p class="small muted">' +
              (x.status === "passed"
                ? "Recorded values match the approved oracle."
                : "Comparison unavailable.") +
              "</p>") +
        '<div class="result-links">' +
        (x.screenshot
          ? '<button data-action="screenshot" data-result="' +
            esc(x.id) +
            '">Screenshot</button>'
          : "") +
        (x.trace
          ? '<a href="' + fileURL(r.id, x.trace) + '">Download trace</a>'
          : "") +
        (x.log
          ? '<a href="' +
            fileURL(r.id, x.log) +
            '" target="_blank" rel="noopener">Log</a><button data-action="steps" data-result="' +
            esc(x.id) +
            '">Show steps</button>'
          : "") +
        '</div><details data-key="values-' +
        esc(x.id) +
        '"><summary>Expected and actual values</summary><pre class="step-data">' +
        esc(
          JSON.stringify({ expected: x.expected, actual: x.actual }, null, 2),
        ) +
        '</pre></details><div id="steps-' +
        esc(x.id) +
        '"></div></article>',
    )
    .join("");
  return (
    h +
    '</div><p class="small muted" style="margin-top:18px">Open the downloaded trace in a trusted local Playwright trace viewer. Reports and evidence require your current access permissions.</p>'
  );
}
export function modelCard(m, index, selected, canRequest) {
  const val = (v) => v ?? "Not reported";
  const local = m.classification === "local" || m.provider === "replay";
  return (
    '<article class="model-card"><div class="test-meta"><span class="chip">' +
    (m.provider === "replay" ? "Recorded" : local ? "Local" : "Hosted") +
    '</span><span class="chip">' +
    (m.permitted ? "Permitted" : "Not permitted") +
    "</span></div><h2>" +
    esc(providerName(m.provider)) +
    "</h2><p>" +
    esc(m.model) +
    "</p><p>" +
    esc(
      m.provider === "nvidia"
        ? "Unsupported / deferred"
        : m.health.replaceAll("_", " "),
    ) +
    " · availability " +
    (m.available === true
      ? "available"
      : m.available === false
        ? "unavailable"
        : "not checked") +
    "</p><dl>" +
    [
      ["Configured", m.configured ? "Yes" : "No"],
      ["Approved", m.approved ? "Yes" : "No"],
      ["Context", val(m.context_window)],
      ["Structured output", m.structured_output],
      ["Version", m.version || "Unknown"],
      [
        "Quota",
        m.quota?.remaining == null
          ? "Unknown"
          : Object.entries(m.quota.remaining)
              .map(([k, v]) => k + ": " + v)
              .join(" · ") || "No limits reported",
      ],
    ]
      .map(([k, v]) => "<div><dt>" + k + "</dt><dd>" + esc(v) + "</dd></div>")
      .join("") +
    "</dl>" +
    (m.reason ? "<p>" + esc(m.reason) + "</p>" : "") +
    '<details data-key="model-' +
    index +
    '"><summary>Performance & quota details</summary><pre class="step-data">' +
    esc(
      JSON.stringify(
        {
          latency_ms: m.latency_ms,
          tokens_per_second: m.tokens_per_second,
          memory_bytes: m.memory_bytes,
          quota: m.quota,
          last_evaluation: m.last_evaluation,
          discovered_models: m.available_models,
        },
        null,
        2,
      ),
    ) +
    '</pre></details><div class="actions"><button class="button ' +
    (selected ? "primary" : "") +
    '" data-action="choose-model" data-model="' +
    index +
    '" ' +
    (!usable(m) ? "disabled" : "") +
    ">" +
    (selected ? "Selected" : "Use for new run") +
    '</button><button class="button" data-action="probe" data-model="' +
    index +
    '" ' +
    (!m.permitted || m.provider === "nvidia" || !canRequest ? "disabled" : "") +
    ">Check connection</button></div></article>"
  );
}
