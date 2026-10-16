export const $ = (s) => document.querySelector(s);
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const paths = {
  plus: "M12 5v14M5 12h14",
  menu: "M4 6h16M4 12h16M4 18h16",
  sessions: "M5 4h14v13H8l-4 3V4z",
  projects: "M3 7h7l2-3h9v16H3z",
  runs: "M8 4l12 8-12 8z",
  reports: "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h7",
  cpu: "M7 7h10v10H7zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1",
  panel: "M3 4h18v16H3zM9 4v16",
  "panel-right": "M3 4h18v16H3zM15 4v16",
  "arrow-up": "M12 19V5M5 12l7-7 7 7",
  check: "M5 12l4 4L19 6",
  file: "M6 3h9l4 4v14H6zM14 3v5h5",
  copy: "M8 8h12v13H8zM4 16V3h12",
  shield: "M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6",
  logout: "M10 4H4v16h6M9 12h12M17 8l4 4-4 4",
};
export function icon(name) {
  return (
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="' +
    (paths[name] || paths.file) +
    '"/></svg>'
  );
}
export function icons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
    el.removeAttribute("data-icon");
  });
}
export const labels = {
  awaiting_approval: "Awaiting approval",
  planning: "Planning",
  queued_planning: "Queued for planning",
  queued_execution: "Queued for execution",
  executing: "Executing",
  completed: "Completed",
  analysis_only: "Analysis only",
  failed: "Failed",
  blocked: "Blocked",
  cancelled: "Cancelled",
  rejected: "Rejected",
  interrupted: "Interrupted",
  passed: "Passed",
  error: "Error",
  not_run: "Not run",
  running: "Running",
  pending: "Pending",
  approved: "Approved",
};
export const status = (s) =>
  '<span class="status ' + esc(s) + '">' + esc(labels[s] || s) + "</span>";
export const date = (value) =>
  value
    ? new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Not reported";
export const time = (value) =>
  value
    ? new Date(value).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "";
export const fileURL = (rid, rel) =>
  "/api/runs/" +
  encodeURIComponent(rid) +
  "/artifacts/" +
  String(rel)
    .replaceAll("\\", "/")
    .split("/")
    .map(encodeURIComponent)
    .join("/");
export async function api(url, body, method) {
  const csrf = document.cookie.match(/(?:^|;\s*)(?:__Host-qa_csrf|qa_csrf)=([^;]+)/)?.[1];
  const response = await fetch(url, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(csrf ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch (_) {
      detail = response.statusText;
    }
    const error = new Error(
      typeof detail === "string"
        ? detail
        : "Request rejected (" +
            response.status +
            "). Check your input and access permissions.",
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}
// Escaped text-only Markdown subset: no raw HTML, images or executable URLs.
export function markdown(text) {
  const pieces = String(text || "").split(/```/);
  return pieces
    .map((part, i) => {
      if (i % 2) {
        const code = part.replace(/^\w*\n/, "");
        return (
          "<pre><code>" +
          esc(code) +
          '</code></pre><button class="button subtle" data-action="copy-code">' +
          icon("copy") +
          "Copy code</button>"
        );
      }
      return esc(part)
        .split(/\n\n+/)
        .filter(Boolean)
        .map((block) => {
          const inline = (s) =>
            s
              .replace(/`([^`]+)`/g, "<code>$1</code>")
              .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
          if (/^#{1,3} /.test(block))
            return "<h3>" + inline(block.replace(/^#{1,3} /, "")) + "</h3>";
          if (block.split("\n").every((line) => /^[-*] /.test(line)))
            return (
              "<ul>" +
              block
                .split("\n")
                .map((line) => "<li>" + inline(line.slice(2)) + "</li>")
                .join("") +
              "</ul>"
            );
          return "<p>" + inline(block).replaceAll("\n", "<br>") + "</p>";
        })
        .join("");
    })
    .join("");
}
export function update(el, html) {
  if (el.dataset.html === html) return;
  const open = [...el.querySelectorAll("details[open][data-key]")].map(
    (d) => d.dataset.key,
  );
  const focused = el.contains(document.activeElement)
    ? document.activeElement
    : null;
  const focus = focused?.id;
  const action = focused?.dataset.action;
  const result = focused?.dataset.result;
  const link = focused?.getAttribute("href");
  const detail =
    focused?.tagName === "SUMMARY" ? focused.parentElement.dataset.key : null;
  const scroll = el.parentElement.scrollTop;
  el.innerHTML = html;
  el.dataset.html = html;
  el.querySelectorAll("details[data-key]").forEach((d) => {
    if (open.includes(d.dataset.key)) d.open = true;
  });
  if (focus) {
    const next = document.getElementById(focus);
    if (next) next.focus({ preventScroll: true });
  } else if (action) {
    const next = [...el.querySelectorAll("[data-action]")].find(
      (node) =>
        node.dataset.action === action &&
        node.dataset.result === result &&
        !node.disabled,
    );
    next?.focus({ preventScroll: true });
  } else if (link) {
    [...el.querySelectorAll("a[href]")]
      .find((node) => node.getAttribute("href") === link)
      ?.focus({ preventScroll: true });
  } else if (detail) {
    [...el.querySelectorAll("details[data-key]")]
      .find((node) => node.dataset.key === detail)
      ?.querySelector("summary")
      ?.focus({ preventScroll: true });
  }
  el.parentElement.scrollTop = scroll;
}
export const empty = (title, body) =>
  '<div class="empty-state">' +
  icon("file") +
  "<h2>" +
  esc(title) +
  "</h2><p>" +
  esc(body) +
  "</p></div>";
export function announce(message) {
  $("#live-status").textContent = message;
}
