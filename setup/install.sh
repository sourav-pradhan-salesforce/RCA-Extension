#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  RCA Analysis — One-Command Installer
#  Usage: bash install.sh
# ─────────────────────────────────────────────────────────────

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
info() { echo -e "  ${CYAN}→${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; }
step() { echo -e "\n${BOLD}${BLUE}$1${NC}"; }
hr()   { echo -e "${BLUE}────────────────────────────────────────────────────────${NC}"; }

clear
hr
echo -e "${BOLD}   RCA Analysis — Installer${NC}"
echo -e "   Salesforce Support Intelligence Extension"
hr

# ── Locate this script's directory ───────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
EXTENSION_DIR="$PROJECT_DIR/rca-extension"
SERVER_SRC="$PROJECT_DIR/rca-proxy/server.py"
USER_HOME="$HOME"
SUPPORT_DIR="$USER_HOME/Library/Application Support/rca-backend"
LAUNCH_AGENTS="$USER_HOME/Library/LaunchAgents"
PLIST_DEST="$LAUNCH_AGENTS/com.rca.backend.plist"

# ── Step 1: Check Prerequisites ───────────────────────────────
step "STEP 1 — Checking prerequisites"

# Python 3
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    ok "Python 3 found ($PY_VER)"
else
    fail "Python 3 not found."
    echo -e "       Install Xcode Command Line Tools: ${CYAN}xcode-select --install${NC}"
    exit 1
fi

# Find Python path
PYTHON_BIN=$(command -v python3)
# Prefer CLT python for stability
if [ -f "/Library/Developer/CommandLineTools/usr/bin/python3" ]; then
    PYTHON_BIN="/Library/Developer/CommandLineTools/usr/bin/python3"
fi
info "Using Python: $PYTHON_BIN"

# Claude Code CLI
CLAUDE_BIN=""
for candidate in \
    "$USER_HOME/.local/bin/claude" \
    "/usr/local/bin/claude" \
    "$(command -v claude 2>/dev/null)"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
        CLAUDE_BIN="$candidate"
        break
    fi
done

if [ -n "$CLAUDE_BIN" ]; then
    CLAUDE_VER=$("$CLAUDE_BIN" --version 2>&1 | head -1)
    ok "Claude Code found: $CLAUDE_BIN ($CLAUDE_VER)"
else
    fail "Claude Code CLI not found."
    echo ""
    echo -e "  ${YELLOW}Claude Code is REQUIRED. Install it:${NC}"
    echo -e "  ${CYAN}npm install -g @anthropic-ai/claude-code${NC}"
    echo -e "  Then log in: ${CYAN}claude login${NC}"
    echo ""
    echo -e "  ${YELLOW}After installing Claude Code, run this installer again.${NC}"
    exit 1
fi

# Check Claude login
CLAUDE_SETTINGS="$USER_HOME/.claude/settings.json"
if [ -f "$CLAUDE_SETTINGS" ]; then
    ok "Claude Code settings found"
else
    warn "Claude Code settings not found — you may need to run 'claude login'"
fi

# Chrome
if [ -d "/Applications/Google Chrome.app" ] || \
   [ -d "$USER_HOME/Applications/Google Chrome.app" ]; then
    ok "Google Chrome found"
else
    warn "Google Chrome not found — you'll need it to load the extension"
fi

# ── Step 2: Copy server ────────────────────────────────────────
step "STEP 2 — Installing backend server"

mkdir -p "$SUPPORT_DIR"

if [ ! -f "$SERVER_SRC" ]; then
    fail "server.py not found at $SERVER_SRC"
    exit 1
fi

cp "$SERVER_SRC" "$SUPPORT_DIR/server.py"
ok "Server copied to $SUPPORT_DIR"

# ── Step 3: Install LaunchAgent ────────────────────────────────
step "STEP 3 — Setting up auto-start (LaunchAgent)"

mkdir -p "$LAUNCH_AGENTS"

# Stop existing if running
if launchctl list | grep -q "com.rca.backend" 2>/dev/null; then
    info "Stopping existing service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

# Write plist with correct paths for THIS user
cat > "$PLIST_DEST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.rca.backend</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$SUPPORT_DIR/server.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$USER_HOME</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$USER_HOME</string>
        <key>PATH</key>
        <string>$USER_HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$(dirname "$CLAUDE_BIN")</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$SUPPORT_DIR/server.log</string>

    <key>StandardErrorPath</key>
    <string>$SUPPORT_DIR/server.log</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

ok "LaunchAgent written to $PLIST_DEST"

# Load it
launchctl load "$PLIST_DEST"
ok "LaunchAgent loaded — server will start now and on every login"

# ── Step 4: Wait for server ────────────────────────────────────
step "STEP 4 — Waiting for server to start"

MAX_WAIT=15
for i in $(seq 1 $MAX_WAIT); do
    HEALTH=$(curl -s --max-time 1 http://127.0.0.1:3001/health 2>/dev/null)
    if echo "$HEALTH" | grep -q '"ok"'; then
        ok "Backend server is running on http://127.0.0.1:3001"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        warn "Server did not start within ${MAX_WAIT}s — check log:"
        warn "$SUPPORT_DIR/server.log"
    fi
    sleep 1
    printf "  Waiting... (%d/%d)\r" $i $MAX_WAIT
done
echo ""

# ── Step 5: MCP Check ─────────────────────────────────────────
step "STEP 5 — MCP server status"

MCP_OAUTH="$USER_HOME/.claude/mcp-needs-auth-cache.json"
KEYCHAIN_DATA=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || echo "{}")
SLACK_OK=false; ORGCS_OK=false; ORG62_OK=false

if echo "$KEYCHAIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print('slack' in str(d))" 2>/dev/null | grep -q "True"; then
    SLACK_OK=true
fi
if echo "$KEYCHAIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print('orgcs' in str(d))" 2>/dev/null | grep -q "True"; then
    ORGCS_OK=true
fi
if echo "$KEYCHAIN_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Org62' in str(d))" 2>/dev/null | grep -q "True"; then
    ORG62_OK=true
fi

if $SLACK_OK;  then ok "Slack MCP authenticated"; else warn "Slack MCP not authenticated"; fi
if $ORGCS_OK;  then ok "OrgCS MCP authenticated"; else warn "OrgCS MCP not authenticated"; fi
if $ORG62_OK;  then ok "Org62 MCP authenticated"; else warn "Org62 MCP not authenticated"; fi

if ! $SLACK_OK || ! $ORGCS_OK || ! $ORG62_OK; then
    echo ""
    echo -e "  ${YELLOW}To connect missing MCPs:${NC}"
    echo -e "  1. Open Terminal"
    echo -e "  2. Run: ${CYAN}claude${NC}"
    echo -e "  3. Type: ${CYAN}/mcp${NC}"
    echo -e "  4. Authenticate each missing server (opens browser)"
fi

# ── Step 6: Print Chrome instructions ─────────────────────────
step "STEP 6 — Load Chrome Extension (manual, 30 seconds)"

echo ""
echo -e "  ${BOLD}Open Google Chrome and follow these steps:${NC}"
echo ""
echo -e "  ${CYAN}1.${NC} Go to:  ${BOLD}chrome://extensions${NC}"
echo -e "  ${CYAN}2.${NC} Turn ON ${BOLD}Developer mode${NC} (toggle, top-right corner)"
echo -e "  ${CYAN}3.${NC} Click  ${BOLD}Load unpacked${NC}"
echo -e "  ${CYAN}4.${NC} Select this folder:"
echo -e "     ${BOLD}$EXTENSION_DIR${NC}"
echo -e "  ${CYAN}5.${NC} The ${BOLD}RCA Analysis${NC} extension will appear in your toolbar"
echo ""

# Auto-open Chrome to extensions page
if open -a "Google Chrome" "chrome://extensions" 2>/dev/null; then
    info "Opening Chrome extensions page..."
fi

# ── Done ───────────────────────────────────────────────────────
hr
echo -e "${BOLD}${GREEN}   Installation complete!${NC}"
hr
echo ""
echo -e "  ${BOLD}How to use:${NC}"
echo -e "  ${CYAN}1.${NC} Click the RCA Analysis icon in Chrome toolbar"
echo -e "  ${CYAN}2.${NC} Enter a Salesforce case number"
echo -e "  ${CYAN}3.${NC} Choose audience & template"
echo -e "  ${CYAN}4.${NC} Click Generate RCA"
echo ""
echo -e "  ${BOLD}Troubleshooting:${NC}"
echo -e "  • Backend log:  ${CYAN}$SUPPORT_DIR/server.log${NC}"
echo -e "  • Restart server: ${CYAN}launchctl kickstart -k gui/\$(id -u)/com.rca.backend${NC}"
echo -e "  • Settings → Check Connection in extension"
echo ""
hr
