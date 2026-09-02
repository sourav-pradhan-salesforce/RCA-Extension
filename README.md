# RCA Analysis — Salesforce Intelligence

Chrome Extension + local backend that generates Root Cause Analysis documents from Salesforce case data (OrgCS, Org62, GUS, Slack).

---

## What it does

- Pulls case data from OrgCS, Org62, GUS work items, and Slack SEV channels
- Generates a structured RCA document using Claude + MCP tools
- Lets you switch timezones on all timestamps with one click
- Export to Google Doc or PDF

---

## Requirements

- macOS
- [Claude Code](https://claude.ai/code) installed and logged in (`claude` CLI available in PATH)
- Chrome browser
- MCP plugins connected in Claude Code: `orgcs`, `Org62-Sobject-Read`, `slack`, `dxmcp-gus`, `google-workspace`

---

## Installation

### Step 1 — Copy backend to support directory

```bash
mkdir -p ~/Library/Application\ Support/rca-backend
cp rca-proxy/server.py ~/Library/Application\ Support/rca-backend/server.py
```

### Step 2 — Install the LaunchAgent (auto-start on login)

```bash
cp setup/com.rca.backend.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rca.backend.plist
launchctl start com.rca.backend
```

Verify it's running:

```bash
curl http://127.0.0.1:3001/health
# Expected: {"status": "ok"}
```

### Step 3 — Load the Chrome extension

1. Open Chrome and go to `chrome://extensions`
2. Enable **Developer mode** (toggle top-right)
3. Click **Load unpacked**
4. Select the `rca-extension/` folder from this repo

The RCA Analysis icon will appear in your Chrome toolbar.

### Step 4 — Allow MCP tools in Claude Code

Add these to `~/.claude/settings.local.json` under `permissions.allow`:

```json
"mcp__orgcs__soqlQuery",
"mcp__orgcs__find",
"mcp__orgcs__getRelatedRecords",
"mcp__plugin_dxmcp-gus_dxmcp-gus__query_gus_records",
"mcp__plugin_slack_slack__slack_search_public_and_private",
"mcp__plugin_slack_slack__slack_read_thread",
"mcp__plugin_slack_slack__slack_read_channel",
"mcp__plugin_google-workspace_google-workspace__create_doc",
"mcp__plugin_google-workspace_google-workspace__import_to_google_doc",
"mcp__plugin_google-workspace_google-workspace__batch_update_doc"
```

---

## Usage

1. Click the **RCA Analysis** icon in Chrome toolbar
2. Enter a Salesforce case number (e.g. `00123456`)
3. Click **Generate RCA**
4. Wait ~2 minutes while it fetches data from OrgCS, Org62, GUS, and Slack
5. RCA opens in a new tab with timezone switcher, edit mode, PDF export, and Google Doc export

---

## Managing the backend

```bash
# Restart
launchctl kickstart -k "gui/$(id -u)/com.rca.backend"

# View logs
tail -f ~/Library/Application\ Support/rca-backend/server.log

# Stop
launchctl stop com.rca.backend
```

After updating `server.py`, copy it to the support directory and restart:

```bash
cp rca-proxy/server.py ~/Library/Application\ Support/rca-backend/server.py
launchctl kickstart -k "gui/$(id -u)/com.rca.backend"
```

---

## Project structure

```
rca-extension/     Chrome extension (load unpacked from here)
rca-proxy/         Backend server source
setup/             LaunchAgent plist for auto-start
```
