"""The dashboard's single self-contained HTML page.

No build step, no CDN, no external assets -- everything (CSS, JS) is
inlined so this works offline on a laptop, per the brief. The page
polls `/api/summary` and `/api/alerts` and renders six panels matching
the spec's priority order: source health, coverage, live alerts, volume
over time, notification log, registry freshness.

The disclaimer and the heuristic-classification caveat are written into
the static HTML below (not only fetched at runtime), so they are always
present even before the first JS poll completes, and so a plain `GET /`
(no JS execution) already carries both -- see docs/DECISIONS.md.
"""

from __future__ import annotations

from alertmux.schema import DISCLAIMER

HEURISTIC_CAVEAT_TEXT = (
    "Hazard-family coverage is heuristic: each alert's free-text event "
    "field is matched against a fixed keyword table, not an authoritative "
    "classification. It can misfile or fail to classify wording the table "
    "has not seen, and it never changes alert data anywhere else."
)


def render_page() -> str:
    return _PAGE_TEMPLATE.format(
        disclaimer=DISCLAIMER,
        heuristic_caveat=HEURISTIC_CAVEAT_TEXT,
    )


_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>alertmux dashboard</title>
<style>
  :root {{
    --bg: #0b0d12; --panel: #12151c; --border: #262b36; --text: #e6e9ef;
    --muted: #8b93a3; --ok: #2fbf71; --bad: #e5484d; --warn: #e2a03f;
    --accent: #4f8cff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); padding: 1.5rem;
  }}
  h1 {{ font-size: 1.3rem; margin: 0 0 0.25rem 0; }}
  .sub {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; }}
  .disclaimer {{
    background: #1b2030; border: 1px solid var(--border); border-left: 4px solid var(--accent);
    padding: 0.6rem 0.9rem; border-radius: 6px; font-size: 0.85rem; margin-bottom: 1rem;
  }}
  #partial-banner, #run-banner {{
    display: none; padding: 0.6rem 0.9rem; border-radius: 6px; font-size: 0.9rem;
    margin-bottom: 0.75rem; border: 1px solid var(--bad); background: #2a1216; color: #ffb4b7;
    font-weight: 600;
  }}
  #partial-banner.show, #run-banner.show {{ display: block; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 1rem; }}
  .panel {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 1rem; min-height: 120px;
  }}
  .panel h2 {{ font-size: 1rem; margin: 0 0 0.6rem 0; }}
  .caveat {{ color: var(--muted); font-size: 0.75rem; margin-bottom: 0.5rem; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th, td {{ text-align: left; padding: 0.3rem 0.4rem; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 500; }}
  .pill {{ display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.72rem; }}
  .pill.ok {{ background: rgba(47,191,113,0.15); color: var(--ok); }}
  .pill.bad {{ background: rgba(229,72,77,0.15); color: var(--bad); }}
  .uncovered {{ color: var(--bad); font-weight: 600; }}
  .filters input, .filters select {{
    background: #0f1219; border: 1px solid var(--border); color: var(--text);
    padding: 0.3rem 0.5rem; border-radius: 4px; font-size: 0.8rem; margin-right: 0.4rem;
  }}
  .no-data {{ color: var(--muted); font-style: italic; }}
  .small {{ font-size: 0.75rem; color: var(--muted); }}
</style>
</head>
<body>
  <h1>alertmux dashboard</h1>
  <div class="sub">Local, read-only, single operator. Operational confidence, not presentation.</div>
  <div class="disclaimer" id="disclaimer">{disclaimer}</div>
  <div id="partial-banner">This fetch is PARTIAL: one or more sources failed, truncated, or quarantined records. Data below is incomplete.</div>
  <div id="run-banner">The most recent notifier run had delivery FAILURES. See the notification log below.</div>

  <div class="grid">
    <div class="panel" id="panel-sources">
      <h2>1. Source health</h2>
      <table id="sources-table"><thead>
        <tr><th>Source</th><th>Status</th><th>Latency</th><th>Alerts</th><th>Consecutive failures</th><th>Last OK</th></tr>
      </thead><tbody></tbody></table>
    </div>

    <div class="panel" id="panel-coverage">
      <h2>2. Coverage by hazard family</h2>
      <div class="caveat" id="heuristic-caveat">{heuristic_caveat}</div>
      <table id="coverage-table"><thead>
        <tr><th>Hazard family</th><th>Sources</th></tr>
      </thead><tbody></tbody></table>
      <div class="small" id="uncovered-note"></div>
    </div>

    <div class="panel" id="panel-alerts" style="grid-column: 1 / -1;">
      <h2>3. Live alerts</h2>
      <div class="filters">
        <input id="f-authority" placeholder="authority" />
        <input id="f-severity" placeholder="severity" />
        <input id="f-event" placeholder="event contains" />
        <button id="f-apply">Filter</button>
      </div>
      <table id="alerts-table"><thead>
        <tr><th>Event</th><th>Authority</th><th>Severity</th><th>Source</th><th>Area</th><th>Expires</th></tr>
      </thead><tbody></tbody></table>
      <div class="small" id="alerts-count"></div>
    </div>

    <div class="panel" id="panel-volume">
      <h2>4. Volume over time (per source)</h2>
      <div id="volume-content" class="no-data">Loading...</div>
    </div>

    <div class="panel" id="panel-notifications">
      <h2>5. Notification log</h2>
      <table id="notify-table"><thead>
        <tr><th>Time</th><th>Result</th><th>Sent</th><th>Suppressed</th><th>Failures</th></tr>
      </thead><tbody></tbody></table>
      <div class="small" id="notify-empty"></div>
    </div>

    <div class="panel" id="panel-registry">
      <h2>6. Registry freshness</h2>
      <div id="registry-content" class="small">Loading...</div>
    </div>
  </div>

<script>
function el(tag, cls, text) {{
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}}

function fmtBool(ok) {{
  const span = el('span', 'pill ' + (ok ? 'ok' : 'bad'), ok ? 'ok' : 'down');
  return span;
}}

async function loadSummary() {{
  const res = await fetch('/api/summary');
  const data = await res.json();

  document.getElementById('partial-banner').classList.toggle('show', !!data.partial);

  const failedRun = data.last_run_ok === false;
  document.getElementById('run-banner').classList.toggle('show', failedRun);

  const sBody = document.querySelector('#sources-table tbody');
  sBody.innerHTML = '';
  (data.sources || []).forEach(function(s) {{
    const tr = document.createElement('tr');
    const tdName = el('td', null, s.source_id);
    const tdStatus = el('td'); tdStatus.appendChild(fmtBool(s.ok));
    if (s.error) tdStatus.appendChild(el('span', 'small', ' ' + s.error));
    const tdLatency = el('td', null, s.latency_ms == null ? '-' : (s.latency_ms + ' ms'));
    const tdAlerts = el('td', null, String(s.alert_count));
    const tdFail = el('td', null, String(s.consecutive_failures));
    if (s.consecutive_failures > 0) tdFail.classList.add('uncovered');
    const tdLastOk = el('td', null, s.last_ok_at || 'never');
    tr.append(tdName, tdStatus, tdLatency, tdAlerts, tdFail, tdLastOk);
    sBody.appendChild(tr);
  }});

  const cBody = document.querySelector('#coverage-table tbody');
  cBody.innerHTML = '';
  const families = data.all_hazard_families || [];
  families.forEach(function(family) {{
    const sources = (data.hazard_coverage || {{}})[family] || [];
    const tr = document.createElement('tr');
    const tdFamily = el('td', sources.length ? null : 'uncovered', family);
    const tdSources = el('td', null, sources.length ? sources.join(', ') : 'NO SOURCE');
    tr.append(tdFamily, tdSources);
    cBody.appendChild(tr);
  }});
  const uncovered = data.uncovered_hazards || [];
  document.getElementById('uncovered-note').textContent = uncovered.length
    ? ('No source at all for: ' + uncovered.join(', ') + '. This means no source covers that family, not that no hazard exists.')
    : 'Every tracked hazard family has at least one contributing source this fetch.';

  const volumeContent = document.getElementById('volume-content');
  if (!data.volume_recording_started_at || !(data.volume_history || []).length) {{
    volumeContent.className = 'no-data';
    volumeContent.textContent = 'No data yet -- volume history begins when the dashboard starts recording.';
  }} else {{
    volumeContent.className = '';
    volumeContent.innerHTML = '';
    const note = el('div', 'small', 'Recording since ' + data.volume_recording_started_at + ' (' + data.volume_history.length + ' snapshot(s)).');
    volumeContent.appendChild(note);
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    thead.innerHTML = '<tr><th>Time</th><th>Total alerts</th><th>Partial</th></tr>';
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    data.volume_history.slice(-20).forEach(function(snap) {{
      const tr = document.createElement('tr');
      tr.append(
        el('td', null, snap.timestamp),
        el('td', null, String(snap.total_alerts)),
        el('td', null, snap.partial ? 'yes' : 'no')
      );
      tbody.appendChild(tr);
    }});
    table.appendChild(tbody);
    volumeContent.appendChild(table);
  }}

  const nBody = document.querySelector('#notify-table tbody');
  nBody.innerHTML = '';
  const runs = data.notification_runs || [];
  if (!runs.length) {{
    document.getElementById('notify-empty').textContent = 'No notifier runs recorded yet.';
  }} else {{
    document.getElementById('notify-empty').textContent = '';
    runs.slice().reverse().forEach(function(run) {{
      const tr = document.createElement('tr');
      const tdTime = el('td', null, run.timestamp);
      const tdResult = el('td'); tdResult.appendChild(fmtBool(run.ok));
      const sentTotal = Object.values(run.matched_by_rule || {{}}).reduce(function(a, b) {{ return a + b; }}, 0);
      const tdSent = el('td', null, String(run.sent_count));
      const suppressedTotal = Object.values(run.suppressed_by_rate_limit || {{}}).reduce(function(a, b) {{ return a + b; }}, 0);
      const tdSuppressed = el('td', null, String(suppressedTotal));
      const tdFailures = el('td', run.failures && run.failures.length ? 'uncovered' : null, (run.failures || []).join('; '));
      tr.append(tdTime, tdResult, tdSent, tdSuppressed, tdFailures);
      nBody.appendChild(tr);
    }});
  }}

  const reg = data.registry || {{}};
  const regEl = document.getElementById('registry-content');
  if (!reg.available) {{
    regEl.textContent = 'Registry unavailable: ' + (reg.error || 'unknown error');
  }} else {{
    let text = 'Fetched ' + reg.fetched_at + ', cache age ' + Math.round(reg.cache_age_seconds) + 's.';
    if (reg.fetch_error) text += ' Last refresh failed (' + reg.fetch_error + '), serving previous cache.';
    regEl.textContent = text;
  }}
}}

async function loadAlerts() {{
  const authority = document.getElementById('f-authority').value.trim();
  const severity = document.getElementById('f-severity').value.trim();
  const event = document.getElementById('f-event').value.trim();
  const params = new URLSearchParams();
  if (authority) params.set('authority', authority);
  if (severity) params.set('severity', severity);
  if (event) params.set('event', event);
  const res = await fetch('/api/alerts?' + params.toString());
  const data = await res.json();
  const body = document.querySelector('#alerts-table tbody');
  body.innerHTML = '';
  data.alerts.forEach(function(a) {{
    const tr = document.createElement('tr');
    tr.append(
      el('td', null, a.event || ''),
      el('td', null, a.authority || ''),
      el('td', null, a.severity || 'unmapped'),
      el('td', null, a.source_id || ''),
      el('td', null, a.area_description || ''),
      el('td', null, a.expires || '')
    );
    body.appendChild(tr);
  }});
  document.getElementById('alerts-count').textContent = data.count + ' alert(s) shown' + (data.partial ? ' (fetch was partial)' : '');
}}

document.getElementById('f-apply').addEventListener('click', loadAlerts);

loadSummary();
loadAlerts();
setInterval(loadSummary, 30000);
</script>
</body>
</html>
"""
