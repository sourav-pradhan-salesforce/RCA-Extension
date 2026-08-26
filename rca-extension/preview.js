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
  'America/Los_Angeles': 'PST',
  'America/New_York':    'EST',
  'America/Chicago':     'CST',
  'America/Denver':      'MST',
  'Asia/Kolkata':        'IST',
  'America/Sao_Paulo':   'BRT',
  'UTC':                 'UTC',
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
  if (btn.classList.contains('tz-btn') && btn.dataset.tz) {
    applyTz(btn.dataset.tz);
    return;
  }
});

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
  container.style.display = 'block';

  const parser = new DOMParser();
  const doc    = parser.parseFromString(rcaPreviewHtml, 'text/html');

  // Inject styles
  doc.querySelectorAll('style').forEach(s => {
    const el = document.createElement('style');
    el.textContent = s.textContent;
    document.head.appendChild(el);
  });

  // Inject body content — strip scripts, remove inline event handlers (CSP)
  const tmp = document.createElement('div');
  tmp.innerHTML = doc.body.innerHTML;
  tmp.querySelectorAll('script').forEach(s => s.remove());
  tmp.querySelectorAll('*').forEach(el => {
    Array.from(el.attributes)
      .filter(a => a.name.startsWith('on'))
      .forEach(a => el.removeAttribute(a.name));
  });
  container.appendChild(tmp);

  // Apply default timezone (PST) to all tz-ts spans once content is in DOM
  applyTz('America/Los_Angeles');
});
