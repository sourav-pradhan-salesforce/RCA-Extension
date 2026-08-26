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

HOME        = os.path.expanduser('~')
PORT        = 3001
LOG_FILE    = os.path.join(HOME, 'Library/Application Support/rca-backend/server.log')
SUPPORT_DIR = os.path.join(HOME, 'Library/Application Support/rca-backend')

def find_claude_bin():
    """Auto-detect claude CLI — works on any Mac regardless of how it was installed."""
    candidates = [
        os.path.join(HOME, '.local/bin/claude'),
        '/usr/local/bin/claude',
        '/opt/homebrew/bin/claude',
        '/usr/bin/claude',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    # Fall back to PATH lookup
    try:
        result = subprocess.run(['which', 'claude'], capture_output=True, text=True, timeout=5)
        path = result.stdout.strip()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None

CLAUDE_BIN  = find_claude_bin()
# Use support dir as working dir so server works regardless of where this file lives
PROJECT_DIR = SUPPORT_DIR

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)


class ThreadedHTTPServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def read_keychain_credentials():
    """Read the full MCP OAuth credentials blob from keychain."""
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
    """Write updated credentials back to keychain."""
    try:
        payload = json.dumps(data)
        subprocess.run(
            ['security', 'add-generic-password', '-U',
             '-s', 'Claude Code-credentials',
             '-a', os.environ.get('USER', 'b.mohanty'),
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
    """Build a temporary mcp_config.json with bearer tokens pre-filled."""
    cfg_path = os.path.join(SUPPORT_DIR, 'mcp_config.json')
    base_cfg = {}
    try:
        with open(os.path.join(HOME, '.claude.json')) as f:
            d = json.load(f)
            # Global MCP servers first
            base_cfg.update(d.get('mcpServers', {}))
            # Then merge ALL project-level MCP servers (covers any install path)
            for proj_cfg in d.get('projects', {}).values():
                base_cfg.update(proj_cfg.get('mcpServers', {}))
    except Exception as e:
        logging.warning(f'claude.json read error: {e}')

    servers = {}
    for name, cfg in base_cfg.items():
        token = tokens.get(name)
        if token:
            servers[name] = {
                'type': cfg.get('type', 'http'),
                'url': cfg.get('url', ''),
                'headers': {'Authorization': f'Bearer {token}'},
            }
        else:
            servers[name] = cfg

    with open(cfg_path, 'w') as f:
        json.dump({'mcpServers': servers}, f)
    logging.info(f'MCP config servers: {list(servers.keys())}')
    return cfg_path


def build_prompt(case_number, audience, template):
    is_cic = (audience == 'cic')

    sections_rule = {
        'leadership': 'Output sections 1,2,3,5,8 ONLY. Executive language, no stack traces.',
        'customer':   'Output sections 1,2,3,7 ONLY. Plain language, no internal system names.',
        'cic':        'Output all 8 sections.',
    }.get(audience, 'Output all 8 sections.')

    # GUS note: no dedicated GUS MCP, so extract W-numbers from OrgCS feed/comments
    gus_note = """D. GUS (via OrgCS) — scan CaseComment and FeedItem results for any W-XXXXXXX work item numbers.
   Also search the CaseBug__c junction object if accessible:
   SELECT ADM_Work__c, ADM_Work__r.Name, ADM_Work__r.ADM_Status__c
   FROM CaseBug__c WHERE Case__c='<CaseId>' LIMIT 5
   If W-numbers found, construct GUS links as:
   https://gus.lightning.force.com/lightning/r/ADM_Work__c/<Id>/view"""

    if is_cic:
        data_steps = f"""A. OrgCS — run these SOQL queries on the orgcs MCP server (save the 18-char Case Id):
   SELECT Id,CaseNumber,Subject,Description,Status,Priority,Severity__c,
          Sev_1_Case__c,Sev_2_Case__c,
          Account.Name,Account.Id,OrgId__c,Instance__c,Pod__c,
          Time_Format__c,
          Open_Red_Account__c,
          Success_Plan__c,
          CreatedDate,ClosedDate,
          Sev_1_Initiated_Date__c,Sev_1_Mitigated_Date__c,
          Origin,Type,Owner.Name
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1
   (If any field name errors, retry omitting the failing field — do not give up.)

   SELECT Id,CommentBody,CreatedDate,CreatedBy.Name,IsPublished
   FROM CaseComment WHERE ParentId='<CaseId>' ORDER BY CreatedDate ASC LIMIT 30

   SELECT Id,Subject,TextBody,FromAddress,CreatedDate,Incoming,MessageDate
   FROM EmailMessage WHERE ParentId='<CaseId>' ORDER BY MessageDate ASC LIMIT 20

   SELECT Id,Body,CreatedDate,CreatedBy.Name,Type
   FROM FeedItem WHERE ParentId='<CaseId>' ORDER BY CreatedDate ASC LIMIT 20

B. Org62 — use Account.Id from above:
   SELECT Id,Name,Support_Level__c,Open_Red_Account__c,Industry,
          Account_Executive__r.Name,Technical_Account_Manager__r.Name
   FROM Account WHERE Id='<AccountId>' LIMIT 1

C. Slack — search "{case_number}", "sev {case_number}", "swarm {case_number}"
   Record EXACT channel ID (e.g. C0BEQTQL5TL) and name. Read channel for:
   first alert time, error messages, actions taken, resolution time.

{gus_note}"""
    else:
        data_steps = f"""A. OrgCS — run these SOQL queries on the orgcs MCP server (save the 18-char Case Id):
   SELECT Id,CaseNumber,Subject,Description,Status,Priority,Severity__c,
          Sev_1_Case__c,Sev_2_Case__c,
          Account.Name,Account.Id,OrgId__c,Instance__c,Pod__c,
          Time_Format__c,
          Open_Red_Account__c,
          Success_Plan__c,
          CreatedDate,ClosedDate,
          Sev_1_Initiated_Date__c,Sev_1_Mitigated_Date__c,
          Origin,Type,Owner.Name
   FROM Case WHERE CaseNumber='{case_number}' LIMIT 1
   (If any field name errors, retry omitting the failing field — do not give up.)

   SELECT Id,CommentBody,CreatedDate,CreatedBy.Name,IsPublished
   FROM CaseComment WHERE ParentId='<CaseId>' ORDER BY CreatedDate ASC LIMIT 20

B. Org62 — use Account.Id from above:
   SELECT Id,Name,Support_Level__c,Open_Red_Account__c,Industry
   FROM Account WHERE Id='<AccountId>' LIMIT 1

C. Slack — search "{case_number}", "sev {case_number}"
   Record EXACT channel ID and name. Get: first alert time, resolution time, key actions.

{gus_note}"""

    return f"""You are a Salesforce Senior Support Engineer. Write a concise, precise Root Cause Analysis.
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

SECTIONS (keep each one SHORT):
1. Header table — Account Name | Case# (Sev-1, with OrgCS link) | Case# (Sev-2) | SEV Level | Production Org ID | Production Instance | Time Format | Case Opened (PST) | Sev-1 Initiated (PST) | Sev-1 Mitigated (PST) | Success Plan | Red Account | Slack Channel
   NOTE: All time cells use <span class="tz-ts" data-utc="..."> tags. Default display is PST.
2. Executive Summary — 2-3 short paragraphs: what failed, when, impact, how resolved
3. Business Impact — 3-5 bullet points: user count, groups affected, features down, SLA breach
4. Technical Details — Detection paragraph (2-3 sentences) + Remediation Timeline table (Time | Action) with only the 5-8 most important events (all times as tz-ts spans)
5. Root Cause Analysis — Primary root cause (1 precise sentence) + contributing factors (2-4 bullets) + GUS work item link
6. Support Opportunities — 2-3 bullets: process gaps with timestamps
7. Customer Opportunities — 2-3 bullets: commitments, deadlines
8. Engineering Actions — bullet list: each action with owner inline

Start output with <h1>Root Cause Analysis — Case #{case_number}</h1> immediately."""


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
            prompt_text = build_prompt(case_number, audience, template)

            try:
                tokens = get_mcp_oauth_tokens()
                # Block local filesystem/task tools — keep ToolSearch (needed to load MCP schemas)
                # and WebFetch (needed for web searches). MCP tools are allowed.
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
