# UI-1 workspace redesign

## Baseline and audit

Baseline main: `06d2917219d2a3dba04e6b97c0967350d1fde53b` (merged B3 PR #6). Latest main was fetched and fast-forwarded before creating `feature/ui-production-redesign` in a separate checkout. There were no open PRs at inspection. The existing screenshots and a fresh running replay demonstration were inspected before implementation.

Actual baseline findings:

- At 1440×900, the initial sidebar exposes a long paragraph of model diagnostics while the conversation area is almost empty. Configuration, history and model troubleshooting compete for the same narrow space.
- The four workspace selects share a bottom row with the message input. Their intrinsic widths leave little room for the actual request on shorter desktop screens.
- At 390×844, the document measures **866 CSS pixels wide**. The screenshot shows a squeezed navigation column and clipped composer controls; the original document has no viewport meta tag or responsive breakpoints.
- The awaiting-approval capture at 1440×900 places the long test table below event logs, ACs and coverage details. The approval actions require scrolling the conversation; the hash is abbreviated in the main view.
- Tool logs, technical metadata and repeated model labels are always visible. Test results use a dense table with small evidence thumbnails.
- No theme control, application navigation, modal plan review or drawer focus behavior existed. Native controls have inconsistent spacing; some select labels are only title attributes.

Baseline document width measurements:

| Viewport | Original document width | Redesigned document width |
|---|---:|---:|
| 1920×1080 | 1920 | 1920 |
| 1440×900 | 1440 | 1440 |
| 1366×768 | 1366 | 1366 |
| 1024×768 | 1024 | 1024 |
| 390×844 | 866 | 390 |

[Before and after screenshots](SCREENSHOTS.md) are actual captures, not mockups.

## Implementation

Vanilla JavaScript remains sufficient. No framework, package manager, remote font, icon library or new Python dependency was added. The previous inline frontend is split into:

- `qa_agent/static/workspace.css`: semantic light/dark color, typography, spacing, radius, elevation, icon, focus, motion and breakpoint tokens; component styling and responsive layouts.
- `theme.js`: theme preference before paint; only the theme is stored in local storage.
- `ui.js`: escaped Markdown subset, SVG icons, safe artifact URLs, API errors, status formatting and stable DOM updates.
- `components.js`: welcome, conversation, action/criterion/plan cards, timeline, evidence and read-only model cards.
- `app.js`: authentication, workspace selection, navigation, sequential polling, sheets, exact-plan review and action handling.
- `index.html`: semantic application shell, labeled controls, tab panels and native dialogs.

The backend change is a static-file mount for these public UI assets. Authentication, ownership, selection, model dispatch, quotas, policy, approvals, expiry, execution, cancellation, integrations, reports and artifact authorization are unchanged. No default provider/model or evaluation verdict is upgraded.

At 1440px and above, the sidebar is 248px and context panel 360px. At 1024–1439px, navigation becomes a 72px rail and context opens as a sheet. Below 1024px, navigation is a drawer and the conversation gets the full width. The composer stays in the bottom layout row while the conversation scrolls; only the active context tab scrolls. Short desktop screens use tighter welcome spacing. Sidebar and context collapse controls remain functional.

The neutral interface uses existing project naming with a simple text monogram, not a claimed official corporate logo or newly invented brand standard. Status colors are paired with explicit text. Motion is limited to short control-color transitions and disabled under reduced motion. There is no fake streaming, animated reasoning, fabricated history, dummy business metric or simulated progress.

## Workflow map and safety

| Workflow | Redesigned path / actual contract |
|---|---|
| Login/logout | Local token dialog → existing HttpOnly SameSite cookie; logout clears visible run data, cached render state, review context and polling. No token in local storage. Demo identity remains explicitly unauthenticated. |
| New request | New QA Session → context selection → composer. Each submission creates a saved request/run; this backend has no multi-turn conversation memory. |
| Sessions / Test Runs | Real persisted `/api/runs` records, with owner filtering performed by the server; recent entries are derived from those records. |
| Projects / story / repository | Existing project/story/repository endpoints; project selection loads allowed stories and repositories, and story selection shows the fetched summary. Repository access remains read only. |
| Environments | Existing executable environment list; backend selection validation and environment policy remain authoritative. |
| Models | Configured registry entries; local/hosted/recorded classification, approval/configuration, health, context, output format, version and quotas. Technical metrics are expandable. Only permitted supported entries can be selected. NVIDIA NIM is disabled/deferred. The legacy implemented compatible adapter remains visible. |
| Model probe | Explicit metadata probe, never automatic generation/download. A failed probe keeps the selected model and disables submission rather than silently falling back. Hosted calls are never automatic. |
| Plan / approval | Dedicated native dialog shows story, quoted ACs, behaviors, gaps, test categories, skills/actions/oracles/parameters, creation time and full hash. The full hash remains visible beside reviewer controls. Reviewer comment is optional. |
| Approval enforcement | Requester/self-approval and role restrictions are reflected in controls; the server still verifies ownership, role, expiry, exact hash, catalog and conflict state. The reviewed run ID/hash are snapshotted; the client checks current state before submitting, and the backend enforces the decision. No UI-only substitute. |
| Execution | Real status and timestamped events; current test from backend execute events, individual result statuses/durations, recorded-result count and progress. Missing durations remain unknown. Cancellation uses the existing endpoint. |
| Evidence / reports | Expected/actual mismatches, authorized screenshot viewer, ZIP trace download, logs/steps, HTML/JSON reports and audit links. Full expected/actual JSON and raw logs require opening details. Trace viewing uses a trusted local Playwright viewer; files are not uploaded to an external viewer. |
| Errors / retry | Inline API/run errors, refresh for failed reads, and a fresh request action for failed/interrupted runs. Retry never silently resubmits approval or switches models. |

Markdown is an escaped subset: paragraphs, headings, lists, bold and code blocks with copy actions. Raw HTML, images and executable URLs are not rendered from message text. Backend/model text is escaped before it enters HTML. Polling preserves expanded details and scroll positions, and avoids replacing composer or reviewer inputs. Poll requests are sequential and stale responses are ignored after run/auth changes.

## Accessibility and browser validation

Targeted improvements include semantic regions, skip navigation, labels, visible focus outlines, arrow/Home/End tab navigation, status announcements, native dialog focus handling, modal drawer focus containment, Escape and focus return, theme persistence, reduced motion and text-plus-color statuses. Tests check 4.5:1 contrast for the main semantic text/status token pairs in both themes. This is not a complete independent WCAG audit or a screen-reader certification.

Automated Chromium coverage in `tests/test_ui_workspace.py` uses temporary synthetic identities, replay planning and the real synthetic SUT. It covers login failure/success/logout, all viewport sizes, empty state, geometry/overflow, drawer keyboard interactions, tab keyboard interactions, themes/contrast/reduced motion, project/story/repository/environment selection, model registry/probe failure without fallback, prompt submission, forbidden self-approval, exact-hash/comment submission by a distinct test approver, real execution, cancellation, rejection, errors/recovery, reports, screenshot/trace/log access, role restrictions, safe Markdown and copy actions. Test approval is explicitly a synthetic UI contract test, not a real-model human sign-off.

The existing `scripts/demo_and_screenshots.py` now drives the redesigned review dialog and Evidence tab. CI remains offline, uses replay/stub inference, and uploads test outputs and UI screenshots under `reports/`.

Run from the repository root after installing the existing requirements and Chromium:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ui_workspace.py -q
.\.venv\Scripts\python.exe -m pytest -q
$env:QA_UI_SCREENSHOTS_DIR = (New-Item -ItemType Directory -Force 'reports/ui-screenshots').FullName
.\.venv\Scripts\python.exe -m pytest tests/test_ui_workspace.py -q
$env:QA_SCREENSHOTS_DIR = (New-Item -ItemType Directory -Force 'reports/ui-demo').FullName
.\.venv\Scripts\python.exe scripts/demo_and_screenshots.py
```

To launch interactively with replay, set `$env:QA_MODEL_PROVIDER='replay'`, start the synthetic SUT and workspace using the existing [Windows quick start](../../README.md#quick-start--windows). Keep corporate/hosted integrations disabled. Current default local model configuration is unchanged.

## Results and limitations

Local validation completed on Windows/Python 3.13.14:

| Check | Result |
|---|---|
| Existing non-integration suite | **333 passed**, 50 deselected, one upstream Starlette/httpx deprecation warning; 200.33 s |
| Four Chromium UI scenarios | **4 passed**, 73.11 s, including authenticated approval/rejection, synthetic execution/artifacts, responsive/keyboard/theme checks, model failure without fallback, safe Markdown and copy |
| Updated replay demonstration | **Completed**, eight tests: three passed, five failed against the intentionally defective synthetic SUT; screenshots and report generated |
| Documentation / source | Local links, Python/JavaScript syntax, screenshot dimensions/checksums and whitespace checked |
| Full integration suite / offline evaluation | Final-head GitHub Actions results are linked in the draft PR; CI runs all existing integration checks plus the UI scenarios without hosted credentials |

The test iterations exposed and fixed a model-selection refresh race, first-run select defaults after login, focus preservation across polling/dialogs, and demo configuration precedence. A test locator ambiguity was corrected. Approval history/comment visibility and per-run usage remain available. The headless demo writes an isolated ignored replay/demo configuration so an administrator's local-model default cannot override its offline intent. No security or provider policy was relaxed.

- UI sessions correspond to individual persisted QA requests/runs. There is no invented multi-turn chat backend, new session database or artificial streaming.
- The backend does not emit partial model output or token-by-token reasoning state. The UI says Planning while planning, without claiming continuous reasoning. Execution progress uses actual result/event state.
- Quota, availability, versions and performance stay unknown until reported; connection checks do not certify model quality. B3's failed local planning trial remains unchanged.
- Visual checks combine actual screenshots, geometry assertions, contrast samples and manual inspection. Pixel-perfect cross-platform golden-image diffs are not asserted: Windows/Linux fonts differ and IDs/timestamps are live. CI captures remain available as artifacts for review.
- The standalone generated HTML report retains its existing format; the workspace evidence browser and links are redesigned.
- Chromium is the automated browser target; manual Safari/Firefox and assistive-technology validation remain outstanding. Accessible native controls and reduced motion are implemented, but full WCAG 2.2 AA certification is not claimed.
- No real corporate data, hosted/paid inference, deployment or automatic merge was used.
