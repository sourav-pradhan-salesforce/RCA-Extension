#!/usr/bin/env python3
"""
RCA Backend Server — auto-starts via LaunchAgent, no terminal needed.
Uses claude CLI with --dangerously-skip-permissions so MCP tools work.
Pulls MCP OAuth tokens from keychain so background process can access Slack/GUS.
"""
import json, os, subprocess, sys, logging, threading, time
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, TCPServer
from urllib.parse import urlparse, parse_qs

import platform as _platform
import shutil as _shutil

HOME        = os.path.expanduser('~')
PORT        = 3001
IS_WINDOWS  = _platform.system() == 'Windows'

# Cross-platform data directory
if IS_WINDOWS:
    _appdata    = os.environ.get('APPDATA', os.path.join(HOME, 'AppData', 'Roaming'))
    SUPPORT_DIR = os.path.join(_appdata, 'rca-backend')
else:
    SUPPORT_DIR = os.path.join(HOME, 'Library', 'Application Support', 'rca-backend')

LOG_FILE = os.path.join(SUPPORT_DIR, 'server.log')
os.makedirs(SUPPORT_DIR, exist_ok=True)

def find_claude_bin():
    """Auto-detect claude CLI — cross-platform."""
    # shutil.which respects PATH on all platforms
    found = _shutil.which('claude') or _shutil.which('claude.cmd')
    if found:
        return found

    if IS_WINDOWS:
        appdata  = os.environ.get('APPDATA', '')
        localapp = os.environ.get('LOCALAPPDATA', '')
        candidates = [
            os.path.join(localapp, 'npm', 'claude.cmd'),
            os.path.join(appdata,  'npm', 'claude.cmd'),
            os.path.join(localapp, 'npm', 'claude'),
            os.path.join(appdata,  'npm', 'claude'),
        ]
    else:
        candidates = [
            os.path.join(HOME, '.local', 'bin', 'claude'),
            '/usr/local/bin/claude',
            '/opt/homebrew/bin/claude',
            '/usr/bin/claude',
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

CLAUDE_BIN  = find_claude_bin()
# Use support dir as working dir so server works regardless of where this file lives
PROJECT_DIR = SUPPORT_DIR

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)


# In-memory template store: {template_id: {name, text}}
TEMPLATES = {}


class ThreadedHTTPServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _cred_cache_path():
    return os.path.join(SUPPORT_DIR, 'credentials_cache.json')


def read_keychain_credentials():
    """Read MCP OAuth credentials — macOS Keychain or Windows file cache."""
    if IS_WINDOWS:
        # On Windows, Claude Code stores credentials in APPDATA\Claude\credentials.json
        # Try that first, then fall back to our own cache file.
        _appdata = os.environ.get('APPDATA', os.path.join(HOME, 'AppData', 'Roaming'))
        candidates = [
            os.path.join(_appdata, 'Claude', 'credentials.json'),
            os.path.join(HOME, '.claude', 'credentials.json'),
            _cred_cache_path(),
        ]
        for path in candidates:
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                continue
        return {}
    else:
        try:
            result = subprocess.run(
                ['security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return {}
            return json.loads(result.stdout.strip())
        except Exception as e:
            logging.warning(f'keychain read error: {e}')
            return {}


def write_keychain_credentials(data):
    """Write updated credentials — macOS Keychain or Windows file cache."""
    if IS_WINDOWS:
        try:
            with open(_cred_cache_path(), 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logging.warning(f'credentials write error: {e}')
    else:
        try:
            payload = json.dumps(data)
            subprocess.run(
                ['security', 'add-generic-password', '-U',
                 '-s', 'Claude Code-credentials',
                 '-a', os.environ.get('USER', os.environ.get('USERNAME', 'user')),
                 '-w', payload],
                capture_output=True, timeout=5
            )
        except Exception as e:
            logging.warning(f'keychain write error: {e}')


def refresh_slack_token(token_data, client_id):
    """Use refresh token to get a new Slack access token."""
    refresh_token = token_data.get('refreshToken', '')
    if not refresh_token:
        return None
    try:
        body = urllib.parse.urlencode({
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
            'client_id':     client_id,
        }).encode()
        req = urllib.request.Request(
            'https://slack.com/api/oauth.v2.access',
            data=body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get('ok'):
            new_token = result.get('access_token') or result.get('authed_user', {}).get('access_token')
            new_refresh = result.get('refresh_token', refresh_token)
            expires_in = result.get('expires_in', 43200)
            logging.info('Slack token refreshed successfully')
            return {
                'accessToken':  new_token,
                'refreshToken': new_refresh,
                'expiresAt':    int(time.time() * 1000) + expires_in * 1000,
            }
        else:
            logging.warning(f'Slack token refresh failed: {result.get("error")}')
            return None
    except Exception as e:
        logging.warning(f'Slack token refresh error: {e}')
        return None


def get_mcp_oauth_tokens():
    """Read MCP OAuth tokens from keychain, auto-refreshing expired ones."""
    creds = read_keychain_credentials()
    if not creds:
        return {}

    # Get Slack client_id from global or any project config
    slack_client_id = ''
    try:
        with open(os.path.join(HOME, '.claude.json')) as f:
            d = json.load(f)
        # Check global first, then all projects
        all_mcp = dict(d.get('mcpServers', {}))
        for proj_cfg in d.get('projects', {}).values():
            all_mcp.update(proj_cfg.get('mcpServers', {}))
        slack_client_id = all_mcp.get('slack', {}).get('oauth', {}).get('clientId', '')
    except Exception:
        pass

    tokens = {}
    updated = False
    now_ms = time.time() * 1000

    for key, val in creds.get('mcpOAuth', {}).items():
        name = key.split('|')[0]
        if not isinstance(val, dict):
            continue
        access_token = val.get('accessToken', '')
        expires_at   = val.get('expiresAt')
        is_expired   = expires_at and expires_at < now_ms

        if is_expired and name == 'slack' and slack_client_id:
            logging.info(f'Slack token expired, attempting refresh...')
            refreshed = refresh_slack_token(val, slack_client_id)
            if refreshed:
                val.update(refreshed)
                creds['mcpOAuth'][key] = val
                updated = True
                access_token = refreshed['accessToken']

        if access_token:
            tokens[name] = access_token

    if updated:
        write_keychain_credentials(creds)

    return tokens


def get_claude_env():
    env = os.environ.copy()
    env['HOME'] = HOME
    try:
        with open(os.path.join(HOME, '.claude/settings.json')) as f:
            for k, v in json.load(f).get('env', {}).items():
                env[k] = str(v)
    except Exception as e:
        logging.warning(f'settings.json read error: {e}')

    # Inject MCP OAuth tokens so background claude --print can auth
    tokens = get_mcp_oauth_tokens()
    if tokens:
        logging.info(f'Injecting MCP tokens for: {list(tokens.keys())}')
        env['MCP_OAUTH_TOKENS'] = json.dumps(tokens)
        # Claude CLI checks these env vars for pre-authorised MCP tokens
        for name, token in tokens.items():
            env_key = f'MCP_TOKEN_{name.upper().replace("-", "_")}'
            env[env_key] = token
    return env


def build_mcp_config(tokens):
    """Build mcp_config.json from .claude.json only (no plugins — plugins load automatically).
    Including plugin servers here creates duplicate server conflicts that break tool calls."""
    cfg_path = os.path.join(SUPPORT_DIR, 'mcp_config.json')
    base_cfg = {}
    try:
        with open(os.path.join(HOME, '.claude.json')) as f:
            d = json.load(f)
        base_cfg.update(d.get('mcpServers', {}))
        for proj_cfg in d.get('projects', {}).values():
            base_cfg.update(proj_cfg.get('mcpServers', {}))
    except Exception as e:
        logging.warning(f'claude.json read error: {e}')

    servers = {}
    for name, cfg in base_cfg.items():
        # Only include stdio servers in mcp_config — HTTP/OAuth servers (orgcs, Org62-Sobject-Read)
        # are handled by plugins which auto-refresh tokens. Including them here causes stale-token auth failures.
        if cfg.get('type') == 'http':
            continue
        servers[name] = cfg

    with open(cfg_path, 'w') as f:
        json.dump({'mcpServers': servers}, f)
    logging.info(f'MCP config servers: {list(servers.keys())}')
    return cfg_path


def build_prompt(case_number, audience, template, template_text=None):
    is_cic = (audience == 'cic')

    sections_rule = {
        'leadership': 'Output sections 1,2,3,5,8 ONLY. Executive language, no stack traces.',
        'customer':   'Output sections 1,2,3,7 ONLY. Plain language, no internal system names.',
        'cic':        'Output all 8 sections.',
    }.get(audience, 'Output all 8 sections.')

    gus_note = """D. GUS — MANDATORY. Search ALL four sources for W-numbers, then query GUS for each one found.

   D1 — OrgCS CaseBug__c junction (may not exist — skip on error):
        SELECT ADM_Work__c, ADM_Work__r.Name
        FROM CaseBug__c WHERE Case__c='<CaseId>' LIMIT 10

   D2 — Scan CaseComment bodies (from step A3) for:
        - W-\\d+ patterns (e.g. W-22029017)
        - GUS URLs: gus.lightning.force.com/lightning/r/ADM_Work__c/<Id>/view
        Extract both W-numbers AND Salesforce IDs (18-char) from those URLs.

   D3 — Scan Slack messages already retrieved in step C for W-\\d+ patterns.
        Look in channel messages, thread replies, and swarm posts.

   D4 — Search Slack explicitly for W-numbers linked to this case:
        Search query: "{case_number} W-" OR "W-2" to find work item references.

   Once you have W-numbers or GUS record IDs (from ANY of D1–D4):
   For EACH, call mcp__plugin_dxmcp-gus_dxmcp-gus__query_gus_records:
        If you have the W-number:  WHERE Name='<W-XXXXXXXX>'
        If you only have the Id:   WHERE Id='<18-char-Id>'
        Query:
        SELECT Id, Name, Subject__c, Status__c, Priority__c, Type__c,
               Assignee__r.Name, Product_Tag__r.Name, Scheduled_Build__c
        FROM ADM_Work__c WHERE ... LIMIT 1
        (Do NOT include Root_Cause__c or Fix_Summary__c — those fields do not exist)

   Include in RCA:
   - GUS work item number as link: <a href="https://gus.lightning.force.com/lightning/r/ADM_Work__c/<Id>/view" target="_blank" class="source-link">W-XXXXXXX ↗</a>
   - Subject, Status, Priority, Assignee, Scheduled_Build__c

   If GUS MCP errors on every attempt, write "GUS: Not available" and continue.
   If no W-numbers found after all 4 searches, write "GUS: No work items linked" and continue.""".format(case_number=case_number)

    # OrgCS Case queries — split into guaranteed core + optional custom fields
    # Core fields are standard and always present. Custom fields tried separately.
    orgcs_core = f"""A1. OrgCS core case (always works — do NOT add extra fields here):
   SELECT Id,CaseNumber,Subject,Description,Status,Priority,
          Account.Name,Account.Id,CreatedDate,ClosedDate,Origin,Type,Owner.Name
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1
   Save the 18-char Case Id (e.g. 500Hx00001XXXXX) and Account.Id for later queries.

A2. OrgCS custom case fields — attempt in this exact order. On connection/auth error, retry the SAME call ONCE before moving to next attempt. Only mark a field "Not available" if every attempt for that field fails.

   A2a (try first — all fields):
   SELECT Id,OrgId__c,Case_Origin_OrgID__c,Instance__c,Instance_Type__c,Pod__c,
          Severity_Level__c,Open_Red_Account__c,Case_Support_level__c,
          support_available_timezone__c,AX_Sev1_Start_Time__c,AX_Sev1_End_Time__c
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1

   A2b (if A2a fails — instance/org fields):
   SELECT Id,OrgId__c,Case_Origin_OrgID__c,Instance__c,Instance_Type__c,Pod__c
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1

   A2c (if A2a fails — severity/plan/timezone/sev1 times):
   SELECT Id,Severity_Level__c,support_available_timezone__c,AX_Sev1_Start_Time__c,AX_Sev1_End_Time__c
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1

   A2d (ALWAYS run this as a separate query — Success Plan and Red Account are critical):
   SELECT Id,Open_Red_Account__c,Case_Support_level__c
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1
   If this fails, retry it ONE more time before writing "Not available".

A3. OrgCS comments:
   SELECT Id,CommentBody,CreatedDate,CreatedBy.Name,IsPublished
   FROM CaseComment WHERE ParentId='<CaseId>' ORDER BY CreatedDate ASC LIMIT 30

A4. OrgCS emails:
   SELECT Id,Subject,TextBody,FromAddress,CreatedDate,Incoming,MessageDate
   FROM EmailMessage WHERE ParentId='<CaseId>' ORDER BY MessageDate ASC LIMIT 20"""

    orgcs_non_cic = f"""A1. OrgCS core case (always works — do NOT add extra fields here):
   SELECT Id,CaseNumber,Subject,Description,Status,Priority,
          Account.Name,Account.Id,CreatedDate,ClosedDate,Origin,Type,Owner.Name
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1
   Save the 18-char Case Id and Account.Id.

A2. OrgCS custom case fields (skip on error, use "Not available"):
   SELECT Id,OrgId__c,Case_Origin_OrgID__c,Instance__c,Instance_Type__c,Pod__c,
          Severity_Level__c,Open_Red_Account__c,Case_Support_level__c,
          support_available_timezone__c,AX_Sev1_Start_Time__c,AX_Sev1_End_Time__c
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1

A3. OrgCS comments:
   SELECT Id,CommentBody,CreatedDate,CreatedBy.Name,IsPublished
   FROM CaseComment WHERE ParentId='<CaseId>' ORDER BY CreatedDate ASC LIMIT 20"""

    # Org62 — only standard Account fields that actually exist
    org62_query = """B. Org62 — use Account.Id from above:
   SELECT Id,Name,Industry,Type,BillingCountry
   FROM Account WHERE Id='<AccountId>' LIMIT 1
   (Support_Level__c and Open_Red_Account__c are NOT on Org62 Account — skip them)"""

    if is_cic:
        data_steps = f"""{orgcs_core}

{org62_query}

C. Slack — search "{case_number}", "sev {case_number}", "swarm {case_number}"
   Record EXACT channel ID (e.g. C0BEQTQL5TL) and name. Read channel for:
   first alert time, error messages, actions taken, resolution time.

{gus_note}"""
    else:
        data_steps = f"""{orgcs_non_cic}

{org62_query}

C. Slack — search "{case_number}", "sev {case_number}"
   Record EXACT channel ID and name. Get: first alert time, resolution time, key actions.

{gus_note}"""

    base_prompt = f"""You are a Salesforce Senior Support Engineer. Write a concise, precise Root Cause Analysis.
STRICT LENGTH RULE: The entire RCA must be similar in length to a 1-2 page document. Short bullet points, no padding, no repetition.

TASK: Generate HTML RCA for case {case_number}.

RULES:
- NEVER write to a file. Output HTML to stdout ONLY. Never use Write/Edit/Bash tools.
- Do NOT repeat a tool call that already returned data.
- If a source has no data, write "Not available" inline and continue.
- Output the full HTML immediately after collecting data — do not summarise first.

PHASE 1 — COLLECT DATA
{data_steps}

PHASE 2 — OUTPUT HTML

{sections_rule}

TIMEZONE RULE — ALL timestamps must use this format:
  <span class="tz-ts" data-utc="<ISO-8601-UTC>"><UTC display></span>
  Example: <span class="tz-ts" data-utc="2026-07-07T14:22:00Z">2026-07-07 14:22 UTC</span>
  Use this for EVERY time value in the header table and timeline. The UI converts them to any timezone.

STYLE — match this compact format exactly:
- Header: one clean 2-column table (Field | Value)
- Each section: 2-4 short bullet points OR 1 short paragraph max
- Timeline: compact table (Time | Action) — only key events, not every message
- Root cause: ONE precise sentence stating what failed, why, at what time
- Engineering actions: bullet list with owner inline, e.g. "Fix X — Owner: Team Y"
- NO verbose paragraphs. NO padding sentences like "This section describes..."
- Total output should be roughly 600-900 words of visible content

LINKS — always use real <a> tags:
- OrgCS: <a href="https://orgcs.lightning.force.com/lightning/r/Case/<CaseId>/view" target="_blank" class="source-link">View in OrgCS ↗</a>
- Slack: <a href="https://salesforce.enterprise.slack.com/archives/<channelId>" target="_blank" class="source-link">#channel-name ↗</a>
- GUS: <a href="https://gus.lightning.force.com/lightning/r/ADM_Work__c/<Id>/view" target="_blank" class="source-link">W-XXXXXXX ↗</a>

Source badges after key facts: <span class="source-badge">OrgCS</span> <span class="source-badge">Slack: #name</span> <span class="source-badge">Org62</span> <span class="source-badge">GUS</span>

HEADING RULE — CRITICAL:
- The document has exactly ONE <h1>: the title at the very top.
- ALL section headings use <h2> tags. NEVER use <h1> for a section heading.
- Section 5 heading must be <h2>5. Root Cause Analysis</h2> — same name, but h2 only.

TIMEZONE EXTRACTION (for data-default-tz attribute only — no Time Format row needed):
- From support_available_timezone__c extract the IANA identifier (e.g. "America/Panama" from "(GMT-05:00) Eastern Standard Time (America/Panama)").
- Use it ONLY for the data-default-tz attribute on <h1>. Fallback: "America/Los_Angeles".
- Do NOT add a Time Format row to the header table.

STATUS COLOR RULE:
- If Case Status is "Working" or "Open" or "In Progress", wrap it in <span style="color:var(--warning);font-weight:700;">Working</span>
- If "Closed", wrap in <span style="color:var(--success);font-weight:700;">Closed</span>

SECTIONS (keep each one SHORT):
1. Header table — exact fields in this order:
   Account Name (Account.Name) |
   Case # (CaseNumber as OrgCS link, bold SEV-1 label) |
   Case # Sev-2 (if applicable, else omit row) |
   SEV Level (Severity_Level__c) |
   Production Org ID (Case_Origin_OrgID__c) |
   Production Instance (Instance_Type__c) |
   Case Opened (CreatedDate as tz-ts span) |
   Sev-1 Initiated (AX_Sev1_Start_Time__c as tz-ts span) |
   Sev-1 Mitigated (AX_Sev1_End_Time__c as tz-ts span — if null write "Open — not yet mitigated" in orange) |
   Success Plan (Case_Support_level__c) |
   Red Account (Open_Red_Account__c) |
   Slack Channel (channel name as link) |
   GUS Investigation (W-number as GUS link + " — Status: <status>" + GUS badge; if none write "None linked") |
   Case Owner (Owner.Name from A1 + Sev-1 assignee name if available from Slack, format "Primary / Sev-1 Name") |
   Case Status (Status from A1, color-coded per STATUS COLOR RULE above)
   NOTE: All time cells use <span class="tz-ts" data-utc="..."> tags.
   NOTE: If OrgCS custom fields (A2) fail, still include the row with "Not available" — do NOT skip the row.
2. Executive Summary — 2-3 short paragraphs: what failed, when, impact, how resolved
3. Business Impact — 3-5 bullet points: user count, groups affected, features down, SLA breach
4. Technical Details — Detection paragraph (2-3 sentences) + Remediation Timeline table (Time | Action) with 5-8 most important events (all times as tz-ts spans)
5. Root Cause Analysis — Primary root cause (1 precise sentence, bold "Primary Root Cause:" label) + contributing factors as bold-labeled bullets ("Contributing factor N:") + GUS investigation link with title and status
6. Support Opportunities — 2-3 bullets with bold lead phrase per bullet
7. Customer Opportunities — 2-3 bullets with bold lead phrase per bullet
8. Engineering Actions — bullet list: each action with "— Owner: Team" inline

Start output with <h1 data-default-tz="<extracted-IANA-or-America/Los_Angeles>">Root Cause Analysis — Case #{case_number}</h1> immediately."""

    if template_text:
        base_prompt += f"""

══════════════════════════════════════════════════════
TEMPLATE MODE — OVERRIDE DEFAULT FORMAT
══════════════════════════════════════════════════════
A custom output template has been provided. You MUST fill this template with the data you collected above.

TEMPLATE RULES:
- Follow the template's EXACT structure, section order, and headings.
- Replace every placeholder / "[Under Investigation]" / blank field with real data from the case.
- If a template field has no matching data, write "Not available" in that slot.
- Keep ALL template section headers and labels exactly as they appear.
- Preserve the template's tone (executive, customer-facing, etc.).
- Still apply the TIMEZONE RULE: wrap every timestamp in <span class="tz-ts" data-utc="..."> tags.
- Still apply source badges after key facts.
- Output as clean HTML — use <h2> for every section heading from the template, <table> for tabular sections, <ul>/<li> for bullet lists.
- Start output with <h1 data-default-tz="<IANA-tz>">{template_text[:80].split(chr(10))[0].strip()[:60]} — Case #{case_number}</h1>

TEMPLATE CONTENT (fill this exactly):
---
{template_text[:8000]}
---"""

    return base_prompt


def extract_html(text):
    # If Claude wrote a file path, try reading that file
    import re
    file_match = re.search(r'written to [`\'"]?(/[^\s`\'"]+\.html)', text)
    if file_match:
        fpath = file_match.group(1)
        try:
            with open(fpath) as f:
                content = f.read()
            logging.info(f'Read RCA from file Claude wrote: {fpath}')
            for tag in ['<h1', '<h2', '<table', '<!DOCTYPE', '<html']:
                idx = content.find(tag)
                if idx != -1:
                    return content[idx:].strip()
        except Exception as e:
            logging.warning(f'Could not read Claude-written file {fpath}: {e}')

    for tag in ['<h1', '<h2', '<table']:
        idx = text.find(tag)
        if idx != -1:
            return text[idx:].strip()
    return None


class RCAHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logging.info(fmt % args)

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode())
            return

        if parsed.path == '/generate-rca':
            params      = parse_qs(parsed.query)
            case_number = params.get('caseNumber', [''])[0].strip()
            audience    = params.get('audience',   ['cic'])[0]
            template    = params.get('template',   ['standard'])[0]
            template_id = params.get('template_id', [''])[0]

            if not case_number:
                self.send_response(400)
                self.send_cors_headers()
                self.end_headers()
                return

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_cors_headers()
            self.end_headers()

            done_event = threading.Event()

            def sse_write(event, data):
                try:
                    msg = f'event: {event}\ndata: {json.dumps(data)}\n\n'
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except Exception:
                    pass

            def heartbeat_thread():
                while not done_event.wait(10):
                    try:
                        self.wfile.write(b': ping\n\n')
                        self.wfile.flush()
                    except Exception:
                        break

            hb = threading.Thread(target=heartbeat_thread, daemon=True)
            hb.start()

            if not CLAUDE_BIN:
                sse_write('error', {'message': 'Claude CLI not found. Run: npm install -g @anthropic-ai/claude-code — then: claude login'})
                done_event.set()
                return

            sse_write('status', {'step': 'slack', 'msg': 'Searching Slack SEV channels & swarm threads…'})
            logging.info(f'Starting RCA for case {case_number}, audience={audience}')

            env = get_claude_env()

            # Build MCP config with pre-authorised tokens
            tmpl_text = TEMPLATES.get(template_id, {}).get('text') if template_id else None
            if tmpl_text:
                logging.info(f'Using template {template_id} ({len(tmpl_text)} chars)')
            prompt_text = build_prompt(case_number, audience, template, template_text=tmpl_text)

            try:
                tokens = get_mcp_oauth_tokens()
                # Block local filesystem/task tools — keep ToolSearch (needed to load MCP schemas)
                # and WebFetch (needed for web searches). MCP plugin tools are explicitly allowed below.
                blocked = 'Write,Edit,NotebookEdit,Bash,Grep,Task,TaskCreate,TaskUpdate,TaskGet,TaskList,TaskOutput,TaskStop,Workflow,SendMessage,CronCreate,CronList,CronDelete,ScheduleWakeup,EnterWorktree,ExitWorktree,ReportFindings,Skill,ListMcpResourcesTool,ReadMcpResourceDirTool,ReadMcpResourceTool'
                no_local = ['--disallowed-tools', blocked]
                if tokens:
                    cfg_path = build_mcp_config(tokens)
                    cmd = [CLAUDE_BIN, '--print', '--dangerously-skip-permissions',
                           '--output-format', 'stream-json', '--verbose',
                           '--mcp-config', cfg_path] + no_local + ['--', prompt_text]
                    logging.info(f'Using MCP config at {cfg_path} with tokens for: {list(tokens.keys())}')
                else:
                    cmd = [CLAUDE_BIN, '--print', '--dangerously-skip-permissions',
                           '--output-format', 'stream-json', '--verbose'] + no_local + [prompt_text]
                    logging.warning('No MCP tokens found in keychain — Slack/GUS may not connect')
            except Exception as e:
                logging.warning(f'MCP config build error: {e}')
                cmd = [CLAUDE_BIN, '--print', '--dangerously-skip-permissions',
                       '--output-format', 'stream-json', '--verbose'] + no_local + [prompt_text]

            # Hard 8-minute server-side kill — prevents runaway Claude processes
            HARD_TIMEOUT = 480

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=PROJECT_DIR,
                    env=env,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                done_event.set()
                sse_write('error', {'message': f'Claude CLI not found at {CLAUDE_BIN}'})
                return
            except Exception as e:
                done_event.set()
                sse_write('error', {'message': f'Failed to start Claude: {e}'})
                return

            def kill_after_timeout():
                if proc.poll() is None:
                    logging.warning(f'Hard timeout ({HARD_TIMEOUT}s) reached — killing Claude process')
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except Exception: proc.kill()

            kill_timer = threading.Timer(HARD_TIMEOUT, kill_after_timeout)
            kill_timer.daemon = True
            kill_timer.start()

            # Tool-name → step mapping for auto-advancing the step indicators
            TOOL_STEP_MAP = {
                'orgcs':         ('orgcs',  'Loading OrgCS case data…'),
                'Org62':         ('org62',  'Loading Org62 account data…'),
                'Org62-Sobject': ('org62',  'Loading Org62 account data…'),
                'gus':           ('gus',    'Searching GUS work items…'),
                'WebFetch':      ('public', 'Searching Knowledge Articles & Known Issues…'),
                'WebSearch':     ('public', 'Searching Knowledge Articles & Known Issues…'),
            }

            output      = ''
            tools_seen  = set()
            seen_msg_ids = set()  # track unique message IDs to count turns

            def tool_label(name, inp):
                try:
                    nl = name.lower()
                    if 'soqlquery' in nl or 'find' in nl:
                        q = inp.get('query') or inp.get('soql') or ''
                        short = str(q)[:140].replace('\n', ' ').strip()
                        return f'SOQL   {short}' if short else name
                    if 'slack' in nl:
                        q = inp.get('query') or inp.get('channel') or inp.get('channelId') or ''
                        return f'Slack  {name.split("__")[-1]}({q})'
                    if 'webfetch' in nl:
                        return f'Web    {inp.get("url","")[:100]}'
                    if 'websearch' in nl:
                        return f'Search {inp.get("query","")[:100]}'
                    first = next((str(v)[:80] for v in inp.values() if v), '')
                    return f'{name}  {first}' if first else name
                except Exception:
                    return name

            try:
                for raw_line in iter(proc.stdout.readline, ''):
                    raw_line = raw_line.rstrip('\n')
                    if not raw_line:
                        continue

                    try:
                        evt = json.loads(raw_line)
                    except json.JSONDecodeError:
                        output += raw_line + '\n'
                        continue

                    etype = evt.get('type', '')

                    # ── Assistant message: contains tool_use and/or text blocks ──
                    if etype == 'assistant':
                        msg     = evt.get('message', {})
                        msg_id  = msg.get('id', '')
                        content = msg.get('content', [])

                        # New turn = new unique message ID
                        if msg_id and msg_id not in seen_msg_ids:
                            seen_msg_ids.add(msg_id)
                            turn = len(seen_msg_ids)
                            sse_write('console', {'line': f'── Turn {turn} ─────────────────────────', 'kind': 'sep'})

                        for block in content:
                            btype = block.get('type', '')

                            if btype == 'tool_use':
                                tool_name = block.get('name', '')
                                tool_inp  = block.get('input', {})
                                label     = tool_label(tool_name, tool_inp)
                                logging.info(f'Tool call: {tool_name}')
                                sse_write('console', {'line': f'→ {label}', 'kind': 'tool'})
                                # Advance step indicator
                                for prefix, (step_id, smsg) in TOOL_STEP_MAP.items():
                                    if prefix.lower() in tool_name.lower() and step_id not in tools_seen:
                                        tools_seen.add(step_id)
                                        sse_write('status', {'step': step_id, 'msg': smsg})
                                        break

                            elif btype == 'text':
                                chunk = block.get('text', '')
                                output += chunk
                                if 'generate' not in tools_seen and ('<h1' in output or '<h2' in output):
                                    tools_seen.add('generate')
                                    sse_write('status', {'step': 'generate', 'msg': 'Generating & validating RCA…'})
                                    sse_write('console', {'line': '→ Writing HTML output…', 'kind': 'tool'})

                    # ── User message: contains tool results ──────────
                    elif etype == 'user':
                        for block in evt.get('message', {}).get('content', []):
                            if block.get('type') == 'tool_result':
                                raw = block.get('content', '')
                                if isinstance(raw, list):
                                    raw = ' '.join(str(r.get('text','')) for r in raw if isinstance(r,dict))
                                is_err = block.get('is_error', False)
                                preview = str(raw)[:140].replace('\n', ' ')
                                kind = 'error' if is_err else 'result'
                                prefix = '✗' if is_err else '✓'
                                sse_write('console', {'line': f'   {prefix} {preview}', 'kind': kind})

                    # ── Final result summary ──────────────────────────
                    elif etype == 'result':
                        turns = evt.get('usage', {}).get('iterations') or len(seen_msg_ids)
                        cost  = evt.get('total_cost_usd', 0)
                        sse_write('console', {'line': f'── Done  {turns} turns · ${cost:.4f} ─────', 'kind': 'sep'})

                    # ── Top-level error ───────────────────────────────
                    elif etype == 'error':
                        emsg = evt.get('error', {}).get('message') or str(evt)
                        sse_write('console', {'line': f'✗ {emsg}', 'kind': 'error'})

            except Exception as e:
                logging.error(f'Stream read error: {e}')

            proc.wait()
            kill_timer.cancel()
            done_event.set()

            stderr_out = proc.stderr.read() if proc.stderr else ''
            if stderr_out:
                logging.warning(f'stderr: {stderr_out[:300]}')

            if proc.returncode != 0:
                err = stderr_out[:300] or f'Exit code {proc.returncode}'
                logging.error(f'Claude failed: {err}')
                sse_write('error', {'message': f'Claude error: {err}'})
                return

            html = extract_html(output)
            if not html:
                logging.error(f'No HTML. Raw output start: {output[:300]}')
                sse_write('error', {'message': 'No RCA HTML generated. See server.log for details.'})
                return

            logging.info(f'RCA done for {case_number} — {len(html)} chars')
            sse_write('done', {'html': html})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == '/set-template':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8', errors='replace')
            try:
                payload = json.loads(body)
            except Exception:
                self.send_response(400); self.send_cors_headers(); self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid JSON'}).encode())
                return

            file_name = payload.get('file_name', 'template')
            file_data_b64 = payload.get('file_data', '')
            if not file_data_b64:
                self.send_response(400); self.send_cors_headers(); self.end_headers()
                self.wfile.write(json.dumps({'error': 'No file data'}).encode())
                return

            try:
                import base64, hashlib, tempfile, io as _io
                file_bytes = base64.b64decode(file_data_b64)
                template_text = ''

                if file_name.lower().endswith('.pdf'):
                    try:
                        import pdfplumber
                        with pdfplumber.open(_io.BytesIO(file_bytes)) as pdf:
                            pages_text = []
                            for page in pdf.pages:
                                t = page.extract_text()
                                if t:
                                    pages_text.append(t)
                        template_text = '\n\n'.join(pages_text)
                    except ImportError:
                        # Fallback: write to temp file
                        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
                            tf.write(file_bytes)
                            tf_path = tf.name
                        import pdfplumber
                        with pdfplumber.open(tf_path) as pdf:
                            template_text = '\n\n'.join(p.extract_text() or '' for p in pdf.pages)
                        os.unlink(tf_path)
                else:
                    template_text = file_bytes.decode('utf-8', errors='replace')

                template_text = template_text.strip()
                if not template_text:
                    raise Exception('Could not extract text from file')

                template_id = hashlib.md5(file_bytes).hexdigest()[:12]
                TEMPLATES[template_id] = {'name': file_name, 'text': template_text}
                logging.info(f'Template stored: {template_id} ({file_name}, {len(template_text)} chars)')

                preview = template_text[:300]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'template_id': template_id, 'name': file_name, 'preview': preview, 'chars': len(template_text)}).encode())
            except Exception as e:
                logging.error(f'Template processing error: {e}')
                self.send_response(500); self.send_cors_headers(); self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        if parsed.path == '/create-gdoc':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8', errors='replace')
            try:
                payload = json.loads(body)
            except Exception:
                self.send_response(400)
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid JSON'}).encode())
                return

            html_content = payload.get('html', '')
            case_number  = payload.get('case_number', 'Unknown')

            if not html_content:
                self.send_response(400)
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Missing content'}).encode())
                return

            logging.info(f'Creating Google Doc for case {case_number}')

            def gdoc_mcp(tool, arguments):
                """Call Google Workspace MCP directly — no Claude subprocess needed."""
                import glob as _glob
                # Find latest plugin token
                token = '0d238fe9-9184-4264-aac8-1c6f28ea8ad7'
                for p in sorted(_glob.glob(os.path.join(HOME, '.claude/plugins/cache/aisuite/google-workspace/*/.mcp.json')), key=os.path.getmtime, reverse=True):
                    try:
                        with open(p) as f:
                            cfg = json.load(f)
                        t = cfg.get('mcpServers', {}).get('google-workspace', {}).get('headers', {}).get('Authorization', '')
                        if t.startswith('Bearer '):
                            token = t[7:]
                            break
                    except Exception:
                        pass
                req_body = json.dumps({'jsonrpc': '2.0', 'method': 'tools/call', 'id': 1,
                                       'params': {'name': tool, 'arguments': arguments}}).encode()
                req = urllib.request.Request(
                    'http://127.0.0.1:29051/mcp/servers/google-workspace',
                    data=req_body,
                    headers={
                        'Authorization': f'Bearer {token}',
                        'Content-Type': 'application/json',
                        'Accept': 'application/json, text/event-stream',
                    },
                    method='POST'
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        return json.loads(resp.read())
                except urllib.error.HTTPError as e:
                    body = e.read().decode('utf-8', errors='replace')
                    raise Exception(f'MCP HTTP {e.code}: {body[:500]}')

            try:
                import re as _re

                # Preprocess HTML for Google Docs:
                # <th> has white text + dark bg via CSS — GDocs strips CSS leaving invisible text.
                # Convert <th ...>content</th> → <td><strong>content</strong></td>
                gdoc_html = _re.sub(
                    r'<th([^>]*)>(.*?)</th>',
                    lambda m: f'<td><strong>{m.group(2)}</strong></td>',
                    html_content,
                    flags=_re.DOTALL | _re.IGNORECASE
                )
                # Also strip source badges and links inside th content to keep it clean
                gdoc_html = _re.sub(r'<span[^>]*class="source-badge"[^>]*>.*?</span>', '', gdoc_html, flags=_re.DOTALL)

                # Single call: import HTML directly — preserves formatting
                r1 = gdoc_mcp('import_to_google_doc', {
                    'file_name': f'RCA — Case {case_number}',
                    'content': gdoc_html[:500000],
                    'source_format': 'html',
                })
                content1 = r1.get('result', {}).get('content', [{}])
                text1 = content1[0].get('text', '') if content1 else ''
                if r1.get('result', {}).get('isError') or 'Error' in text1[:20]:
                    raise Exception(f'import_to_google_doc failed: {text1[:300]}')
                doc_id_match = _re.search(r'Document ID:\s*([\w-]{20,})', text1)
                if not doc_id_match:
                    raise Exception(f'Could not get doc ID: {text1[:200]}')
                doc_id = doc_id_match.group(1)
                logging.info(f'Created Google Doc {doc_id}')

                doc_url = f'https://docs.google.com/document/d/{doc_id}/edit'
                logging.info(f'Google Doc ready: {doc_url}')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'url': doc_url}).encode())

            except Exception as e:
                logging.error(f'Google Doc creation error: {e}')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        self.send_response(404)
        self.end_headers()


if __name__ == '__main__':
    if not CLAUDE_BIN:
        logging.error('Claude CLI not found — install with: npm install -g @anthropic-ai/claude-code')
        print('ERROR: Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code', file=sys.stderr)
        # Don't exit — keep server up so the extension can show a clear error message
    else:
        logging.info(f'Claude CLI found at: {CLAUDE_BIN}')

    server = ThreadedHTTPServer(('127.0.0.1', PORT), RCAHandler)
    logging.info(f'RCA Backend started (threaded) on port {PORT}')
    print(f'\n  RCA Backend running at http://127.0.0.1:{PORT}')
    print(f'  Claude CLI: {CLAUDE_BIN}')
    print(f'  Log: {LOG_FILE}\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info('Server stopped')
