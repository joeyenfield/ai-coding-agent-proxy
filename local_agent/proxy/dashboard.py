UI_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Proxy | Request Review</title>
<style>
:root{color-scheme:light;--ink:#202726;--muted:#66716e;--line:#dbe2df;--paper:#fff;--wash:#f4f7f6;--accent:#087c68;--soft:#e7f4ef;--error:#ae3535;--warn:#89600d;font-family:"IBM Plex Sans","Segoe UI",sans-serif;color:var(--ink);background:var(--wash);letter-spacing:0}
*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button,a,input,select{touch-action:manipulation}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button,input,select{border:1px solid var(--line);border-radius:4px;background:var(--paper);color:var(--ink);min-height:36px;padding:7px 11px}button:hover:not(:disabled){border-color:var(--accent);background:var(--soft)}button.primary{background:var(--accent);border-color:var(--accent);color:white}input,select{min-width:0;max-width:100%}input[type=checkbox]{min-height:0;accent-color:var(--accent)}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}:focus-visible{outline:2px solid var(--accent);outline-offset:3px}h1,h2,h3,p{margin:0}h1{font-size:21px;font-weight:650}h2{font-size:18px;font-weight:600}h3{font-size:14px;font-weight:600}.muted{color:var(--muted)}.mono,pre,code{font-family:"IBM Plex Mono","DejaVu Sans Mono",monospace}.mono,code{font-size:12px;overflow-wrap:anywhere}.ok{color:var(--accent)}.warn{color:var(--warn)}.error{color:var(--error)}[hidden]{display:none!important}
header{padding:20px 28px 0;background:var(--paper);border-bottom:1px solid var(--line)}.topbar,.section-head,.toolbar,.actions,.payload-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.brand{display:flex;align-items:center;gap:12px}.brand-mark{display:grid;place-items:center;width:34px;height:34px;background:var(--ink);color:white;font-family:monospace;font-weight:700;border-radius:4px}.connection{font-size:12px;display:flex;align-items:center;gap:8px}.dot{width:7px;height:7px;border-radius:50%;background:var(--accent)}.connection.offline .dot{background:var(--error)}.navigation{display:flex;gap:24px;margin-top:20px}.navigation button{padding:10px 0;border:0;border-radius:0;border-bottom:2px solid transparent;background:none;color:var(--muted)}.navigation button[aria-selected=true]{border-color:var(--accent);color:var(--ink);font-weight:600}
main{padding:22px 28px;max-width:1800px;margin:auto}.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:24px;border-bottom:1px solid var(--line);padding-bottom:20px;gap:20px}.summary .label{font-size:12px;color:var(--muted);margin-bottom:5px}.summary .value{font-size:24px;font-weight:600}.section-head{margin-bottom:16px}.section-head p{font-size:12px;color:var(--muted);margin-top:5px}.workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.12fr);gap:22px;align-items:start}.request-list{min-width:0}.toolbar{justify-content:start;margin-bottom:12px}.search{flex:1 1 180px}.toolbar select{max-width:190px}.table-scroll{overflow:auto;border-top:1px solid var(--line)}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;font-size:11px;font-weight:600;color:var(--muted);padding:11px 9px;background:var(--wash);white-space:nowrap}td{padding:12px 9px;border-bottom:1px solid var(--line);vertical-align:top}td small{display:block;margin-top:5px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}td .request-open{display:block;text-align:left;width:100%;border:0;background:none;padding:0;min-height:24px;font-weight:600;overflow-wrap:anywhere;color:var(--accent)}tr.selected td{background:var(--soft)}tbody tr:hover td{background:#edf3f0}.badge{display:inline-block;font-size:10px;padding:3px 6px;border-radius:3px;background:#ebefed;color:var(--muted);white-space:nowrap}.badge.traced{background:var(--soft);color:var(--accent)}.badge.failed{background:#fcebec;color:var(--error)}.pagination{display:flex;align-items:center;justify-content:space-between;padding-top:14px;font-size:12px;color:var(--muted);gap:10px}.pagination .actions{gap:6px}.pagination button{width:36px;padding:0;font-size:18px}.empty{padding:32px 15px;text-align:center;color:var(--muted);font-size:13px}.live{font-size:12px;border-left:2px solid var(--accent);padding:10px 12px;margin-bottom:16px;background:var(--soft);overflow-wrap:anywhere}
.inspector{position:sticky;top:18px;min-width:0;border:1px solid var(--line);border-radius:6px;background:var(--paper);overflow:hidden}.inspector-head{padding:17px 18px;border-bottom:1px solid var(--line)}.inspector-head h2{font-size:16px;overflow-wrap:anywhere}.inspector-head .muted{font-size:12px;line-height:1.7;margin-top:5px;overflow-wrap:anywhere}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;padding:14px 18px;border-bottom:1px solid var(--line)}.metrics dt{font-size:10px;color:var(--muted);margin-bottom:5px}.metrics dd{margin:0;font-size:12px;overflow-wrap:anywhere}.metrics dl{margin:0}.tabs{display:flex;padding:0 18px;border-bottom:1px solid var(--line);gap:18px;overflow:auto}.tabs button{border:0;border-radius:0;background:none;white-space:nowrap;padding:12px 0;font-size:12px;color:var(--muted);border-bottom:2px solid transparent}.tabs button[aria-selected=true]{border-color:var(--accent);color:var(--ink);font-weight:600}.payload-toolbar{padding:10px 18px;font-size:11px;border-bottom:1px solid var(--line);background:#fafcfb}.payload-toolbar .actions{gap:6px}.payload-toolbar button{font-size:11px;min-height:30px;padding:5px 8px}.payload-toolbar label{display:flex;gap:5px;align-items:center}.payload-area{max-height:62vh;min-height:280px;overflow:auto;padding:18px}pre{margin:0;font-size:12px;line-height:1.65;tab-size:2;white-space:pre;min-width:0}pre.wrap{white-space:pre-wrap;overflow-wrap:anywhere}.notice{padding:24px 20px;font-size:13px;line-height:1.7}.notice p{margin:8px 0 16px;color:var(--muted)}.inspector-placeholder{padding:75px 24px;text-align:center;color:var(--muted);font-size:13px}.conversation-entry{padding:0 0 18px;margin-bottom:18px;border-bottom:1px solid var(--line)}.conversation-entry:last-child{margin-bottom:0;border:0}.conversation-entry h3{font-size:11px;text-transform:uppercase;color:var(--accent);margin-bottom:8px}.conversation-entry pre{white-space:pre-wrap;overflow-wrap:anywhere}.inspector-footer{padding:10px 18px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);overflow-wrap:anywhere}.flash{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:white;padding:10px 16px;border-radius:4px;font-size:12px;max-width:90vw;z-index:10}
.form-section{padding:20px 0;border-top:1px solid var(--line);margin-top:22px}.form-section h2{margin-bottom:16px}.form-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.field{display:flex;flex-direction:column;gap:7px;font-size:12px;color:var(--muted)}.check-field{display:flex;align-items:center;gap:8px;font-size:12px}.form-section .actions{justify-content:start;margin-top:16px}.backend-row{display:grid;grid-template-columns:minmax(120px,.7fr) minmax(180px,2fr) auto;gap:10px;margin-top:10px}.settings{max-width:960px}.settings h3{margin-top:24px;margin-bottom:10px}.session-client{font-weight:600}.session-controls{display:flex;align-items:center;gap:10px;white-space:nowrap}
@media(min-width:1500px){.payload-area{max-height:68vh}}@media(max-width:1050px){.workspace{grid-template-columns:1fr}.inspector{position:static}.payload-area{max-height:600px}.form-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){header{padding:16px 16px 0}main{padding:18px 16px}.topbar{align-items:start}.connection{max-width:145px;overflow-wrap:anywhere}.summary{gap:12px;grid-template-columns:repeat(2,minmax(0,1fr))}.summary .value{font-size:21px}.navigation{gap:22px}.toolbar{gap:8px}.toolbar select{flex:1;max-width:none}.search{flex-basis:100%}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.payload-toolbar{padding:10px 12px}.payload-area{padding:12px}.tabs{gap:14px;padding:0 12px}.form-grid,.backend-row{grid-template-columns:1fr}.session-table{min-width:700px}.inspector-head{padding:14px}.section-head{align-items:start}h1{font-size:19px}}
.conversation-entry summary{cursor:pointer;color:var(--accent);font-size:11px;padding:5px 0}.conversation-entry summary h3{display:inline;margin:0}.conversation-entry[open] summary{margin-bottom:8px}
body.request-focused{overflow:hidden}#request-fullscreen{margin:0;padding:0;border:0;width:100%;height:100dvh;max-width:none;max-height:none;background:var(--paper);color:var(--ink)}#request-fullscreen .inspector{position:static;display:flex;flex-direction:column;width:100%;height:100%;border:0;border-radius:0}#request-fullscreen #inspector-body{display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto}#request-fullscreen .payload-area{flex:1;min-height:0;max-height:none}#request-fullscreen .inspector-head,#request-fullscreen .metrics,#request-fullscreen .tabs,#request-fullscreen .payload-toolbar,#request-fullscreen .inspector-footer{flex-shrink:0}
.session-controls{flex-wrap:wrap;white-space:normal}.danger{color:var(--error);border-color:#e2baba}.danger:hover:not(:disabled){background:#fcebec;border-color:var(--error)}.confirmation{width:min(480px,calc(100% - 32px));max-height:calc(100dvh - 32px);padding:24px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--ink)}.confirmation::backdrop{background:rgba(20,30,25,.45)}.confirmation p{margin:14px 0;font-size:13px;line-height:1.6}.confirmation .actions{justify-content:flex-end;margin-top:20px}.confirmation .mono{font-size:11px}
</style>
</head>
<body>
<header>
  <div class="topbar"><div class="brand"><div class="brand-mark" aria-hidden="true">AI</div><h1>AI Proxy</h1></div><div class="connection" id="connection"><span class="dot"></span><span id="connection-text">Connecting</span></div></div>
  <nav class="navigation" role="tablist" aria-label="Workspace"><button id="nav-requests" role="tab" aria-selected="true" aria-controls="view-requests" data-view="requests">Requests</button><button id="nav-sessions" role="tab" aria-selected="false" aria-controls="view-sessions" data-view="sessions">Sessions</button><button id="nav-settings" role="tab" aria-selected="false" aria-controls="view-settings" data-view="settings">Settings</button></nav>
</header>
<main>
  <div class="summary" id="stats"></div>
  <section id="view-requests" role="tabpanel" aria-labelledby="nav-requests">
    <div class="section-head"><div><h2>Request history</h2><p id="history-count">Loading requests...</p></div><button id="refresh">Refresh</button></div>
    <div id="current" class="live" hidden></div>
    <div class="workspace">
      <div class="request-list">
        <div class="toolbar"><input class="search" id="search" type="search" aria-label="Search request history" placeholder="Search client, model, endpoint or ID"><select id="session-filter" aria-label="Filter by session"><option value="">All sessions</option></select><select id="trace-filter" aria-label="Filter by trace"><option value="">All requests</option><option value="traced">Trace captured</option><option value="untraced">No trace</option><option value="errors">Errors</option></select></div>
        <div class="table-scroll"><table><thead><tr><th scope="col">Request / model</th><th scope="col">Client / time</th><th scope="col">Status</th><th scope="col">Duration</th></tr></thead><tbody id="requests"></tbody></table><div id="history-empty" class="empty" hidden>No matching requests.</div></div>
        <div class="pagination"><span id="page-label"></span><div class="actions"><button id="previous" aria-label="Previous page" title="Previous page">&larr;</button><button id="next" aria-label="Next page" title="Next page">&rarr;</button></div></div>
      </div>
      <aside class="inspector" id="inspector" aria-label="Request inspector"><div class="inspector-placeholder">Select a request</div></aside>
    </div>
  </section>
  <section id="view-sessions" role="tabpanel" aria-labelledby="nav-sessions" hidden>
    <div class="section-head"><h2>Sessions</h2><span class="muted" id="session-count"></span></div>
    <div class="table-scroll"><table class="session-table"><thead><tr><th>Client / session</th><th>Model / backend</th><th>Started</th><th>Requests</th><th>Capture</th><th>Review</th></tr></thead><tbody id="sessions"></tbody></table></div>
    <section class="form-section"><h2>New session</h2><form id="new-session"><div class="form-grid"><label class="field">Client<input name="client" value="manual" required></label><label class="field">Model<input name="model" placeholder="Backend default"></label><label class="field">Backend<select name="backend" id="backend"></select></label><label class="check-field"><input name="trace" type="checkbox">Full tracing</label></div><div class="actions"><button class="primary">Create session</button><code id="created" role="status"></code></div></form></section>
  </section>
  <section class="settings" id="view-settings" role="tabpanel" aria-labelledby="nav-settings" hidden>
    <div class="section-head"><h2>Network configuration</h2></div>
    <form id="network-config"><div class="form-grid"><label class="field">Listen host<input id="listen-host" required></label><label class="field">Listen port<input id="listen-port" type="number" min="1" max="65535" required></label><label class="field">Proxy URL<input id="proxy-url" type="url" required></label><label class="field">Default backend<select id="default-backend"></select></label></div><h3>Ollama backends</h3><div id="backend-rows"></div><div class="actions"><button type="button" id="add-backend">Add backend</button><button class="primary" type="submit">Save configuration</button><span id="config-message" role="status"></span></div></form>
  </section>
</main>
<dialog id="request-fullscreen" aria-label="Fullscreen request flow"></dialog>
<dialog id="cleanup-dialog" class="confirmation" aria-labelledby="cleanup-title" aria-describedby="cleanup-message"><form id="cleanup-form"><h2 id="cleanup-title"></h2><p id="cleanup-message"></p><p id="cleanup-session" class="mono muted"></p><p id="cleanup-error" class="error" role="alert" hidden></p><div class="actions"><button id="cleanup-cancel" type="button">Cancel</button><button id="cleanup-confirm" class="danger" type="submit"></button></div></form></dialog>
<div id="flash" class="flash" role="status" hidden></div>
<script>
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
const format = value => value == null ? '-' : Number(value).toLocaleString(undefined, {maximumFractionDigits:1});
const date = value => value ? new Date(value).toLocaleString() : '-';
const key = record => `${record.session_id}:${record.request_id}`;
const traceURL = record => `/api/sessions/${encodeURIComponent(record.session_id)}/traces/${encodeURIComponent(record.request_id)}`;
let records = [], sessions = [], selected = null, selectedTrace = null, payloadTab = 'request', page = 0, lastCount = -1, selectedDefault = '', selectionVersion = 0, refreshing = false;
let inFlightSessions = new Set(), pendingCleanup = null;
const pageSize = 25;
async function api(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { const body = await response.json(); message = body.detail || message; } catch {}
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  return response.json();
}
function notify(message) {
  $('#flash').textContent = message; $('#flash').hidden = false;
  clearTimeout(notify.timer); notify.timer = setTimeout(() => $('#flash').hidden = true, 4000);
}
function showView(view) {
  document.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.view === view)));
  for (const name of ['requests','sessions','settings']) $(`#view-${name}`).hidden = name !== view;
}
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => showView(button.dataset.view));
function filteredRecords() {
  const query = $('#search').value.trim().toLowerCase(), session = $('#session-filter').value, trace = $('#trace-filter').value;
  return records.filter(record => (!session || record.session_id === session) &&
    (!query || [record.request_id,record.session_id,record.client,record.model,record.backend,record.endpoint].some(value => String(value || '').toLowerCase().includes(query))) &&
    (!trace || (trace === 'traced' ? record.trace_available : trace === 'untraced' ? !record.trace_available : record.status >= 400 || record.error_type)));
}
function renderRequests() {
  const matches = filteredRecords(); page = Math.min(page, Math.max(0, Math.ceil(matches.length / pageSize) - 1));
  const start = page * pageSize;
  $('#history-count').textContent = `${format(records.length)} saved requests across ${format(sessions.length)} sessions`;
  $('#requests').innerHTML = matches.slice(start, start + pageSize).map(record => `<tr class="${selected && key(selected) === key(record) ? 'selected' : ''}"><td><button class="request-open" data-request="${esc(key(record))}" aria-label="Inspect request ${esc(record.request_id)} from ${esc(record.client)}">#${esc(record.request_id)} &middot; ${esc(record.model || 'Unknown model')}</button><small>${esc(record.endpoint || record.backend || '-')}</small></td><td>${esc(record.client)}<small>${esc(date(record.timestamp))}</small></td><td><span class="badge ${record.status >= 400 || record.error_type ? 'failed' : ''}">${esc(record.status || '-')}</span><small><span class="badge ${record.trace_available ? 'traced' : ''}">${record.trace_available ? 'Trace' : 'No trace'}</span></small></td><td>${format(record.total_ms)} ms<small>${format(record.output_tokens)} tokens</small></td></tr>`).join('');
  $('#history-empty').hidden = matches.length > 0;
  $('#page-label').textContent = matches.length ? `${start + 1}-${Math.min(start + pageSize, matches.length)} of ${format(matches.length)}` : '0 requests';
  $('#previous').disabled = page === 0; $('#next').disabled = start + pageSize >= matches.length;
  document.querySelectorAll('[data-request]').forEach(button => button.onclick = () => inspect(records.find(record => key(record) === button.dataset.request)));
}
for (const selector of ['#search','#session-filter','#trace-filter']) $(selector).addEventListener(selector === '#search' ? 'input' : 'change', () => {page = 0; renderRequests();});
$('#previous').onclick = () => {page--; renderRequests();};
$('#next').onclick = () => {page++; renderRequests();};
function reviewSession(sessionId) {showView('requests'); $('#session-filter').value = sessionId; $('#search').value = ''; $('#trace-filter').value = ''; page = 0; renderRequests();}
async function setTrace(sessionId, enabled) {
  await api(`/api/sessions/${encodeURIComponent(sessionId)}`, {method:'PATCH',headers:{'content-type':'application/json'},body:JSON.stringify({ended:false,trace:enabled})});
  notify(enabled ? 'Tracing enabled for future requests' : 'Tracing disabled');
  await refresh(true);
}
function renderSessions() {
  $('#session-count').textContent = `${sessions.length} sessions`;
  $('#sessions').innerHTML = sessions.map(session => `<tr><td><div class="session-client">${esc(session.client)}</div><small class="mono">${esc(session.session_id)}</small><small>${session.ended_at ? 'Ended' : 'Active'}</small></td><td>${esc(session.model || '-')}<small>${esc(session.backend)}</small></td><td>${esc(date(session.started_at))}</td><td>${format(session.request_count)}</td><td><label class="check-field"><input type="checkbox" data-trace-session="${esc(session.session_id)}" ${session.trace ? 'checked' : ''} aria-label="Full tracing for ${esc(session.client)}">Full trace</label></td><td><div class="session-controls"><button data-review-session="${esc(session.session_id)}">Review</button><a href="/api/sessions/${encodeURIComponent(session.session_id)}/telemetry">JSONL</a><button class="danger" data-clear-session="${esc(session.session_id)}" title="Clear this session's saved requests and traces" ${inFlightSessions.has(session.session_id) ? 'disabled' : ''}>Clear data</button><button class="danger" data-delete-session="${esc(session.session_id)}" title="Delete this session and all its saved data" ${inFlightSessions.has(session.session_id) ? 'disabled' : ''}>Delete session</button></div></td></tr>`).join('');
  document.querySelectorAll('[data-review-session]').forEach(button => button.onclick = () => reviewSession(button.dataset.reviewSession));
  document.querySelectorAll('[data-clear-session]').forEach(button => button.onclick = () => confirmCleanup(button.dataset.clearSession, false));
  document.querySelectorAll('[data-delete-session]').forEach(button => button.onclick = () => confirmCleanup(button.dataset.deleteSession, true));
  document.querySelectorAll('[data-trace-session]').forEach(input => input.onchange = async () => {
    input.disabled = true;
    try {await setTrace(input.dataset.traceSession, input.checked);} catch (error) {input.checked = !input.checked; notify(error.message);} finally {input.disabled = false;}
  });
}
function confirmCleanup(sessionId, remove) {
  const session = sessions.find(item => item.session_id === sessionId);
  if (!session) return;
  pendingCleanup = {sessionId, remove};
  $('#cleanup-title').textContent = remove ? 'Delete session?' : 'Clear session data?';
  $('#cleanup-message').textContent = remove ? `Permanently delete the ${session.client} session and all its saved requests, telemetry, and traces. This cannot be undone.` : `Permanently remove all saved requests, telemetry, and traces from the ${session.client} session. The session and its tracing settings will be kept. This cannot be undone.`;
  $('#cleanup-session').textContent = session.session_id;
  $('#cleanup-confirm').textContent = remove ? 'Delete session' : 'Clear data';
  $('#cleanup-error').hidden = true;
  $('#cleanup-dialog').showModal(); $('#cleanup-cancel').focus();
}
$('#cleanup-cancel').onclick = () => $('#cleanup-dialog').close();
$('#cleanup-dialog').addEventListener('cancel', event => {if ($('#cleanup-confirm').disabled) event.preventDefault();});
$('#cleanup-form').onsubmit = async event => {
  event.preventDefault();
  if (!pendingCleanup || $('#cleanup-confirm').disabled) return;
  const {sessionId, remove} = pendingCleanup;
  $('#cleanup-confirm').disabled = true; $('#cleanup-cancel').disabled = true; $('#cleanup-error').hidden = true;
  try {
    await api(`/api/sessions/${encodeURIComponent(sessionId)}${remove ? '' : '/data'}`, {method:'DELETE'});
    if (selected?.session_id === sessionId) {
      selectionVersion++; selected = null; selectedTrace = null;
      $('#inspector').innerHTML = '<div class="inspector-placeholder">Select a request</div>';
    }
    $('#cleanup-dialog').close(); pendingCleanup = null;
    notify(remove ? 'Session deleted' : 'Session data cleared'); await refresh(true);
  } catch (error) {$('#cleanup-error').textContent = error.message; $('#cleanup-error').hidden = false;}
  finally {$('#cleanup-confirm').disabled = false; $('#cleanup-cancel').disabled = false;}
};
const inspectorHome = $('#inspector').parentElement;
function syncFullscreenButton() {
  const button = $('#fullscreen-request');
  if (!button) return;
  button.textContent = $('#request-fullscreen').open ? 'Exit fullscreen' : 'Fullscreen';
  button.title = $('#request-fullscreen').open ? 'Exit fullscreen request flow' : 'Fullscreen request flow';
}
function toggleRequestFullscreen() {
  const dialog = $('#request-fullscreen');
  if (dialog.open) {dialog.close(); return;}
  dialog.append($('#inspector'));
  document.body.classList.add('request-focused');
  dialog.showModal(); syncFullscreenButton(); $('#fullscreen-request').focus();
}
$('#request-fullscreen').addEventListener('close', () => {
  inspectorHome.append($('#inspector'));
  document.body.classList.remove('request-focused');
  syncFullscreenButton(); $('#fullscreen-request')?.focus({preventScroll:true});
});
$('#request-fullscreen').addEventListener('keydown', event => {
  if (event.key === 'Escape') {event.preventDefault(); $('#request-fullscreen').close();}
});
function inspectorShell(record) {
  return `<div class="inspector-head"><div class="section-head" style="margin-bottom:0"><h2>Request #${esc(record.request_id)}</h2><div class="actions"><span class="badge ${record.trace_available ? 'traced' : ''}">${record.trace_available ? 'Trace captured' : 'Metadata only'}</span><button id="fullscreen-request" type="button" title="Fullscreen request flow" aria-controls="request-fullscreen">Fullscreen</button></div></div><p class="muted">${esc(record.client)} &middot; ${esc(record.model || '-')} &middot; ${esc(record.backend)}<br><span class="mono">${esc(record.endpoint || '-')}</span></p></div><div class="metrics">${[['Status',record.status || '-'],['Duration',`${format(record.total_ms)} ms`],['First token',`${format(record.ttft_ms)} ms`],['Tokens in / out',`${format(record.input_tokens)} / ${format(record.output_tokens)}`]].map(([label,value]) => `<dl><dt>${label}</dt><dd>${esc(value)}</dd></dl>`).join('')}</div><div id="inspector-body"></div><div class="inspector-footer">${esc(date(record.timestamp))} &middot; Session <span class="mono">${esc(record.session_id)}</span></div>`;
}
async function inspect(record) {
  if (!record) return;
  const version = ++selectionVersion; selected = record; selectedTrace = null; payloadTab = 'request';
  renderRequests(); $('#inspector').innerHTML = inspectorShell(record);
  $('#fullscreen-request').onclick = toggleRequestFullscreen; syncFullscreenButton();
  if (matchMedia('(max-width:1050px)').matches) $('#inspector').scrollIntoView({behavior:'smooth',block:'start'});
  if (!record.trace_available) {renderUntraced(); return;}
  $('#inspector-body').innerHTML = '<div class="notice" role="status">Loading captured payload...</div>';
  try {
    const trace = await api(traceURL(record));
    if (version !== selectionVersion) return;
    selectedTrace = trace; renderTrace();
  } catch (error) {
    if (version !== selectionVersion) return;
    $('#inspector-body').innerHTML = `<div class="notice error">${esc(error.message)}<p><button id="retry-trace">Retry</button></p></div>`;
    $('#retry-trace').onclick = () => inspect(selected);
  }
}
function renderUntraced() {
  const session = sessions.find(item => item.session_id === selected.session_id);
  $('#inspector-body').innerHTML = `<div class="notice"><h3>No payload captured</h3><p>This request has no saved trace. ${session?.trace ? 'Tracing is enabled for future requests.' : 'Enable tracing to capture future request and response payloads.'} Past payloads cannot be recovered.</p>${session && !session.trace ? '<button class="primary" id="enable-trace">Enable tracing</button>' : ''}</div><div class="tabs"><button aria-selected="true">Metadata</button></div><div class="payload-area"><pre class="wrap" id="metadata-only"></pre></div>`;
  $('#metadata-only').textContent = JSON.stringify(selected, null, 2);
  if ($('#enable-trace')) $('#enable-trace').onclick = async () => {try {await setTrace(selected.session_id, true); renderUntraced();} catch (error) {notify(error.message);}};
}
function payloadText() {return JSON.stringify(payloadTab === 'metadata' ? selectedTrace.metadata : selectedTrace[payloadTab], null, 2) ?? 'null';}
function download(content, filename) {
  const url = URL.createObjectURL(new Blob([content], {type:'application/json;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function conversationEntries() {
  const request = selectedTrace.request || {}, response = selectedTrace.response;
  const entries = [];
  const add = (role, content) => {if (content != null) entries.push({role, content:typeof content === 'string' ? content : JSON.stringify(content, null, 2)});};
  if (request.system) add('System', request.system);
  if (request.instructions) add('Instructions', request.instructions);
  for (const message of request.messages || []) {add(message.role || 'Message', message.content ?? message); if (message.tool_calls) add('Tool calls', message.tool_calls);}
  if (request.prompt) add('Prompt', request.prompt);
  if (Array.isArray(request.input)) {
    for (const item of request.input) add(item.role || item.type || 'Input', item.content ?? item);
  } else if (request.input) add('Input', request.input);
  if (Array.isArray(response)) {
    const reasoning = response.filter(event => event.type === 'response.reasoning_summary_text.delta').map(event => event.delta || '').join('');
    if (reasoning) add('Reasoning summary', reasoning);
    const text = response.map(event => event.type === 'response.output_text.delta' ? event.delta : event.message?.content ?? event.choices?.[0]?.delta?.content ?? event.delta?.text ?? event.response?.output_text ?? (typeof event.response === 'string' ? event.response : '')).join('');
    if (text) add('Assistant', text); else add('Response events', response);
    for (const event of response) {
      if (event.type === 'response.output_item.done' && event.item?.type === 'function_call') add('Tool call', event.item);
      for (const choice of event.choices || []) {if (choice.delta?.tool_calls) add('Tool call delta', choice.delta.tool_calls);}
    }
  } else if (response) {
    add('Assistant', response.message?.content ?? response.choices?.[0]?.message?.content ?? response.content ?? response.output ?? response.response ?? response);
  }
  return entries;
}
function renderTrace() {
  $('#inspector-body').innerHTML = `<div class="tabs" role="tablist" aria-label="Captured payload">${['request','response','conversation','metadata'].map(tab => `<button id="tab-${tab}" role="tab" aria-controls="payload-panel" aria-selected="${tab === payloadTab}" data-payload-tab="${tab}">${tab[0].toUpperCase() + tab.slice(1)}</button>`).join('')}</div><div class="payload-toolbar"><span id="payload-size"></span><div class="actions"><label id="wrap-control"><input id="wrap" type="checkbox" checked>Wrap</label><button id="copy-payload">Copy JSON</button><button id="download-payload">Download</button><button id="download-trace" title="Download complete captured trace">Full trace</button></div></div><div id="payload-panel" class="payload-area" role="tabpanel" aria-labelledby="tab-${payloadTab}"></div>`;
  document.querySelectorAll('[data-payload-tab]').forEach(button => button.onclick = () => {payloadTab = button.dataset.payloadTab; renderTrace(); $(`#tab-${payloadTab}`).focus({preventScroll:true});});
  const conversation = payloadTab === 'conversation';
  $('#wrap-control').hidden = conversation; $('#copy-payload').hidden = conversation; $('#download-payload').hidden = conversation;
  if (conversation) {
    $('#payload-size').textContent = 'Message content';
    const entries = conversationEntries();
    if (!entries.length) $('#payload-panel').textContent = 'No message content in this trace.';
    for (const entry of entries) {
      const section = document.createElement('details'); section.className = 'conversation-entry'; section.open = true;
      const summary = document.createElement('summary');
      const heading = document.createElement('h3'); heading.textContent = entry.role;
      const content = document.createElement('pre'); content.textContent = entry.content;
      summary.append(heading); section.append(summary, content); $('#payload-panel').append(section);
    }
  } else {
    const text = payloadText(), pre = document.createElement('pre'); pre.id = 'payload'; pre.className = 'wrap'; pre.textContent = text; $('#payload-panel').append(pre);
    $('#payload-size').textContent = `${format(new TextEncoder().encode(text).length)} bytes`;
    $('#wrap').onchange = event => pre.classList.toggle('wrap', event.target.checked);
    $('#copy-payload').onclick = async () => {try {await navigator.clipboard.writeText(text); notify('Payload copied');} catch {notify('Clipboard unavailable. Download the payload instead.');}};
    $('#download-payload').onclick = () => download(text, `${selected.session_id}-${selected.request_id}-${payloadTab}.json`);
  }
  $('#download-trace').onclick = () => download(JSON.stringify(selectedTrace, null, 2), `${selected.session_id}-${selected.request_id}-trace.json`);
}
async function refresh(force = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    const [status, sessionList] = await Promise.all([api('/api/status'), api('/api/sessions')]); sessions = sessionList;
    inFlightSessions = new Set(status.current_requests.map(record => record.session_id));
    $('#connection').classList.remove('offline'); $('#connection-text').textContent = `${status.active_sessions} active sessions`;
    $('#stats').innerHTML = [['Requests',status.total_requests],['Input tokens',status.total_input_tokens],['Output tokens',status.total_output_tokens],['In flight',status.current_requests.length]].map(([label,value]) => `<div><div class="label">${label}</div><div class="value">${format(value)}</div></div>`).join('');
    $('#current').hidden = !status.current_requests.length;
    $('#current').textContent = status.current_requests.map(record => `#${record.request_id} / ${record.client} / ${record.model} / ${format(record.response_bytes)} bytes received`).join('  |  ');
    const filter = $('#session-filter').value;
    $('#session-filter').innerHTML = '<option value="">All sessions</option>' + sessions.map(session => `<option value="${esc(session.session_id)}">${esc(session.client)} / ${esc(session.session_id.slice(0,8))}</option>`).join('');
    $('#session-filter').value = filter;
    const backend = $('#backend').value;
    $('#backend').innerHTML = Object.keys(status.backends).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
    $('#backend').value = backend && status.backends[backend] ? backend : status.default_backend;
    if (force || lastCount !== status.total_requests || status.current_requests.length) {
      records = await api('/api/requests'); lastCount = records.length === status.total_requests ? status.total_requests : -1;
      if (selected) {
        const updated = records.find(record => key(record) === key(selected));
        if (updated) {const newlyTraced = !selected.trace_available && updated.trace_available; selected = updated; if (newlyTraced) await inspect(updated);}
      }
    }
    renderRequests(); renderSessions();
  } catch (error) {
    $('#connection').classList.add('offline'); $('#connection-text').textContent = 'Connection unavailable';
    if (force || lastCount === -1) notify(error.message);
  } finally {refreshing = false;}
}
$('#refresh').onclick = () => refresh(true);
function backendRow(name = '', backend = {url:''}) {return `<div class="backend-row"><input class="backend-name" aria-label="Backend name" placeholder="Name" value="${esc(name)}" required><input class="backend-url" aria-label="Ollama URL" type="url" placeholder="http://host:11434" value="${esc(backend.url)}" required><button type="button" class="remove-backend">Remove</button></div>`;}
function syncDefaults() {
  const current = $('#default-backend').value || selectedDefault, names = [...document.querySelectorAll('.backend-name')].map(input => input.value.trim()).filter(Boolean);
  $('#default-backend').innerHTML = names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  if (names.includes(current)) $('#default-backend').value = current;
  selectedDefault = $('#default-backend').value;
}
function bindBackendRows() {
  document.querySelectorAll('.remove-backend').forEach(button => button.onclick = () => {button.closest('.backend-row').remove(); syncDefaults();});
  document.querySelectorAll('.backend-name').forEach(input => input.oninput = syncDefaults);
}
function showConfigMessage(message, warn = false) {$('#config-message').textContent = message; $('#config-message').className = warn ? 'warn' : 'ok';}
async function loadConfig() {
  try {
    const result = await api('/api/config'), config = result.config;
    $('#listen-host').value = config.proxy.listen_host; $('#listen-port').value = config.proxy.listen_port; $('#proxy-url').value = config.proxy.url;
    selectedDefault = config.default_backend; $('#backend-rows').innerHTML = Object.entries(config.backends).map(([name,backend]) => backendRow(name,backend)).join('');
    bindBackendRows(); syncDefaults(); $('#default-backend').value = config.default_backend;
    if (result.restart_required) showConfigMessage('Saved. Restart the proxy to apply its listen address.', true);
  } catch (error) {showConfigMessage(error.message, true);}
}
$('#add-backend').onclick = () => {$('#backend-rows').insertAdjacentHTML('beforeend', backendRow()); bindBackendRows(); syncDefaults();};
$('#network-config').onsubmit = async event => {
  event.preventDefault(); const backends = {};
  for (const row of document.querySelectorAll('.backend-row')) {
    const name = row.querySelector('.backend-name').value.trim();
    if (Object.hasOwn(backends, name)) {showConfigMessage('Backend names must be unique.', true); return;}
    Object.defineProperty(backends, name, {value:{type:'ollama',url:row.querySelector('.backend-url').value.trim()},enumerable:true});
  }
  const body = {proxy:{listen_host:$('#listen-host').value.trim(),listen_port:Number($('#listen-port').value),url:$('#proxy-url').value.trim()},default_backend:$('#default-backend').value,backends};
  showConfigMessage('Saving...');
  try {const result = await api('/api/config', {method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify(body)}); showConfigMessage(result.restart_required ? 'Saved. Restart the proxy to apply its listen address.' : 'Saved and applied.',result.restart_required); await refresh(true);} catch (error) {showConfigMessage(error.message,true);}
};
$('#new-session').onsubmit = async event => {
  event.preventDefault(); const data = new FormData(event.target);
  try {const result = await api('/api/sessions', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({client:data.get('client'),model:data.get('model') || null,backend:data.get('backend'),trace:data.has('trace')})}); $('#created').textContent = result.session_id; await refresh(true);} catch (error) {$('#created').textContent = error.message;}
};
loadConfig(); refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>'''