import {api, esc, announce} from './ui.js';

let activeDialog = null;
let epoch = 0;
export function closeJiraWrites() {
  epoch++;
  activeDialog?.close();
}

export async function openJiraWrites(run, me) {
  closeJiraWrites();
  const generation = epoch;
  const previous = document.activeElement;
  const base = '/api/runs/' + encodeURIComponent(run.id) + '/jira-writes';
  const initial = await api(base);
  if (generation !== epoch) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'review-dialog';
  dialog.setAttribute('aria-labelledby', 'jira-write-title');
  dialog.innerHTML = '<header class="dialog-heading"><h1 id="jira-write-title">Human-reviewed Jira writes</h1><button class="icon-button" data-jira="close" aria-label="Close Jira write review">×</button></header><div class="review-content"><p>Review the destination and exact payload. Approval and sending are separate actions. An uncertain result requires operator reconciliation and cannot be retried here.</p><p data-jira-error class="error-text" role="alert"></p><div data-jira-content></div></div>';
  document.body.append(dialog);
  activeDialog = dialog;
  dialog.addEventListener('close', () => {
    if (activeDialog === dialog) activeDialog = null;
    dialog.remove();
    if (previous?.isConnected) previous.focus();
  }, {once: true});
  let inventory = initial;
  function render() {
    const content = dialog.querySelector('[data-jira-content]');
    content.innerHTML = (!inventory.available ? '<p>New Jira writes are disabled or awaiting operator configuration for this project.</p>' :
      '<div class="actions"><button class="button" data-jira="propose" data-kind="create">Draft defect</button><button class="button" data-jira="propose" data-kind="comment">Draft source-issue comment</button></div>' +
      (inventory.transitions.length ? '<label>Source-issue workflow transition <select data-jira-transition>' + inventory.transitions.map(t => '<option>' + esc(t) + '</option>').join('') + '</select></label><button class="button" data-jira="propose" data-kind="transition">Draft transition</button>' : '')) +
      inventory.operations.map(o => {
        const d = o.draft;
        const reviewer = me.role === 'approver' && me.user !== o.requester && me.user !== run.requested_by;
        const live = Date.now() < d.expires * 1000 && inventory.available;
        const attr = ' data-operation="' + esc(d.id) + '" data-hash="' + esc(o.draft_hash) + '"';
        return '<article class="test-result"><h2>' + esc(d.kind) + ' · ' + esc(o.state) + '</h2><dl><dt>Destination</dt><dd>' + esc(d.destination) + '</dd><dt>Expires</dt><dd>' + esc(new Date(d.expires * 1000).toLocaleString()) + '</dd><dt>Requester</dt><dd>' + esc(o.requester) + '</dd><dt>Reviewer</dt><dd>' + esc(o.approver || 'Awaiting review') + '</dd><dt>Review SHA-256</dt><dd class="hash">' + esc(o.draft_hash) + '</dd></dl><h3>Exact request payload</h3><pre class="step-data">' + esc(JSON.stringify(d.payload, null, 2)) + '</pre>' +
          '<details><summary>Review binding and upstream state</summary><pre class="step-data">' + esc(JSON.stringify({configuration_hash:d.configuration_hash, evidence_hash:d.evidence_hash, upstream:d.upstream}, null, 2)) + '</pre></details>' +
          (o.outcome ? '<p>Recorded outcome: ' + esc(JSON.stringify(o.outcome)) + '</p>' : '') +
          (['dispatching', 'uncertain', 'restored_reconciliation_required'].includes(o.state) ? '<p role="status">Reconciliation required. Do not create a replacement request until the operator verifies Jira and the original worker.</p>' : '') +
          (o.state === 'pending' && reviewer && live ? '<div class="actions"><button class="button" data-jira="reject"' + attr + '>Reject write</button><button class="button primary" data-jira="approve"' + attr + '>Approve exact payload</button></div>' : '') +
          (o.state === 'approved' && o.approver === me.user && live ? '<button class="button danger" data-jira="dispatch"' + attr + '>Send approved Jira write once</button>' : '') + '</article>';
      }).join('');
  }
  dialog.addEventListener('click', async event => {
    const button = event.target.closest('[data-jira]');
    if (!button || button.disabled) return;
    const action = button.dataset.jira;
    if (action === 'close') {dialog.close(); return;}
    const shown = inventory.operations.find(o => o.draft.id === button.dataset.operation);
    dialog.querySelectorAll('button').forEach(b => {b.disabled = true;});
    dialog.querySelector('[data-jira-error]').textContent = '';
    try {
      if (action === 'propose') {
        const kind = button.dataset.kind;
        const body = {kind};
        if (kind !== 'create') body.target = inventory.source_issue;
        if (kind === 'transition') body.transition = dialog.querySelector('[data-jira-transition]').value;
        await api(base, body);
      } else if (shown && action === 'dispatch') {
        await api(base + '/' + shown.draft.id + '/dispatch', {});
      } else if (shown && ['approve', 'reject'].includes(action)) {
        await api(base + '/' + shown.draft.id + '/review', {draft_hash: button.dataset.hash, decision: action});
      }
      inventory = await api(base);
      if (!dialog.isConnected) return;
      render();
      announce('Jira write receipt updated.');
    } catch (error) {
      if (dialog.isConnected) dialog.querySelector('[data-jira-error]').textContent = error.message;
    } finally {
      dialog.querySelectorAll('button').forEach(b => {b.disabled = false;});
    }
  });
  render();
  dialog.showModal();
}
