// Edit toggle
function toggleEdit(btn) {
  const body = document.getElementById('rcaBody');
  if (!body) return;
  const on = body.contentEditable !== 'true';
  body.contentEditable = on ? 'true' : 'false';
  body.classList.toggle('edit-mode', on);
  btn.textContent = on ? '✓ Done Editing' : '✏ Edit';
}

// ── Timezone conversion ──────────────────────────────────────
const TZ_LABELS = {
  'America/Los_Angeles':  'PST',
  'America/New_York':     'EST',
  'America/Chicago':      'CST',
  'America/Denver':       'MST',
  'Asia/Kolkata':         'IST',
  'America/Sao_Paulo':    'BRT',
  'America/Panama':       'EST',
  'America/Indiana/Indianapolis': 'EST',
  'America/Phoenix':      'MST',
  'America/Anchorage':    'AKST',
  'Pacific/Honolulu':     'HST',
  'Europe/London':        'GMT',
  'Europe/Paris':         'CET',
  'Europe/Berlin':        'CET',
  'Asia/Tokyo':           'JST',
  'Asia/Shanghai':        'CST',
  'Asia/Singapore':       'SGT',
  'Australia/Sydney':     'AEST',
  'UTC':                  'UTC',
};

let currentTz = 'America/Los_Angeles';

function formatInTz(isoUtc, tz) {
  try {
    const d = new Date(isoUtc);
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    // en-CA gives YYYY-MM-DD format which is clean
    const parts = fmt.formatToParts(d);
    const get = t => parts.find(p => p.type === t)?.value || '';
    const label = TZ_LABELS[tz] || tz;
    return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')} ${label}`;
  } catch (e) {
    return isoUtc;
  }
}

function applyTz(tz) {
  currentTz = tz;
  document.querySelectorAll('.tz-ts[data-utc]').forEach(el => {
    el.textContent = formatInTz(el.dataset.utc, tz);
  });
  // Update Time Format cell — try id first, then scan table for "Time Format" label row
  let tzDisplay = document.getElementById('tz-format-display');
  if (!tzDisplay) {
    document.querySelectorAll('td, th').forEach(cell => {
      if (cell.textContent.trim().replace(/^\d+\.\s*/, '').toLowerCase() === 'time format') {
        const sibling = cell.nextElementSibling;
        if (sibling) tzDisplay = sibling;
      }
    });
  }
  if (tzDisplay) {
    // Preserve any source badges, only replace the text node
    const badge = tzDisplay.querySelector('.source-badge');
    tzDisplay.textContent = TZ_LABELS[tz] || tz;
    if (badge) tzDisplay.appendChild(badge);
  }
  // Update active button state
  document.querySelectorAll('.tz-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tz === tz);
  });
  // Update toolbar label
  const label = document.getElementById('tzLabel');
  if (label) label.textContent = TZ_LABELS[tz] || tz;
}

function toggleTzSidebar() {
  const sidebar = document.getElementById('tzSidebar');
  const btn     = document.getElementById('btnTzToggle');
  if (!sidebar) return;
  const isOpen = sidebar.classList.toggle('open');
  if (btn) btn.classList.toggle('active', isOpen);
}

// ── Event delegation — handles all injected innerHTML buttons ──
document.addEventListener('click', function(e) {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.classList.contains('btn-pdf'))    { window.print(); return; }
  if (btn.classList.contains('btn-edit'))   { toggleEdit(btn); return; }
  if (btn.id === 'btnTzToggle')             { toggleTzSidebar(); return; }
  if (btn.id === 'btnGdoc')                 { createGoogleDoc(btn); return; }
  if (btn.classList.contains('tz-btn') && btn.dataset.tz) {
    applyTz(btn.dataset.tz);
    return;
  }
});

async function createGoogleDoc(btn) {
  btn.disabled = true;
  const origText = btn.innerHTML;
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Creating…';
  try {
    const { rcaPreviewHtml, rcaPreviewCase } = await new Promise(r =>
      chrome.storage.local.get(['rcaPreviewHtml', 'rcaPreviewCase'], r)
    );
    const res = await fetch('http://127.0.0.1:3001/create-gdoc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html: rcaPreviewHtml, case_number: rcaPreviewCase || '' }),
    });
    const text = await res.text();
    let data;
    try { data = JSON.parse(text); } catch (_) {
      throw new Error(`Server returned non-JSON (status ${res.status}). Restart the backend: launchctl kickstart -k gui/$(id -u)/com.rca.backend`);
    }
    if (data.url) {
      window.open(data.url, '_blank');
    } else {
      alert('Google Doc creation failed: ' + (data.error || 'Unknown error'));
    }
  } catch (err) {
    alert('Google Doc creation failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origText;
  }
}

// With/Without source toggle
document.addEventListener('change', function(e) {
  const input = e.target;
  if (!input.matches('input[name="srcMode"]')) return;
  const page = document.querySelector('.page');
  if (!page) return;
  page.classList.toggle('no-source', input.classList.contains('btn-src-off'));
});

// ── Load RCA from storage ──────────────────────────────────────
chrome.storage.local.get(['rcaPreviewHtml', 'rcaPreviewCase'], ({ rcaPreviewHtml, rcaPreviewCase }) => {
  if (!rcaPreviewHtml) {
    document.getElementById('loading').innerHTML =
      '<p style="color:#BA0517;padding:40px;font-size:14px;">No RCA found. Generate one first.</p>';
    return;
  }

  document.title = 'RCA — Case ' + (rcaPreviewCase || '');
  document.getElementById('loading').style.display = 'none';

  const container = document.getElementById('content');
  // display:contents makes this wrapper transparent to layout so the
  // injected HTML's flex children (tz-sidebar + .page) become direct
  // flex children of <body>, matching the original intended structure.
  container.style.cssText = 'display:contents;';

  const parser = new DOMParser();
  const doc    = parser.parseFromString(rcaPreviewHtml, 'text/html');

  // Inject styles
  doc.querySelectorAll('style').forEach(s => {
    const el = document.createElement('style');
    el.textContent = s.textContent;
    document.head.appendChild(el);
  });

  // Inject body content — strip scripts, remove inline event handlers (CSP)
  // tmp also needs display:contents so tz-sidebar/.page become direct flex
  // children of <body> rather than being wrapped in an opaque block div.
  const tmp = document.createElement('div');
  tmp.style.cssText = 'display:contents;';
  tmp.innerHTML = doc.body.innerHTML;
  tmp.querySelectorAll('script').forEach(s => s.remove());
  tmp.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes)
      .filter(a => a.name.startsWith('on'))
      .forEach(a => el.removeAttribute(a.name));
  });
  container.appendChild(tmp);

  // Apply default timezone from data-default-tz on h1 (set by Claude from OrgCS support_available_timezone__c)
  const h1 = document.querySelector('h1[data-default-tz]');
  const defaultTz = (h1 && h1.dataset.defaultTz && h1.dataset.defaultTz.includes('/'))
    ? h1.dataset.defaultTz
    : 'America/Los_Angeles';
  applyTz(defaultTz);
});
