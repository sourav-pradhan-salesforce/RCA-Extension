/* ── Dark Mode ── */
(function() {
  const saved = localStorage.getItem('rcaTheme') || 'light';
  if (saved === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
})();

/* ── Particle Canvas ── */
(function() {
  const canvas = document.getElementById('particleCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, particles;

  function resize() {
    W = canvas.width  = canvas.offsetWidth  || 360;
    H = canvas.height = canvas.offsetHeight || 400;
  }

  function initParticles() {
    particles = Array.from({ length: 28 }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      r: 1 + Math.random() * 2,
      dx: (Math.random() - 0.5) * 0.4,
      dy: (Math.random() - 0.5) * 0.4,
      opacity: 0.2 + Math.random() * 0.4,
    }));
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    const color = isDark ? '100,160,255' : '1,118,211';
    particles.forEach(p => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${color},${p.opacity})`;
      ctx.fill();
      p.x += p.dx; p.y += p.dy;
      if (p.x < 0 || p.x > W) p.dx *= -1;
      if (p.y < 0 || p.y > H) p.dy *= -1;
    });
    requestAnimationFrame(draw);
  }

  resize();
  initParticles();
  draw();
  window.addEventListener('resize', () => { resize(); initParticles(); });
})();

/* ── View Router ── */
const views = {
  main:     document.getElementById('mainView'),
  loading:  document.getElementById('loadingView'),
  settings: document.getElementById('settingsView'),
};

function showView(name) {
  Object.values(views).forEach(v => { if (v) v.classList.remove('active'); });
  if (views[name]) views[name].classList.add('active');
}

/* ── Toast ── */
const toastEl = document.getElementById('toast');
let toastTimer;
function showToast(msg, type) {
  clearTimeout(toastTimer);
  toastEl.textContent = msg;
  toastEl.className = 'toast show' + (type ? ' ' + type : '');
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3500);
}

/* ── Dark Mode Toggle ── */
document.getElementById('darkToggleBtn').addEventListener('click', () => {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const next = isDark ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('rcaTheme', next);
  updateMoonSun(!isDark);
});

function updateMoonSun(isDark) {
  const sun  = document.querySelector('.icon-sun');
  const moon = document.querySelector('.icon-moon');
  if (sun)  sun.style.display  = isDark ? 'none' : '';
  if (moon) moon.style.display = isDark ? '' : 'none';
}

updateMoonSun(document.documentElement.getAttribute('data-theme') === 'dark');

/* ── Settings ── */
document.getElementById('settingsBtn').addEventListener('click', () => { checkProxyStatus(); showView('settings'); });
document.getElementById('settingsBackBtn').addEventListener('click', () => showView('main'));
document.getElementById('checkProxyBtn').addEventListener('click', checkProxyStatus);

async function checkProxyStatus() {
  const badge  = document.getElementById('proxyStatusBadge');
  const detail = document.getElementById('proxyDetail');
  badge.textContent = 'Checking…';
  badge.className = 'status-badge checking';
  detail.className = 'hidden';
  try {
    const res  = await fetch('http://127.0.0.1:3001/health', { signal: AbortSignal.timeout(3000) });
    const json = await res.json();
    if (json.status === 'ok') { badge.textContent = 'Online ✓'; badge.className = 'status-badge online'; }
    else throw new Error();
  } catch (_) {
    badge.textContent = 'Offline';
    badge.className   = 'status-badge offline';
    detail.className  = '';
    detail.style.cssText = 'padding:10px 12px;border-radius:8px;font-size:12px;background:#FFF0F0;color:#C41E3A;border:1px solid #FFB3B3;margin-top:4px;';
    detail.textContent = 'Backend not running — the LaunchAgent should auto-start it on login.';
  }
}

/* ── Demo Button ── */
document.getElementById('demoBtn').addEventListener('click', async () => {
  const caseNumber = document.getElementById('caseNumber').value.trim() || 'DEMO-001';
  showView('loading');
  resetSteps();
  resetConsole();
  hideLoadingError();
  startTimer();
  await simulateSteps();
  stopTimer();
  openPreviewTab(buildPreviewPage(getDemoRCA(caseNumber), true), caseNumber);
  showView('main');
});

/* ── Generate RCA ── */
document.getElementById('errorBackBtn').addEventListener('click', () => { stopTimer(); showView('main'); });

document.getElementById('generateBtn').addEventListener('click', async () => {
  const caseNumber = document.getElementById('caseNumber').value.trim();
  if (!caseNumber) { showToast('Please enter a case number', 'error'); return; }

  showView('loading');
  resetSteps();
  resetConsole();
  hideLoadingError();
  startTimer();
  startAutoSteps();

  try {
    const html = await fetchRCA(caseNumber);
    stopTimer();
    stopAutoSteps();
    ['slack','orgcs','org62','gus','public','generate'].forEach(s => setStep(s, 'done'));
    openPreviewTab(buildPreviewPage(html, false), caseNumber);
    showView('main');
  } catch (err) {
    stopTimer();
    stopAutoSteps();
    showLoadingError(err.message || 'Generation failed');
  }
});

/* ── Timer ── */
let timerInterval = null;
let timerStart    = null;

function startTimer() {
  timerStart = Date.now();
  const el = document.getElementById('loadingTimer');
  if (el) { el.textContent = '0:00'; el.style.display = 'block'; }
  timerInterval = setInterval(() => {
    const s  = Math.floor((Date.now() - timerStart) / 1000);
    const mm = String(Math.floor(s / 60)).padStart(1, '0');
    const ss = String(s % 60).padStart(2, '0');
    if (el) el.textContent = mm + ':' + ss;
  }, 1000);
}

function stopTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
}

/* ── Auto-step animation ── */
let autoStepInterval = null;
let autoStepStart    = null;

const STEPS = [
  { step: 'slack',    msg: 'Searching Slack SEV channels & swarm threads…', atMs: 0     },
  { step: 'orgcs',    msg: 'Loading OrgCS case data…',                      atMs: 8000  },
  { step: 'org62',    msg: 'Loading Org62 account data…',                   atMs: 22000 },
  { step: 'gus',      msg: 'Searching GUS work items…',                     atMs: 36000 },
  { step: 'public',   msg: 'Searching Knowledge Articles & Known Issues…',  atMs: 50000 },
  { step: 'generate', msg: 'Generating & validating RCA…',                  atMs: 65000 },
];

function startAutoSteps() {
  autoStepStart    = Date.now();
  autoStepInterval = setInterval(() => {
    const elapsed = Date.now() - autoStepStart;
    let current = 0;
    for (let i = 0; i < STEPS.length; i++) {
      if (elapsed >= STEPS[i].atMs) current = i;
    }
    STEPS.forEach((s, i) => {
      if (i < current)        setStep(s.step, 'done');
      else if (i === current) setStep(s.step, 'active');
      else                    setStep(s.step, '');
    });
    setStatus(STEPS[current].msg);
  }, 500);
}

function stopAutoSteps() {
  clearInterval(autoStepInterval);
  autoStepInterval = null;
}

/* ── Fetch RCA from backend (SSE) ── */
function fetchRCA(caseNumber) {
  const url = 'http://127.0.0.1:3001/generate-rca?caseNumber=' + encodeURIComponent(caseNumber) + '&audience=cic&template=standard';

  return new Promise((resolve, reject) => {
    let settled = false;
    const done  = (fn, v) => { if (!settled) { settled = true; fn(v); } };

    const timeout = setTimeout(() =>
      done(reject, new Error('Timed out after 15 minutes.')), 900000);

    const es = new EventSource(url);

    es.onerror = () => {
      if (!settled) {
        es.close(); clearTimeout(timeout);
        done(reject, new Error('Could not connect to backend. Make sure you are logged into Claude Code and the server is running.'));
      }
    };

    es.addEventListener('status', e => {
      try { const d = JSON.parse(e.data); setStatus(d.msg); } catch (_) {}
    });

    es.addEventListener('console', e => {
      try { const d = JSON.parse(e.data); consoleLog(d.line, d.kind || 'info'); } catch (_) {}
    });

    es.addEventListener('done', e => {
      es.close(); clearTimeout(timeout);
      try { done(resolve, JSON.parse(e.data).html); }
      catch (_) { done(reject, new Error('Bad response from backend')); }
    });

    es.addEventListener('error', e => {
      es.close(); clearTimeout(timeout);
      try { done(reject, new Error(JSON.parse(e.data).message)); }
      catch (_) { done(reject, new Error('Unknown error from backend')); }
    });
  });
}

/* ── Open RCA in a full Chrome tab ── */
function openPreviewTab(htmlContent, caseNumber) {
  chrome.storage.local.set({ rcaPreviewHtml: htmlContent, rcaPreviewCase: caseNumber }, () => {
    chrome.tabs.create({ url: chrome.runtime.getURL('preview.html') });
  });
}

/* ── Console Panel ── */
document.getElementById('consoleToggle').addEventListener('click', () => {
  const panel  = document.getElementById('consolePanel');
  const toggle = document.getElementById('consoleToggle');
  const hidden = panel.classList.toggle('hidden');
  toggle.textContent = hidden ? '▶ Show console' : '▼ Hide console';
});

function consoleLog(line, kind) {
  const lines = document.getElementById('consoleLines');
  if (!lines) return;
  const el = document.createElement('span');
  el.className  = 'console-line ' + (kind || 'info');
  el.textContent = line;
  lines.appendChild(el);
  lines.appendChild(document.createTextNode('\n'));
  lines.scrollTop = lines.scrollHeight;
}

function resetConsole() {
  const lines = document.getElementById('consoleLines');
  if (lines) lines.textContent = '';
  document.getElementById('consolePanel').classList.remove('hidden');
  document.getElementById('consoleToggle').textContent = '▼ Hide console';
}

/* ── Step Indicators ── */
function resetSteps() {
  ['slack','orgcs','org62','gus','public','generate'].forEach(id => {
    const el = document.getElementById('step-' + id);
    if (el) el.classList.remove('active','done');
  });
}
function setStep(id, state) {
  const el = document.getElementById('step-' + id);
  if (!el) return;
  el.classList.remove('active','done');
  if (state) el.classList.add(state);
}
function setStatus(msg) {
  const el = document.getElementById('loadingStatus');
  if (el) el.textContent = msg;
}

function showLoadingError(msg) {
  document.getElementById('loadingSpinner').style.display = 'none';
  document.getElementById('loadingTitle').textContent     = 'Something went wrong';
  document.getElementById('loadingStatus').textContent    = '';
  document.getElementById('loadingErrorMsg').textContent  = msg;
  document.getElementById('loadingError').classList.remove('hidden');
}
function hideLoadingError() {
  document.getElementById('loadingSpinner').style.display = '';
  document.getElementById('loadingTitle').textContent     = 'Generating RCA';
  document.getElementById('loadingError').classList.add('hidden');
}

/* ── RCA page builder (trusted internal HTML from our backend) ── */
function buildPreviewPage(rcaBodyHtml, isDemo) {
  const caseNum = document.getElementById('caseNumber') ? document.getElementById('caseNumber').value : '';
  const demoBanner = isDemo
    ? '<div class="demo-banner">⚠ <strong>Demo Mode</strong> — Sample data only. Use Generate RCA for real data.</div>'
    : '';
  const aiLabel   = isDemo ? '⚠ Demo Sample' : '✓ AI Generated';
  const badgeClass = isDemo ? 'badge-orange' : 'badge-green';

  return [
    '<!DOCTYPE html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="UTF-8"/>',
    '<title>RCA — Case ' + escHtml(caseNum) + '</title>',
    previewStyles(),
    '</head>',
    '<body>',
    tzSidebarHTML(),
    '<div class="page">',
    toolbarHTML(),
    '<div class="meta-row">',
    '<span class="badge ' + badgeClass + '">' + aiLabel + '</span>',
    '<span class="badge badge-blue">Critical Incident Center</span>',
    '</div>',
    demoBanner,
    '<div class="rca-body"><div id="rcaBody">' + rcaBodyHtml + '</div></div>',
    '</div>',
    '</body>',
    '</html>',
  ].join('\n');
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function previewStyles() {
  return '<style>' + [
    ':root{--brand:#0176D3;--brand-dark:#014486;--brand-light:#D8EDFF;--success:#2E844A;--warning:#DD7A01;--border:#DDDBDA;--n2:#F3F3F3;--n5:#706E6B;--n6:#3E3E3C;--n7:#181818;}',
    '*{box-sizing:border-box;margin:0;padding:0;}',
    'body{font-family:-apple-system,"Salesforce Sans",Arial,sans-serif;font-size:13px;color:var(--n7);background:var(--n2);display:flex;flex-direction:row;}',
    '.tz-sidebar{width:0;overflow:hidden;flex-shrink:0;background:#fff;border-right:1px solid var(--border);transition:width 0.25s cubic-bezier(0.4,0,0.2,1);position:sticky;top:0;height:100vh;display:flex;flex-direction:column;}',
    '.tz-sidebar.open{width:160px;}',
    '.tz-sidebar-inner{width:160px;padding:14px 12px;display:flex;flex-direction:column;gap:6px;overflow:hidden;}',
    '.tz-sidebar-title{font-size:10px;font-weight:700;color:var(--n5);text-transform:uppercase;letter-spacing:0.6px;margin-bottom:4px;white-space:nowrap;}',
    '.tz-btn{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;cursor:pointer;border:1.5px solid var(--border);background:#fff;font-size:12px;font-weight:600;color:var(--n6);font-family:inherit;transition:all 0.12s;white-space:nowrap;width:100%;text-align:left;}',
    '.tz-btn:hover{background:var(--brand-light);border-color:var(--brand);color:var(--brand);}',
    '.tz-btn.active{background:var(--brand);border-color:var(--brand);color:#fff;}',
    '.tz-btn .tz-dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex-shrink:0;opacity:0.7;}',
    '.tz-divider{border:none;border-top:1px solid var(--border);margin:4px 0;}',
    '.page{flex:1;max-width:860px;margin:0 auto;background:#fff;min-height:100vh;box-shadow:0 0 40px rgba(0,0,0,0.08);}',
    '.toolbar{position:sticky;top:0;z-index:99;background:#fff;border-bottom:2px solid var(--brand-light);padding:10px 28px;display:flex;align-items:center;gap:10px;box-shadow:0 2px 8px rgba(1,118,211,0.08);}',
    '.toolbar-brand{display:flex;align-items:center;gap:8px;margin-right:auto;}',
    '.toolbar-brand-icon{width:28px;height:28px;border-radius:6px;background:linear-gradient(135deg,var(--brand-dark),var(--brand));display:flex;align-items:center;justify-content:center;}',
    '.toolbar-brand-text{font-size:13px;font-weight:700;color:var(--brand-dark);}',
    '.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none;font-family:inherit;transition:all 0.15s;}',
    '.btn-pdf{background:linear-gradient(135deg,var(--brand-dark),var(--brand));color:#fff;box-shadow:0 2px 6px rgba(1,118,211,0.35);}',
    '.btn-pdf:hover{box-shadow:0 4px 12px rgba(1,118,211,0.45);transform:translateY(-1px);}',
    '.btn-edit{background:#fff;color:var(--n6);border:1.5px solid var(--border);}',
    '.btn-edit:hover{background:var(--n2);border-color:var(--brand);color:var(--brand);}',
    '.btn-gdoc{background:#fff;color:#188038;border:1.5px solid #B7DDB4;}',
    '.btn-gdoc:hover{background:#E8F5E9;border-color:#188038;}',
    '.btn-gdoc:disabled{opacity:0.6;cursor:not-allowed;}',
    '.btn-tz{background:#fff;color:var(--n6);border:1.5px solid var(--border);}',
    '.btn-tz:hover,.btn-tz.active{background:var(--brand-light);border-color:var(--brand);color:var(--brand);}',
    '.source-toggle{display:inline-flex;align-items:center;border:1.5px solid var(--border);border-radius:6px;overflow:hidden;margin-left:4px;}',
    '.source-toggle label{display:inline-flex;align-items:center;gap:5px;padding:6px 11px;font-size:11px;font-weight:600;cursor:pointer;color:var(--n6);background:#fff;transition:all 0.12s;white-space:nowrap;}',
    '.source-toggle label:first-child{border-right:1px solid var(--border);}',
    '.source-toggle input{display:none;}',
    '.source-toggle label:has(input:checked){background:var(--brand-light);color:var(--brand);}',
    '.source-toggle-dot{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:0.6;}',
    '.meta-row{display:flex;gap:7px;padding:14px 28px 0;flex-wrap:wrap;}',
    '.badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;}',
    '.badge-green{background:#EEF6EE;color:var(--success);border:1px solid #A8D5B5;}',
    '.badge-orange{background:#FEF6E8;color:var(--warning);border:1px solid #F5C97A;}',
    '.badge-blue{background:var(--brand-light);color:var(--brand);border:1px solid #9DC8F0;}',
    '.demo-banner{margin:12px 28px 0;background:#FEF6E8;border:1px solid #F5C97A;padding:10px 14px;border-radius:8px;font-size:12px;color:#7A4F00;}',
    '.rca-body{padding:20px 28px 40px;}',
    '.edit-mode{background:#FFFDF0!important;outline:2px dashed var(--warning);outline-offset:6px;border-radius:4px;}',
    'h1{font-size:22px;font-weight:800;color:var(--brand-dark);padding-bottom:10px;border-bottom:3px solid var(--brand);margin-bottom:18px;letter-spacing:-0.3px;}',
    'h2{font-size:13px;font-weight:700;color:var(--brand);margin:22px 0 8px;padding:7px 12px;background:var(--brand-light);border-left:3px solid var(--brand);border-radius:0 6px 6px 0;}',
    'h3{font-size:13px;font-weight:700;color:var(--n6);margin:14px 0 5px;}',
    'p{margin-bottom:9px;line-height:1.65;}ul{padding-left:20px;margin-bottom:9px;}li{margin-bottom:4px;line-height:1.55;}',
    'table{width:100%;border-collapse:collapse;margin:12px 0;font-size:12px;border-radius:6px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.06);}',
    'th{background:linear-gradient(135deg,var(--brand-dark),var(--brand));color:#fff;font-weight:600;padding:8px 12px;text-align:left;}',
    'td{padding:7px 12px;border-bottom:1px solid var(--border);}tr:nth-child(even) td{background:var(--n2);}',
    '.source-badge{display:inline-flex;align-items:center;font-size:10px;background:var(--n2);color:var(--n5);border:1px solid var(--border);border-radius:20px;padding:1px 7px;margin-left:4px;vertical-align:middle;font-weight:600;}',
    '.source-link{display:inline-flex;align-items:center;gap:3px;font-size:11px;font-weight:600;color:var(--brand);text-decoration:none;background:var(--brand-light);border:1px solid #9DC8F0;border-radius:4px;padding:1px 7px;margin-left:4px;vertical-align:middle;}',
    '.source-link:hover{background:#BFD9F5;text-decoration:underline;}',
    'code{background:var(--n2);padding:2px 6px;border-radius:4px;font-size:11px;font-family:monospace;color:#C41E3A;}',
    '.tz-ts{font-weight:600;color:var(--n7);}',
    '@media print{.toolbar,.tz-sidebar{display:none!important;}body{display:block;background:#fff;}.page{box-shadow:none;}}',
    '.no-source .source-badge,.no-source .source-link{display:none!important;}',
  ].join('') + '</style>';
}

function tzSidebarHTML() {
  return [
    '<div class="tz-sidebar" id="tzSidebar"><div class="tz-sidebar-inner">',
    '<div class="tz-sidebar-title">Timezone</div>',
    '<button class="tz-btn active" data-tz="America/Los_Angeles"><span class="tz-dot"></span>PST / PDT</button>',
    '<button class="tz-btn" data-tz="America/New_York"><span class="tz-dot"></span>EST / EDT</button>',
    '<button class="tz-btn" data-tz="America/Chicago"><span class="tz-dot"></span>CST / CDT</button>',
    '<button class="tz-btn" data-tz="America/Denver"><span class="tz-dot"></span>MST / MDT</button>',
    '<button class="tz-btn" data-tz="Asia/Kolkata"><span class="tz-dot"></span>IST</button>',
    '<button class="tz-btn" data-tz="America/Sao_Paulo"><span class="tz-dot"></span>BRT</button>',
    '<hr class="tz-divider"/>',
    '<button class="tz-btn" data-tz="UTC"><span class="tz-dot"></span>UTC</button>',
    '</div></div>',
  ].join('');
}

function toolbarHTML() {
  return [
    '<div class="toolbar">',
    '<div class="toolbar-brand">',
    '<div class="toolbar-brand-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg></div>',
    '<span class="toolbar-brand-text">RCA Analysis</span>',
    '</div>',
    '<button class="btn btn-tz" id="btnTzToggle"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span id="tzLabel">PST</span></button>',
    '<button class="btn btn-pdf"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Download PDF</button>',
    '<button class="btn btn-gdoc" id="btnGdoc"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>Google Doc</button>',
    '<button class="btn btn-edit"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>Edit</button>',
    '<div class="source-toggle"><label><input type="radio" name="srcMode" class="btn-src-on" checked/><span><span class="source-toggle-dot"></span> With Source</span></label><label><input type="radio" name="srcMode" class="btn-src-off"/><span>Without Source</span></label></div>',
    '</div>',
  ].join('');
}

/* ── Demo simulate steps ── */
function simulateSteps() {
  return new Promise(resolve => {
    const steps   = ['slack','orgcs','org62','gus','public','generate'];
    const msgs = [
      'Searching Slack SEV channels…',
      'Loading OrgCS case data…',
      'Loading Org62 records…',
      'Searching GUS work items…',
      'Searching Knowledge Articles & Known Issues…',
      'Generating & validating RCA…',
    ];
    let i = 0;
    function next() {
      if (i > 0) setStep(steps[i-1], 'done');
      if (i < steps.length) {
        setStep(steps[i], 'active');
        setStatus(msgs[i]);
        i++;
        setTimeout(next, 600);
      } else {
        setStep(steps[steps.length-1], 'done');
        setTimeout(resolve, 300);
      }
    }
    next();
  });
}

/* ── Demo RCA content ── */
function getDemoRCA(caseNumber) {
  var cn = escHtml(caseNumber);
  return [
    '<h1>Root Cause Analysis — Case #' + cn + '</h1>',
    '<table><tr><th>Field</th><th>Value</th></tr>',
    '<tr><td>Account Name</td><td>GM Holdings LLC <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Case # (Sev-1)</td><td>SEV1-' + cn + ' <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>SEV Level</td><td>SEV1 <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Production Org ID</td><td>00D5g000004DEMO <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Production Instance</td><td>NA147 <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Time Format</td><td>PST <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Case Opened (PST)</td><td><span class="tz-ts" data-utc="2026-07-07T22:22:00Z">2026-07-07 14:22 PST</span> <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Sev-1 Initiated (PST)</td><td><span class="tz-ts" data-utc="2026-07-07T22:35:00Z">2026-07-07 14:35 PST</span> <span class="source-badge">Slack</span></td></tr>',
    '<tr><td>Sev-1 Mitigated (PST)</td><td><span class="tz-ts" data-utc="2026-07-08T01:48:00Z">2026-07-07 17:48 PST</span> <span class="source-badge">Slack</span></td></tr>',
    '<tr><td>Success Plan</td><td>Signature Success <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Red Account</td><td>Yes <span class="source-badge">OrgCS</span></td></tr>',
    '<tr><td>Slack Channel</td><td>#sev1-na147</td></tr>',
    '</table>',
    '<h2>Executive Summary</h2>',
    '<p>On 2026-07-07 at 14:22 PST, GM Holdings LLC reported complete login failure on NA147. All ~1,200 users received "Authentication Failed". <span class="source-badge">OrgCS</span></p>',
    '<p>Root cause: expired SSO certificate failed to auto-renew due to AWS ACM rate limit. Engineering rotated at 17:31 PST; access restored by 17:48 PST — <strong>3h 26m total impact</strong>. <span class="source-badge">Slack: #sev1-na147</span></p>',
    '<h2>Business Impact</h2><ul>',
    '<li>~1,200 users unable to log in for 3h 26m <span class="source-badge">OrgCS</span></li>',
    '<li>3 advisor groups affected <span class="source-badge">Slack</span></li>',
    '<li>Signature Success SLA breach at T+60 min <span class="source-badge">Org62</span></li>',
    '</ul>',
    '<h2>Technical Details</h2>',
    '<p><code>INVALID_LOGIN: SSO Certificate validation failed. Certificate CN=sf-sso-prod expired 2026-07-06T23:59:59Z</code></p>',
    '<table><tr><th>Time</th><th>Action</th><th>Owner</th></tr>',
    '<tr><td><span class="tz-ts" data-utc="2026-07-07T22:22:00Z">14:22 PST</span></td><td>Alert fired, on-call paged</td><td>SRE</td></tr>',
    '<tr><td><span class="tz-ts" data-utc="2026-07-07T22:35:00Z">14:35 PST</span></td><td>SEV1 declared</td><td>IC</td></tr>',
    '<tr><td><span class="tz-ts" data-utc="2026-07-07T23:10:00Z">15:10 PST</span></td><td>Root cause identified</td><td>Auth Team</td></tr>',
    '<tr><td><span class="tz-ts" data-utc="2026-07-08T01:31:00Z">17:31 PST</span></td><td>Certificate rotated</td><td>Auth Team</td></tr>',
    '<tr><td><span class="tz-ts" data-utc="2026-07-08T01:48:00Z">17:48 PST</span></td><td>Login restored — SEV1 closed</td><td>IC</td></tr>',
    '</table>',
    '<h2>Root Cause Analysis</h2>',
    '<p>SSO certificate expired 2026-07-06 23:59 UTC. CertRenewalJob failed silently after hitting AWS ACM rate limit. <span class="source-badge">Slack</span></p>',
    '<p>GUS: W-12345678 — Add alerting for CertRenewalJob failures, increase expiry warning to 30 days <span class="source-badge">GUS</span></p>',
    '<h2>Support Opportunities</h2><ul>',
    '<li>No runbook for manual cert rotation — added 90 min to resolution <span class="source-badge">Slack</span></li>',
    '<li>Customer not notified until T+45 min <span class="source-badge">OrgCS</span></li>',
    '</ul>',
    '<h2>Customer Opportunities</h2><ul>',
    '<li>Executive briefing with CTO within 5 business days <span class="source-badge">Org62</span></li>',
    '<li>Written RCA within 48h per Signature Success SLA <span class="source-badge">Org62</span></li>',
    '</ul>',
    '<h2>Engineering Actions</h2>',
    '<table><tr><th>Action</th><th>Owner</th><th>Due</th></tr>',
    '<tr><td>Alert for CertRenewalJob failures</td><td>Auth Platform</td><td>2026-07-14</td></tr>',
    '<tr><td>Increase cert expiry warning to 30 days</td><td>Auth Platform</td><td>2026-07-14</td></tr>',
    '<tr><td>Write cert rotation runbook</td><td>SRE</td><td>2026-07-14</td></tr>',
    '</table>',
  ].join('');
}
