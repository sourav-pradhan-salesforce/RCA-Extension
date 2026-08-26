╔══════════════════════════════════════════════════════════════╗
║           RCA Analysis — Salesforce Intelligence             ║
║         Chrome Extension + Backend   |   v1.0               ║
╚══════════════════════════════════════════════════════════════╝

BEFORE YOU START — REQUIREMENTS
──────────────────────────────────────────────────────────────
  ✅  Mac (macOS only)
  ✅  Paid Anthropic account → claude.ai
  ✅  Salesforce internal access (OrgCS, Org62, GUS, Enterprise Slack)
  ✅  Google Chrome
  ✅  Internet connection


INSTALL — 2 steps only
──────────────────────────────────────────────────────────────

STEP 1 — Open Terminal
  Press Cmd + Space, type "Terminal", press Enter

STEP 2 — Paste this command and press Enter:

  bash ~/Desktop/Project-RCA-NEW/START-HERE.command

  (If you unzipped somewhere else, replace ~/Desktop/Project-RCA-NEW
   with the actual path to the Project-RCA-NEW folder)

The installer will guide you through everything on screen:
  → Installing Claude CLI
  → Logging in to your Anthropic account
  → Connecting Salesforce MCP servers (OrgCS, Org62, GUS, Slack)
  → Starting the backend
  → Opening Chrome to load the extension

Total time: ~15 minutes (first time only)


WHY NOT DOUBLE-CLICK?
──────────────────────────────────────────────────────────────
macOS blocks downloaded scripts with a security warning.
Running via Terminal bypasses this — it is completely safe.


HOW TO USE (after install)
──────────────────────────────────────────────────────────────
  1. Click the RCA Analysis icon in Chrome toolbar
  2. Enter a Salesforce case number (e.g. 00473996707)
  3. Select audience: Leadership / Customer / CIC
  4. Click Generate RCA
  5. Wait 3-5 minutes
  6. Review in the new tab — edit inline or Download as PDF


TROUBLESHOOTING
──────────────────────────────────────────────────────────────
  Problem                 Solution
  ─────────────────────────────────────────────────────────
  "Backend offline"       Open Terminal and run:
                          launchctl kickstart -k gui/$(id -u)/com.rca.backend

  "MCP not connecting"    Open Terminal → run: claude
                          Then type: /mcp
                          Re-authenticate any disconnected server

  Extension missing       Go to chrome://extensions
                          → Load unpacked → select rca-extension folder

  View server log         ~/Library/Application Support/rca-backend/server.log


UNINSTALL
──────────────────────────────────────────────────────────────
  launchctl unload ~/Library/LaunchAgents/com.rca.backend.plist
  rm ~/Library/LaunchAgents/com.rca.backend.plist
  rm -rf ~/Library/Application\ Support/rca-backend
  Then remove the extension from chrome://extensions

──────────────────────────────────────────────────────────────
Built with Claude Code + Salesforce MCP
──────────────────────────────────────────────────────────────
