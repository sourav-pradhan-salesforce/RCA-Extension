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


def _read_plugin_server(plugin_name):
    """Read latest active .mcp.json for a plugin — returns {server_name: cfg} or {}."""
    import glob as _glob
    pattern = os.path.join(HOME, f'.claude/plugins/cache/aisuite/{plugin_name}/*/.mcp.json')
    # Exclude orphaned entries
    candidates = [
        p for p in sorted(_glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if not os.path.exists(os.path.join(os.path.dirname(p), '.orphaned_at'))
    ]
    for p in candidates:
        try:
            with open(p) as f:
                d = json.load(f)
            servers = d.get('mcpServers', {})
            if servers:
                logging.info(f'Plugin {plugin_name}: loaded from {p}')
                return servers
        except Exception as e:
            logging.warning(f'Plugin {plugin_name} read error ({p}): {e}')
    return {}


def build_mcp_config(tokens):
    """Build mcp_config.json.
    - stdio servers from .claude.json (non-HTTP)
    - Plugin servers (slack, google-workspace, gus, orgcs, Org62) injected from plugin cache
      so subprocess has live authenticated connections."""
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
        if cfg.get('type') == 'http':
            continue
        servers[name] = cfg

    # Inject plugin servers from cache (these have live Bearer tokens).
    # Plugin versions override stdio stubs with the same name — plugin has fresh auth.
    for plugin in ('slack', 'google-workspace', 'dxmcp-gus', 'orgcs', 'Org62-Sobject-Read'):
        plugin_servers = _read_plugin_server(plugin)
        for sname, scfg in plugin_servers.items():
            if sname in servers:
                logging.info(f'Plugin {plugin}: overriding existing {sname} with plugin version')
            servers[sname] = scfg

    with open(cfg_path, 'w') as f:
        json.dump({'mcpServers': servers}, f)
    logging.info(f'MCP config servers: {list(servers.keys())}')
    return cfg_path


def call_plugin_mcp(plugin_name, tool, arguments, timeout=20):
    """Call a plugin MCP tool directly via the local devbar proxy."""
    import glob as _glob
    token = '0d238fe9-9184-4264-aac8-1c6f28ea8ad7'
    pattern = os.path.join(HOME, f'.claude/plugins/cache/aisuite/{plugin_name}/*/.mcp.json')
    candidates = [
        p for p in sorted(_glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if not os.path.exists(os.path.join(os.path.dirname(p), '.orphaned_at'))
    ]
    for p in candidates:
        try:
            with open(p) as f:
                cfg = json.load(f)
            t = cfg.get('mcpServers', {}).get(plugin_name.split('@')[0], {}).get('headers', {}).get('Authorization', '')
            if t.startswith('Bearer '):
                token = t[7:]
                break
        except Exception:
            pass

    url = f'http://127.0.0.1:29051/mcp/servers/{plugin_name}'
    body = json.dumps({'jsonrpc': '2.0', 'method': 'tools/call', 'id': 1,
                       'params': {'name': tool, 'arguments': arguments}}).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read())
    content = result.get('result', {}).get('content', [])
    if isinstance(content, list) and content:
        return content[0].get('text', '')
    return str(result)


def _get_ssl_context():
    """Build SSL context: certifi root CAs + corporate CA bundle."""
    import ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    ca_path = os.path.join(HOME, '.devbar/certs/corporate-ca-bundle.pem')
    if os.path.isfile(ca_path):
        ctx.load_verify_locations(ca_path)
    return ctx


def _orgcs_mcp_call(tool_name, arguments, timeout=20):
    """Call OrgCS MCP with proper initialize→tools/call session handshake."""
    creds = read_keychain_credentials()
    token = ''
    for key, val in creds.get('mcpOAuth', {}).items():
        if key.split('|')[0] == 'orgcs' and isinstance(val, dict):
            token = val.get('accessToken', '')
            break
    if not token:
        raise Exception('OrgCS token not found in keychain')

    url = 'https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads'
    ctx = _get_ssl_context()
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }

    def post(body_dict):
        req = urllib.request.Request(url, data=json.dumps(body_dict).encode(),
                                     method='POST', headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            # Capture session key from response headers if present
            session_key = r.headers.get('mcp-session-id') or r.headers.get('x-session-key') or ''
            data = json.loads(r.read())
            return data, session_key

    # Step 1: initialize
    init_msg = {
        'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'rca-backend', 'version': '1.0'},
        }
    }
    init_result, session_key = post(init_msg)
    logging.info(f'OrgCS init: session_key={session_key!r} result_keys={list(init_result.keys())}')

    # Add session key to headers if returned
    if session_key:
        headers['mcp-session-id'] = session_key

    # Step 2: initialized notification (no response expected, fire and forget)
    try:
        post({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
    except Exception:
        pass

    # Step 3: tools/call
    call_msg = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': arguments}
    }
    result, _ = post(call_msg)
    content = result.get('result', {}).get('content', [])
    if isinstance(content, list) and content:
        return content[0].get('text', str(result))
    return str(result)


def call_orgcs_soql(soql, timeout=20):
    """Call OrgCS MCP soqlQuery with session handshake. Parameter is 'q'."""
    return _orgcs_mcp_call('soqlQuery', {'q': soql}, timeout=timeout)


def call_gus_soql(soql, timeout=20):
    """Call GUS MCP query_gus_records via devbar proxy."""
    return call_plugin_mcp('dxmcp-gus', 'query_gus_records', {'soql': soql}, timeout=timeout)


def _extract_text_from_mcp(raw):
    """Parse MCP tool result — handles plain text and JSON content arrays."""
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
            content = d.get('result', {}).get('content', [])
            if content:
                return content[0].get('text', raw)
        except Exception:
            pass
    return str(raw)


def prefetch_slack_and_gus(case_number, account_name):
    """
    Server-side pre-fetch of Slack channel + GUS work items.
    Returns dict: {channel_id, channel_name, channel_url, messages_summary, gus_items}
    """
    import re as _re

    result = {
        'channel_id': None, 'channel_name': None, 'channel_url': None,
        'messages_summary': '', 'gus_items': [], 'error': None,
    }

    # ── 1. Find SEV1 channel by name pattern ────────────────────────────────
    # Slack channel naming: sev1-<account-slug>-<case_number>
    # Slug rules observed: periods→hyphens, spaces→underscores, other non-alnum→hyphens
    def make_slug(name):
        s = name.lower()
        s = s.replace('.', '-')          # periods → hyphens
        s = s.replace(' ', '_')          # spaces → underscores
        s = _re.sub(r'[^a-z0-9_-]+', '-', s)  # other chars → hyphens
        s = _re.sub(r'-+', '-', s).strip('-')  # collapse hyphens
        return s

    slug = make_slug(account_name) if account_name else ''

    # Build multiple candidate queries (case number is the most reliable anchor)
    queries = [f'sev1-{case_number}']
    if slug:
        queries.insert(0, f'sev1-{slug}-{case_number}')
        # Also try just the case number in the channel name
        queries.append(f'sev1-{slug[:20]}-{case_number}')

    logging.info(f'Prefetch: searching Slack channels with queries: {queries}')

    channel_id = None
    channel_name = None

    def parse_channel_result(raw, match_case_number=None):
        """Extract (channel_id, channel_name) from slack_search_channels result.
        Result is a JSON object with a 'results' key containing markdown text.
        Markdown format:
          Name: #sev1-account-name-123456
          Permalink: [link](https://...slack.com/archives/CXXXXXXXX)
        """
        # Unwrap JSON envelope to get the markdown string
        try:
            if isinstance(raw, str):
                d = json.loads(raw)
                text = d.get('results', str(raw))
            else:
                text = str(raw)
        except Exception:
            text = str(raw)

        # Extract name/id pairs from markdown (text is now unescaped)
        names = _re.findall(r'Name:\s*#([^\n]+)', text)
        ids   = _re.findall(r'archives/([A-Z0-9]{8,})', text)
        pairs = list(zip(names, ids))

        if not pairs:
            return None, None

        if match_case_number:
            for cname, cid in pairs:
                if match_case_number in cname:
                    return cid, cname.strip()
            return None, None  # no match for this case number — don't use wrong channel
        return pairs[0][1], pairs[0][0].strip()

    # Search by account slug only (case number at end often yields no results)
    # Truncate slug to first 2-3 meaningful words for broader match
    slug_words = [w for w in _re.split(r'[-_]', slug) if w and len(w) > 1]
    search_slug = '-'.join(slug_words[:4]) if slug_words else slug[:20]
    # Try multiple queries: account slug alone (broad), then case number alone, then combined
    queries = []
    if slug:
        queries.append(f'sev1-{search_slug}')      # broad: finds all channels for this account
    queries.append(f'sev1-{case_number}')           # narrow: case number might be indexed
    if slug:
        queries.append(f'{search_slug} {case_number}')  # combined keyword search

    logging.info(f'Prefetch: searching Slack channels with queries: {queries}')

    for q in queries:
        if channel_id:
            break
        try:
            raw = call_plugin_mcp('slack', 'slack_search_channels', {'query': q, 'limit': 50})
            cid, cname = parse_channel_result(raw, match_case_number=case_number)
            if cid:
                channel_id, channel_name = cid, cname
                logging.info(f'Prefetch: found channel via query "{q}": {channel_id} #{channel_name}')
            else:
                logging.info(f'Prefetch: query "{q}" returned results but none matched case {case_number}')
        except Exception as e:
            logging.warning(f'Prefetch: channel search "{q}" failed: {e}')
            result['error'] = str(e)

    # Fallback 1: try reading channel directly by constructed name
    if not channel_id and slug:
        constructed_name = f'sev1-{slug}-{case_number}'
        try:
            raw = call_plugin_mcp('slack', 'slack_read_channel',
                                   {'channel_id': constructed_name, 'limit': 5}, timeout=15)
            raw_str = str(raw)
            # If it returns content (not an error), extract the channel ID from the response
            if raw_str and 'error' not in raw_str.lower()[:50] and len(raw_str) > 50:
                m = _re.search(r'C[A-Z0-9]{8,}', raw_str)
                if m:
                    channel_id = m.group(0)
                else:
                    channel_id = constructed_name  # use name as identifier
                channel_name = constructed_name
                logging.info(f'Prefetch: found channel by direct name lookup: {constructed_name}')
        except Exception as e:
            logging.warning(f'Prefetch: direct name lookup failed for {constructed_name}: {e}')

    # Fallback 2: search by case number in messages
    if not channel_id:
        try:
            raw = call_plugin_mcp('slack', 'slack_search_public_and_private',
                                   {'query': f'sev1 {case_number}', 'limit': 10})
            raw_str = str(raw)
            # Look for channel IDs in message search results (Channel: #name format or archives/ID)
            cid_match = _re.search(r'archives/([A-Z0-9]{8,})', raw_str)
            if cid_match:
                channel_id = cid_match.group(1)
                channel_name = f'sev1-{slug}-{case_number}' if slug else f'sev1-{case_number}'
                logging.info(f'Prefetch: found channel ID via message search: {channel_id}')
            else:
                m = _re.search(r'C[A-Z0-9]{8,}', raw_str)
                if m:
                    channel_id = m.group(0)
                    channel_name = f'sev1-{slug}-{case_number}' if slug else f'sev1-{case_number}'
            logging.info(f'Prefetch: message search fallback — channel_id={channel_id}')
        except Exception as e:
            logging.warning(f'Prefetch: message search fallback failed: {e}')

    if channel_id:
        result['channel_id'] = channel_id
        result['channel_name'] = channel_name or f'sev1-{slug}-{case_number}'
        result['channel_url'] = f'https://salesforce.enterprise.slack.com/archives/{channel_id}'

        # ── 2. Read channel history ──────────────────────────────────────────
        try:
            raw = call_plugin_mcp('slack', 'slack_read_channel',
                                   {'channel_id': channel_id, 'limit': 50}, timeout=30)
            result['messages_summary'] = str(raw)[:6000]
            logging.info(f'Prefetch: read {len(str(raw))} chars from channel {channel_id}')
        except Exception as e:
            logging.warning(f'Prefetch: channel read failed: {e}')
            try:
                raw = call_plugin_mcp('slack', 'slack_get_channel_history',
                                       {'channel_id': channel_id, 'limit': 50}, timeout=30)
                result['messages_summary'] = str(raw)[:6000]
            except Exception as e2:
                logging.warning(f'Prefetch: channel history fallback failed: {e2}')

    # ── 3. Get W-numbers from OrgCS — try multiple approaches ───────────────
    w_numbers_orgcs = []
    case_id = ''
    try:
        raw = call_orgcs_soql(f"SELECT Id FROM Case WHERE CaseNumber='{case_number}' LIMIT 1")
        id_match = _re.search(r'"Id"\s*:\s*"([0-9A-Za-z]{15,18})"', str(raw))
        if id_match:
            case_id = id_match.group(1)

            # Case_Relationship__c is the correct junction object in OrgCS
            try:
                raw2 = call_orgcs_soql(
                    f"SELECT GUS_Work__c, GUS_Work__r.Name FROM Case_Relationship__c WHERE Case__c='{case_id}' LIMIT 10"
                )
                w_numbers_orgcs = list(dict.fromkeys(_re.findall(r'W-\d{6,}', str(raw2))))
                logging.info(f'Prefetch: W-numbers from Case_Relationship__c: {w_numbers_orgcs}')
            except Exception as e:
                logging.warning(f'Prefetch: Case_Relationship__c query failed: {e}')
    except Exception as e:
        logging.warning(f'Prefetch: case ID lookup failed: {e}')

    # ── 4. Extract W-numbers from OrgCS comments ─────────────────────────────
    w_numbers_comments = []
    if case_id:
        try:
            raw = call_orgcs_soql(
                f"SELECT CommentBody FROM CaseComment WHERE ParentId='{case_id}' ORDER BY CreatedDate ASC LIMIT 30"
            )
            w_numbers_comments = list(dict.fromkeys(_re.findall(r'W-\d{6,}', str(raw))))
            logging.info(f'Prefetch: W-numbers from comments: {w_numbers_comments}')
        except Exception as e:
            logging.warning(f'Prefetch: comment scan failed: {e}')

    # ── 5. Extract W-numbers from Slack messages ──────────────────────────────
    w_numbers_slack = list(dict.fromkeys(_re.findall(r'W-\d{6,}', result['messages_summary'])))
    logging.info(f'Prefetch: W-numbers from Slack: {w_numbers_slack}')

    # Merge all W-numbers, CaseBug__c first (most authoritative)
    seen = set()
    w_numbers = []
    for w in w_numbers_orgcs + w_numbers_comments + w_numbers_slack:
        if w not in seen:
            seen.add(w)
            w_numbers.append(w)
    logging.info(f'Prefetch: total unique W-numbers: {w_numbers}')

    # ── 6. Query GUS for each W-number ───────────────────────────────────────
    for wnum in w_numbers[:8]:
        try:
            raw = call_gus_soql(
                f"SELECT Id,Name,Subject__c,Status__c,Priority__c,Type__c,Assignee__r.Name,Product_Tag__r.Name,Scheduled_Build__c FROM ADM_Work__c WHERE Name='{wnum}' LIMIT 1"
            )
            if raw and wnum in str(raw):
                result['gus_items'].append({'wnum': wnum, 'data': str(raw)[:800]})
                logging.info(f'Prefetch: GUS item found for {wnum}')
            else:
                # Still record the W-number even if GUS query returned nothing
                result['gus_items'].append({'wnum': wnum, 'data': str(raw)[:400]})
                logging.warning(f'Prefetch: GUS query for {wnum} returned no match: {str(raw)[:100]}')
        except Exception as e:
            logging.warning(f'Prefetch: GUS query failed for {wnum}: {e}')
            result['gus_items'].append({'wnum': wnum, 'data': f'GUS query error: {e}'})

    logging.info(f'Prefetch complete: channel={result["channel_id"]}, gus_items={len(result["gus_items"])}')
    return result


def _is_escalation_template(text):
    t = text.upper()
    return 'EXECUTIVE ESCALATION' in t or 'LIVING ONE-PAGER' in t


def build_escalation_prompt(case_number, data_steps):
    css = """<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Salesforce Sans',Arial,sans-serif;background:#fff;color:#181818;font-size:13px;}
.page{max-width:860px;margin:0 auto;padding:32px 40px;}
h1{font-size:16px;font-weight:800;color:#032D60;text-transform:uppercase;letter-spacing:1.5px;
   margin-bottom:4px;border-bottom:3px solid #0176D3;padding-bottom:8px;}
.esc-subtitle{font-size:10px;color:#706E6B;font-style:italic;margin-bottom:20px;margin-top:4px;}
h2{background:#032D60;color:#fff;padding:8px 16px;font-size:11px;font-weight:700;
   text-transform:uppercase;letter-spacing:1px;margin-top:20px;margin-bottom:0;}
table{width:100%;border-collapse:collapse;font-size:13px;}
table th{background:#F3F3F3;font-weight:700;padding:8px 12px;border:1px solid #E0E0E0;text-align:left;}
table td{padding:8px 12px;border:1px solid #E0E0E0;vertical-align:top;}
.fv-field{width:35%;font-weight:600;background:#FAFAFA;color:#032D60;}
.fv-value{background:#fff;}
tr:nth-child(odd) td{background:#FAFAFA;}
tr:nth-child(even) td{background:#fff;}
.fv-field{background:#FAFAFA!important;}
.fv-value{background:#fff!important;}
.escalation-footer{margin-top:28px;padding-top:10px;border-top:1px solid #E0E0E0;
   font-size:10px;color:#706E6B;font-style:italic;}
a{color:#0176D3;text-decoration:none;}
a:hover{text-decoration:underline;}
.source-badge{display:inline-block;font-size:9px;font-weight:700;padding:1px 6px;
   border-radius:3px;background:#E8F4FD;color:#032D60;margin-left:4px;vertical-align:middle;}
.tz-ts{font-variant-numeric:tabular-nums;}
</style>"""

    prompt = f"""You are a Salesforce Senior Support Engineer writing an Executive Escalation Living One-Pager.
IMPORTANT: Do NOT output the default RCA format. Output ONLY the escalation one-pager HTML described below.

RULES:
- NEVER write to a file. Output HTML to stdout ONLY.
- Do NOT repeat a tool call that already returned data.
- If a field has no data, write "Under Investigation" in that cell.
- Output the full HTML immediately after collecting data.

PHASE 1 — COLLECT DATA
{data_steps}

PHASE 2 — OUTPUT HTML

Output a single self-contained HTML fragment (no <html>/<body> tags) that starts with the <style> block below,
then the document content. Follow the exact structure shown.

TIMEZONE RULE — ALL timestamps:
  <span class="tz-ts" data-utc="<ISO-8601-UTC>">display text</span>

STATUS / TEMPERATURE COLOR RULES:
- STATUS "In Progress"  → <span style="color:#FE9339;font-weight:700;">🟡 In Progress</span>
- STATUS "Resolved"     → <span style="color:#2E844A;font-weight:700;">🟢 Resolved</span>
- STATUS "Closed"       → <span style="color:#2E844A;font-weight:700;">🟢 Closed</span>
- TEMPERATURE "Hot"     → <span style="color:#BA0517;font-weight:700;">🔴 Hot</span>
- TEMPERATURE "Warm"    → <span style="color:#FE9339;font-weight:700;">🟡 Warm</span>
- TEMPERATURE "Cool"/"Cold" → <span style="color:#2E844A;font-weight:700;">🟢 Cool</span>

DATA MAPPING — extract from collected data:
- Customer              ← Account.Name
- Case/Incident #       ← CaseNumber as <a href="https://orgcs.lightning.force.com/lightning/r/Case/<CaseId>/view" target="_blank" class="source-link">CaseNumber ↗</a>
- Support Tier          ← Case_Support_level__c
- Red Account           ← Open_Red_Account__c (Yes/No)
- AOV Band              ← search OrgCS comments + Slack for dollar band (e.g. $1M-5M); write "Under Investigation" if not found
- ACV at Risk           ← search OrgCS comments + Slack; write "Under Investigation" if not found
- Renewal Date          ← search Org62 Account or Slack; write "Under Investigation" if not found
- Escalation Reason     ← from case Type, Subject, or Slack context (e.g. "Technical / CX")
- Escalation History    ← count prior Sev-1 cases from Slack or write "Under Investigation"
- Escalation Owner/DRI  ← Owner.Name from A1 SOQL + email if visible
- CIC Owner             ← search Slack for "Case Commander" or "CIC"; write "Under Investigation" if not found
- Days Open             ← integer days from CreatedDate to today ({__import__('datetime').date.today().isoformat()})
- STATUS                ← map case Status to In Progress / Resolved / Closed
- TEMPERATURE           ← infer from Slack message tone and customer urgency (Hot/Warm/Cool)
- UPDATE #              ← count from Slack or "1"
- NEXT UPDATE           ← from Slack or "TBD"
- UPDATE TYPE           ← "Awareness" or "Action Required" based on context
- What's broken         ← Subject/Description — 1 precise sentence
- What customer can't do ← 1 sentence business impact
- Business consequence  ← user count + business risk
- Root Cause Status     ← "Confirmed" or "Under Investigation"
- Root Cause Summary    ← 2-3 precise sentences
- Ruled out             ← from engineering notes in Slack/comments
- Fix identified        ← from engineering actions
- Deployment window     ← from Slack/engineering timeline
- Swim lanes            ← Technical / Customer-Exec / Commercial tracks from Slack
- Customer informed     ← Yes/No + date from Slack
- Temperature evidence  ← 2-3 sentences from Slack tone and customer messages
- Last contact          ← most recent OrgCS comment/email or Slack message date + author
- Trust status          ← "Recoverable", "Degraded", or "Critical" based on tone
- Customer's specific ask ← numbered list of customer asks from Slack/comments
- Exec-to-exec call     ← from Slack/comments or "Not yet arranged"
- SLA Status            ← "Within SLA" or "Breached" (only include row if non-default)
- Renewal Risk          ← "Monitoring", "At Risk", "High Risk" (only include if non-default)
- Legal Engaged         ← Yes/No (only include if Yes)
- PR/Media Exposure     ← Yes/No (only include if Yes)
- Next Steps            ← max 4 actions from engineering/Slack, each with named owner + specific date
- Changelog             ← from OrgCS comments + Slack messages, newest first, format: <tz-ts> | what happened — Author Name

EXACT HTML STRUCTURE TO OUTPUT:

{css}
<div class="page">
<h1 data-default-tz="<IANA-tz-from-support_available_timezone__c-or-America/Los_Angeles>">EXECUTIVE ESCALATION — LIVING ONE-PAGER</h1>
<p class="esc-subtitle">Always current. Update in place — do not reissue as a new document. Everything above the changelog reflects the state as of right now; the changelog is the only place history lives.</p>

<h2>ACCOUNT — STATIC FACTS</h2>
<p style="font-size:10px;color:#706E6B;padding:4px 0 8px 0;">Set once at Update 1. Does not change across the life of the escalation.</p>
<table>
  <tr><td class="fv-field">Customer</td><td class="fv-value">VALUE <span class="source-badge">OrgCS</span></td></tr>
  <tr><td class="fv-field">Case / Incident #</td><td class="fv-value">LINK</td></tr>
  <tr><td class="fv-field">Support Tier</td><td class="fv-value">VALUE <span class="source-badge">OrgCS</span></td></tr>
  <tr><td class="fv-field">Red Account</td><td class="fv-value">VALUE <span class="source-badge">OrgCS</span></td></tr>
  <tr><td class="fv-field">AOV Band</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">ACV at Risk</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Renewal Date</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Escalation Reason</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Escalation History</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Escalation Owner / DRI</td><td class="fv-value">VALUE <span class="source-badge">OrgCS</span></td></tr>
  <tr><td class="fv-field">CIC Owner</td><td class="fv-value">VALUE <span class="source-badge">Slack</span></td></tr>
  <tr><td class="fv-field">Days Open</td><td class="fv-value">N days <span class="source-badge">OrgCS</span></td></tr>
</table>

<h2>CURRENT STATE</h2>
<p style="font-size:10px;color:#706E6B;padding:4px 0 8px 0;">The only strip that must be re-checked every time this doc is touched.</p>
<table>
  <tr><td class="fv-field">STATUS</td><td class="fv-value">COLORED-STATUS-SPAN</td></tr>
  <tr><td class="fv-field">TEMPERATURE</td><td class="fv-value">COLORED-TEMP-SPAN</td></tr>
  <tr><td class="fv-field">UPDATE #</td><td class="fv-value">N - last touched <span class="tz-ts" data-utc="...">...</span></td></tr>
  <tr><td class="fv-field">NEXT UPDATE</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">UPDATE TYPE</td><td class="fv-value">VALUE</td></tr>
</table>

<h2>THE ISSUE IN 3 LINES</h2>
<table>
  <tr><td class="fv-field">What's broken</td><td class="fv-value">1 precise sentence</td></tr>
  <tr><td class="fv-field">What the customer can't do</td><td class="fv-value">1 sentence</td></tr>
  <tr><td class="fv-field">Business consequence</td><td class="fv-value">user count + risk</td></tr>
</table>

<h2>ROOT CAUSE</h2>
<table>
  <tr><td class="fv-field">Status</td><td class="fv-value">Confirmed or Under Investigation</td></tr>
  <tr><td class="fv-field">Summary</td><td class="fv-value">2-3 sentence root cause</td></tr>
  <tr><td class="fv-field">Ruled out</td><td class="fv-value">What was ruled out</td></tr>
</table>

<h2>PATH TO GREEN</h2>
<table>
  <tr><td class="fv-field">Fix identified</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Deployment window</td><td class="fv-value">VALUE</td></tr>
  <tr><td class="fv-field">Swim lanes (parallel, not sequential)</td><td class="fv-value">Technical — [detail, ETA]<br>Customer/Exec — [detail, ETA]<br>Commercial — [detail, ETA]</td></tr>
  <tr><td class="fv-field">Customer informed of path</td><td class="fv-value">Yes/No [date] — detail</td></tr>
</table>

<h2>CUSTOMER STATE</h2>
<table>
  <tr><td class="fv-field">Temperature evidence</td><td class="fv-value">2-3 sentences from Slack/comments tone</td></tr>
  <tr><td class="fv-field">Last contact</td><td class="fv-value"><span class="tz-ts" data-utc="...">...</span> by Name</td></tr>
  <tr><td class="fv-field">Trust status</td><td class="fv-value">Recoverable / Degraded / Critical</td></tr>
  <tr><td class="fv-field">Customer's specific ask</td><td class="fv-value"><ol style="margin:0;padding-left:16px;"><li>Ask 1</li><li>Ask 2</li></ol></td></tr>
  <tr><td class="fv-field">Exec-to-exec call</td><td class="fv-value">VALUE</td></tr>
</table>

<h2>COMMERCIAL &amp; RISK FLAGS</h2>
<p style="font-size:10px;color:#706E6B;padding:4px 0 8px 0;">Show only fields that are non-default (not 'Within SLA' / 'No'). Omit this section entirely if everything is default.</p>
<table>
  <!-- Only include rows for non-default values. If all are default, output only the note paragraph above and no table. -->
  <tr><td class="fv-field">SLA Status</td><td class="fv-value">VALUE — only if NOT "Within SLA"</td></tr>
  <tr><td class="fv-field">Renewal Risk</td><td class="fv-value">VALUE — only if NOT "No"</td></tr>
  <tr><td class="fv-field">Legal Engaged</td><td class="fv-value">Yes — only if Yes</td></tr>
  <tr><td class="fv-field">PR / Media Exposure</td><td class="fv-value">Yes — only if Yes</td></tr>
</table>

<h2>NEXT STEPS</h2>
<p style="font-size:10px;color:#706E6B;padding:4px 0 8px 0;">Max 4. Every action has a named role and a date — never TBD.</p>
<table>
  <tr><th style="width:55%">Action</th><th style="width:25%">Owner (Role)</th><th style="width:20%">Due</th></tr>
  <tr><td>Action description</td><td>Name (Role)</td><td><span class="tz-ts" data-utc="...">...</span></td></tr>
</table>

<h2>CHANGELOG</h2>
<p style="font-size:10px;color:#706E6B;padding:4px 0 8px 0;">Append only. Newest entry on top. This is the one place prior-update history is allowed to live.</p>
<table>
  <tr><th style="width:22%">Date / Time</th><th>What Changed (newest on top)</th></tr>
  <!-- One row per Slack message or OrgCS comment, newest first -->
  <tr><td><span class="tz-ts" data-utc="...">...</span></td><td>What happened — Author Name</td></tr>
</table>

<footer class="escalation-footer">Golden rules: business first, tech second · delta only in the changelog · name the owner, never TBD · paragraphs 3 lines max · unknown = state it explicitly · timestamp everything.</footer>
</div>"""

    return prompt


def build_prompt(case_number, audience, template, template_text=None, prefetch=None):
    is_cic = (audience == 'cic')

    sections_rule = {
        'leadership': 'Output sections 1,2,3,5,8 ONLY (plus section 9 if case not Closed). Executive language, no stack traces.',
        'customer':   'Output sections 1,2,3,7 ONLY (plus section 9 if case not Closed). Plain language, no internal system names.',
        'cic':        'Output all sections (1-8, plus section 9 if case not Closed).',
    }.get(audience, 'Output all sections (1-8, plus section 9 if case not Closed).')

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

    # Build Slack + GUS sections — decouple channel from GUS items
    gus_pre = prefetch.get('gus_items', []) if prefetch else []

    if prefetch and prefetch.get('channel_id'):
        ch_id   = prefetch['channel_id']
        ch_name = prefetch['channel_name'] or f'sev1-channel-{case_number}'
        ch_url  = prefetch['channel_url']
        msgs    = prefetch.get('messages_summary', '')

        slack_section = f"""C. Slack — PRE-FETCHED (do NOT call Slack MCP tools):
   Channel: #{ch_name} (ID: {ch_id})
   URL: {ch_url}
   Messages (first 6000 chars):
{msgs[:6000]}

   USE this data for: first alert time, error messages, actions taken, resolution time.
   Channel link for RCA: <a href="{ch_url}" target="_blank" class="source-link">#{ch_name} ↗</a>"""
    else:
        slack_section = f"""C. Slack — scan OrgCS comments (A3) for Slack channel URLs or IDs first.
   If found, use that channel ID directly.
   Otherwise search messages: "{case_number}", "sev {case_number}"
   Try mcp__plugin_slack_slack__slack_search_public_and_private; on error skip Slack and write "Not found".
   Record EXACT channel ID and name."""

    if gus_pre:
        gus_items_text = '\n'.join(
            f'   {g["wnum"]}:\n{g["data"][:600]}' for g in gus_pre
        )
        gus_section = f"""D. GUS — PRE-FETCHED (do NOT call GUS MCP tools):
{gus_items_text}

   For each W-number above, build the GUS link:
   <a href="https://gus.lightning.force.com/lightning/r/ADM_Work__c/<Id>/view" target="_blank" class="source-link">W-XXXXXXX ↗</a>
   Include Subject, Status, Priority, Assignee, Scheduled_Build__c in the RCA.
   Also scan OrgCS comments (A3) for additional W-numbers not already listed above."""
    else:
        gus_section = f"""D. GUS — no items pre-fetched. Search OrgCS comments (A3) for W-\\d+ patterns.
   {gus_note}"""

    if is_cic:
        data_steps = f"""{orgcs_core}

{org62_query}

{slack_section}

{gus_section}"""
    else:
        data_steps = f"""{orgcs_non_cic}

{org62_query}

{slack_section}

{gus_section}"""

    # Route to escalation template if uploaded PDF matches that format
    if template_text and _is_escalation_template(template_text):
        return build_escalation_prompt(case_number, data_steps)

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
   Slack Channel (channel name as clickable link ONLY — NEVER add any text after the link; if channel unknown write ONLY the two words "Not found" with nothing else) |
   GUS Investigation (W-number as GUS link + " — Status: <status>" + GUS badge; if none write "None linked") |
   Case Owner (ONLY use Owner.Name from the A1 SOQL result — this is the Salesforce support engineer assigned to the case. NEVER use contact names, customer names, or names found in comments/emails/Slack. If Sev-1 assignee is explicitly named in Slack as the on-call engineer, append as "Primary Owner / Sev-1: Name", otherwise show Owner.Name alone.) |
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
9. Current & Next Steps — INCLUDE THIS SECTION ONLY if Case Status (from A1) is NOT "Closed".
   If status is "Working", "Open", "In Progress", "New", or any non-closed value, output:
   <h2>9. Current &amp; Next Steps</h2>
   TWO sub-sections:
   - <strong>Currently In Progress:</strong> 2-4 bullets — what is actively being worked on RIGHT NOW
     (pull from most recent Slack messages, latest OrgCS comments, engineering actions in progress)
   - <strong>Next Steps:</strong> 2-4 bullets — what happens next, each with a named owner and estimated date/timeframe
     (pull from Slack, OrgCS comments, GUS work item status)
   Keep each bullet to 1 line. If case IS "Closed", skip section 9 entirely — do not output it.

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
            gus_items_raw = params.get('gusItems', [''])[0].strip()
            # Parse comma-separated W-numbers from user input
            import re as _re2
            manual_w_numbers = [w.strip() for w in _re2.split(r'[\s,;]+', gus_items_raw) if _re2.match(r'W-\d+', w.strip())] if gus_items_raw else []

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

            logging.info(f'Starting RCA for case {case_number}, audience={audience}')

            # ── Server-side Slack + GUS pre-fetch (bypasses subprocess MCP issues) ──
            sse_write('status', {'step': 'slack', 'msg': 'Searching Slack SEV channel…'})
            prefetch_data = None
            try:
                # We need account name for channel slug — fetch it from OrgCS first
                # Quick orgcs call to get account name
                account_name = ''
                try:
                    orgcs_raw = call_orgcs_soql(
                        f"SELECT Account.Name FROM Case WHERE CaseNumber='{case_number}' LIMIT 1"
                    )
                    import re as _re
                    m = _re.search(r'"Name"\s*:\s*"([^"]+)"', str(orgcs_raw))
                    if m:
                        account_name = m.group(1)
                        logging.info(f'Pre-fetch account name: {account_name}')
                except Exception as e:
                    logging.warning(f'Pre-fetch account name lookup failed: {e}')

                prefetch_data = prefetch_slack_and_gus(case_number, account_name or case_number)

                # Merge manually-entered W-numbers (query GUS for any not already found)
                if manual_w_numbers:
                    sse_write('console', {'line': f'→ GUS    manual W-numbers: {manual_w_numbers}', 'kind': 'tool'})
                    existing_wnums = {g['wnum'] for g in prefetch_data.get('gus_items', [])}
                    for wnum in manual_w_numbers:
                        if wnum not in existing_wnums:
                            try:
                                raw = call_gus_soql(
                                    f"SELECT Id,Name,Subject__c,Status__c,Priority__c,Type__c,Assignee__r.Name,Product_Tag__r.Name,Scheduled_Build__c FROM ADM_Work__c WHERE Name='{wnum}' LIMIT 1"
                                )
                                prefetch_data['gus_items'].append({'wnum': wnum, 'data': str(raw)[:800]})
                                sse_write('console', {'line': f'   ✓ GUS {wnum} fetched', 'kind': 'result'})
                            except Exception as e:
                                prefetch_data['gus_items'].append({'wnum': wnum, 'data': f'GUS error: {e}'})

                if prefetch_data.get('channel_id'):
                    sse_write('status', {'step': 'slack', 'msg': f'Slack channel found: #{prefetch_data["channel_name"]}'})
                    sse_write('console', {'line': f'→ Slack  #{prefetch_data["channel_name"]} ({prefetch_data["channel_id"]})', 'kind': 'tool'})
                    sse_write('console', {'line': f'   ✓ {len(prefetch_data.get("messages_summary",""))} chars fetched', 'kind': 'result'})
                else:
                    sse_write('console', {'line': '   ✗ Slack channel not found via pre-fetch', 'kind': 'error'})

                if prefetch_data.get('gus_items'):
                    sse_write('status', {'step': 'gus', 'msg': f'GUS: {len(prefetch_data["gus_items"])} work item(s) found'})
                    for g in prefetch_data['gus_items']:
                        sse_write('console', {'line': f'→ GUS    {g["wnum"]}', 'kind': 'tool'})
            except Exception as e:
                logging.warning(f'Pre-fetch failed: {e}')
                sse_write('console', {'line': f'   ✗ Pre-fetch error: {str(e)[:80]}', 'kind': 'error'})

            env = get_claude_env()

            # Build MCP config with pre-authorised tokens
            tmpl_text = TEMPLATES.get(template_id, {}).get('text') if template_id else None
            if tmpl_text:
                logging.info(f'Using template {template_id} ({len(tmpl_text)} chars)')
            prompt_text = build_prompt(case_number, audience, template,
                                       template_text=tmpl_text, prefetch=prefetch_data)

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

                gdoc_html = html_content

                def strip_div_block(html, pattern):
                    """Strip a top-level div whose opening tag matches pattern, handling nested divs."""
                    result = []
                    i = 0
                    while i < len(html):
                        m = _re.search(pattern, html[i:], _re.IGNORECASE | _re.DOTALL)
                        if not m:
                            result.append(html[i:])
                            break
                        # Append everything before this div
                        result.append(html[i:i + m.start()])
                        # Walk forward counting open/close divs to find matching end
                        pos = i + m.end()
                        depth = 1
                        while pos < len(html) and depth > 0:
                            open_m  = _re.search(r'<div\b', html[pos:], _re.IGNORECASE)
                            close_m = _re.search(r'</div\s*>', html[pos:], _re.IGNORECASE)
                            if close_m and (not open_m or close_m.start() < open_m.start()):
                                pos += close_m.end()
                                depth -= 1
                            elif open_m:
                                pos += open_m.end()
                                depth += 1
                            else:
                                break
                        i = pos
                    return ''.join(result)

                # Strip toolbar (PDF/GDoc/Edit buttons + With Source/Without toggle)
                gdoc_html = strip_div_block(gdoc_html, r'<div[^>]*class="[^"]*\btoolbar\b[^"]*"')
                # Strip timezone sidebar (nested divs)
                gdoc_html = strip_div_block(gdoc_html, r'<div[^>]*(?:id="tzSidebar"|class="[^"]*\btz-sidebar\b[^"]*")')
                # Strip source badges
                gdoc_html = _re.sub(r'<span[^>]*class="source-badge"[^>]*>.*?</span>', '', gdoc_html, flags=_re.DOTALL)

                # Make h1 (document title) bold
                gdoc_html = _re.sub(
                    r'<h1([^>]*)>(.*?)</h1>',
                    lambda m: f'<h1{m.group(1)}><strong>{m.group(2)}</strong></h1>',
                    gdoc_html, flags=_re.DOTALL | _re.IGNORECASE
                )
                # Make h2 section headings bold
                gdoc_html = _re.sub(
                    r'<h2([^>]*)>(.*?)</h2>',
                    lambda m: f'<h2{m.group(1)}><strong>{m.group(2)}</strong></h2>',
                    gdoc_html, flags=_re.DOTALL | _re.IGNORECASE
                )

                # <th> has white text + dark bg via CSS — GDocs strips CSS leaving invisible text.
                # Convert <th ...>content</th> → <td><strong>content</strong></td>
                gdoc_html = _re.sub(
                    r'<th([^>]*)>(.*?)</th>',
                    lambda m: f'<td><strong>{m.group(2)}</strong></td>',
                    gdoc_html,
                    flags=_re.DOTALL | _re.IGNORECASE
                )

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
