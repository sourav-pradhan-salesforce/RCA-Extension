#!/bin/bash
# ─────────────────────────────────────────────────────────
#  RCA Analysis — Double-click to install everything
#  Right-click → Open if macOS blocks it
# ─────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()    { echo -e "  ${GREEN}✓${NC}  $1"; }
info()  { echo -e "  ${CYAN}→${NC}  $1"; }
warn()  { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail()  { echo -e "  ${RED}✗${NC}  $1"; }
step()  { echo -e "\n${BOLD}${BLUE}━━━  $1  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }
hr()    { echo -e "${BLUE}────────────────────────────────────────────────────────${NC}"; }
pause() { echo -e "\n  ${BOLD}Press ENTER to continue…${NC}"; read -r; }

clear
hr
echo -e "${BOLD}   RCA Analysis — Installer${NC}"
echo -e "   Salesforce Support Intelligence"
hr
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_HOME="$HOME"

# ── STEP 1: Homebrew ─────────────────────────────────────
step "STEP 1 — Homebrew"
if command -v brew &>/dev/null; then
    ok "Homebrew already installed"
else
    info "Installing Homebrew (~2 min)…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    [ -f "/opt/homebrew/bin/brew" ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    ok "Homebrew installed"
fi

# ── STEP 2: Node.js ──────────────────────────────────────
step "STEP 2 — Node.js"
if command -v node &>/dev/null; then
    ok "Node.js already installed ($(node --version))"
else
    info "Installing Node.js…"
    brew install node
    ok "Node.js installed"
fi

# ── STEP 3: Claude CLI ───────────────────────────────────
step "STEP 3 — Claude Code CLI"
CLAUDE_BIN=""
for p in "$USER_HOME/.local/bin/claude" "/opt/homebrew/bin/claude" "/usr/local/bin/claude" "$(command -v claude 2>/dev/null)"; do
    [ -n "$p" ] && [ -f "$p" ] && { CLAUDE_BIN="$p"; break; }
done

if [ -n "$CLAUDE_BIN" ]; then
    ok "Claude CLI found: $CLAUDE_BIN"
else
    info "Installing Claude Code CLI…"
    npm install -g @anthropic-ai/claude-code 2>&1 | tail -3
    for p in "$USER_HOME/.local/bin/claude" "/opt/homebrew/bin/claude" "/usr/local/bin/claude" "$(command -v claude 2>/dev/null)"; do
        [ -n "$p" ] && [ -f "$p" ] && { CLAUDE_BIN="$p"; break; }
    done
    if [ -z "$CLAUDE_BIN" ]; then
        fail "Claude CLI install failed. Run manually: npm install -g @anthropic-ai/claude-code"
        pause; exit 1
    fi
    ok "Claude CLI installed: $CLAUDE_BIN"
fi

# ── STEP 4: Claude Login ─────────────────────────────────
step "STEP 4 — Claude Login"
echo ""
echo -e "  You need a ${BOLD}paid Anthropic account${NC} (claude.ai)."
echo -e "  A browser will open — sign in with your account."
echo ""
info "Opening Claude login…"
"$CLAUDE_BIN" login
echo ""
ok "Claude login complete"

# ── STEP 5: Write MCP server definitions ─────────────────
step "STEP 5 — Configuring MCP servers"
info "Writing Salesforce & Slack MCP definitions to ~/.claude.json…"

python3 << PYEOF
import json, os, sys

home = os.path.expanduser('~')
claude_json = os.path.join(home, '.claude.json')

# Load existing or start fresh
try:
    with open(claude_json) as f:
        d = json.load(f)
except Exception:
    d = {}

# Ensure top-level structure exists
d.setdefault('mcpServers', {})
d.setdefault('projects', {})

# ── Global MCP servers (Org62, OrgCS, GUS) ───────────────
d['mcpServers']['Org62-Sobject-Read'] = {
    "type": "http",
    "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads",
    "oauth": {
        "clientId": "3MVG9WQsPp5nH_EpM_KnrLdttExiAuzLoaVZkfx52M1ORCimCnoSOKvZzy2bABbcT0dhhi80GJKgFbKkP4Rhf",
        "callbackPort": 8082
    }
}
d['mcpServers']['orgcs'] = {
    "type": "http",
    "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads",
    "oauth": {
        "clientId": "3MVG99OxTyEMCQ3jxnBfUPAOvDMzVueXotu7fDUW5yJlv6_X9xUNZTwqonKeJDMM_AiFpWAoKGpAq7yY67zYz",
        "callbackPort": 8082
    }
}
d['mcpServers']['gus'] = {
    "type": "http",
    "url": "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads"
}

# ── Global Slack MCP (not project-scoped so any working dir can use it) ──
d['mcpServers']['slack'] = {
    "type": "http",
    "url": "https://mcp.slack.com/mcp",
    "oauth": {
        "clientId": "188160004832.9210129962818",
        "callbackPort": 3118
    }
}

with open(claude_json, 'w') as f:
    json.dump(d, f, indent=2)

print("  MCP definitions written successfully")
PYEOF

ok "MCP server definitions configured"

# ── STEP 6: Authenticate each MCP ────────────────────────
step "STEP 6 — Authenticate MCP servers (browser logins)"
echo ""
echo -e "  ${BOLD}You now need to log in to each Salesforce tool.${NC}"
echo -e "  This will open Claude Code. Inside it:"
echo ""
echo -e "  1. Type ${CYAN}/mcp${NC} and press Enter"
echo -e "  2. You will see 4 servers listed:"
echo ""
echo -e "     ${CYAN}• orgcs${NC}            → log in with OrgCS Salesforce"
echo -e "     ${CYAN}• Org62-Sobject-Read${NC} → log in with Org62 Salesforce"
echo -e "     ${CYAN}• gus${NC}              → log in with GUS"
echo -e "     ${CYAN}• slack${NC}            → log in with Salesforce Enterprise Slack"
echo ""
echo -e "  3. Click each one to authenticate (browser opens for each)"
echo -e "  4. When all 4 show ${GREEN}connected${NC}, type ${CYAN}/exit${NC} and press Enter"
echo ""
echo -e "  ${YELLOW}⚠  You MUST have internal Salesforce access for these to work.${NC}"
echo ""
pause
info "Opening Claude Code…"
cd "$SCRIPT_DIR" && "$CLAUDE_BIN"
echo ""
ok "MCP authentication step complete"

# ── STEP 7: Install backend ───────────────────────────────
step "STEP 7 — Installing backend server"
bash "$SCRIPT_DIR/setup/install.sh"

# ── STEP 8: Load Chrome extension ────────────────────────
step "STEP 8 — Load Chrome Extension"
echo ""
echo -e "  Chrome will open. Do these 4 steps:"
echo ""
echo -e "  ${CYAN}1.${NC} Turn ON ${BOLD}Developer mode${NC} (toggle, top-right corner)"
echo -e "  ${CYAN}2.${NC} Click ${BOLD}Load unpacked${NC}"
echo -e "  ${CYAN}3.${NC} In the folder picker, open:"
echo -e "     ${BOLD}$SCRIPT_DIR${NC}"
echo -e "  ${CYAN}4.${NC} Select the ${BOLD}rca-extension${NC} folder inside it"
echo ""
pause
open -a "Google Chrome" "chrome://extensions" 2>/dev/null || \
    warn "Chrome not found — open it manually and go to chrome://extensions"

# ── Done ─────────────────────────────────────────────────
echo ""
hr
echo -e "${BOLD}${GREEN}   RCA Analysis is installed and ready!${NC}"
hr
echo ""
echo -e "  ${BOLD}How to use:${NC}"
echo -e "  • Click the RCA Analysis icon in Chrome toolbar"
echo -e "  • Enter a Salesforce case number"
echo -e "  • Choose audience (Leadership / Customer / CIC)"
echo -e "  • Click Generate RCA — wait ~3-5 minutes"
echo -e "  • Review, edit, or download as PDF"
echo ""
echo -e "  ${BOLD}If something breaks:${NC}"
echo -e "  • Check log:    ${CYAN}~/Library/Application Support/rca-backend/server.log${NC}"
echo -e "  • Restart:      ${CYAN}launchctl kickstart -k gui/\$(id -u)/com.rca.backend${NC}"
echo -e "  • Re-auth MCPs: open Terminal → ${CYAN}claude${NC} → type ${CYAN}/mcp${NC}"
echo ""
hr
echo ""
echo "  Press ENTER to close…"
read -r
