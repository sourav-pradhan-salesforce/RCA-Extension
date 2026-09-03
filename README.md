# RCA Analysis — Salesforce Intelligence

Chrome Extension + local backend that generates Root Cause Analysis documents from Salesforce case data (OrgCS, Org62, GUS, Slack).

---

## What it does

- Pulls case data from OrgCS, Org62, GUS work items, and Slack SEV channels
- Generates a structured RCA document using Claude + MCP tools
- Lets you switch timezones on all timestamps with one click
- Export to Google Doc or PDF
- Upload a PDF/TXT template to control output format

---

## Requirements

- macOS **or** Windows 10/11
- [Claude Code](https://claude.ai/code) installed and logged in (`claude` CLI in PATH)
- Google Chrome
- MCP plugins connected in Claude Code: `orgcs`, `Org62-Sobject-Read`, `slack`, `dxmcp-gus`, `google-workspace`

---

## Quick Install

### macOS — double-click installer

```bash
# Right-click → Open if macOS blocks it
open START-HERE.command
```

Or run manually:

```bash
bash setup/install.sh
```

### Windows — double-click installer

Double-click **`START-HERE.bat`**  
(or run in PowerShell: `.\setup\install.ps1`)

Requires PowerShell 5.1+ (built into Windows 10/11).  
The installer uses `winget` to install Python and Node.js if missing.

---

## Manual Installation

### macOS

**Step 1 — Copy backend**

```bash
mkdir -p ~/Library/Application\ Support/rca-backend
cp rca-proxy/server.py ~/Library/Application\ Support/rca-backend/server.py
pip3 install pdfplumber
```

**Step 2 — LaunchAgent (auto-start)**

```bash
# Edit setup/com.rca.backend.plist — replace python path and HOME if needed
cp setup/com.rca.backend.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rca.backend.plist
```

Verify: `curl http://127.0.0.1:3001/health` → `{"status": "ok"}`

**Step 3 — Restart after updates**

```bash
cp rca-proxy/server.py ~/Library/Application\ Support/rca-backend/server.py
launchctl kickstart -k "gui/$(id -u)/com.rca.backend"
tail -f ~/Library/Application\ Support/rca-backend/server.log
```

### Windows

**Step 1 — Copy backend**

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\rca-backend"
Copy-Item rca-proxy\server.py "$env:APPDATA\rca-backend\server.py"
python -m pip install pdfplumber
```

**Step 2 — Task Scheduler (auto-start)**

```powershell
$action  = New-ScheduledTaskAction -Execute python -Argument "$env:APPDATA\rca-backend\server.py"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "RCA-Backend-Server" -Action $action -Trigger $trigger
```

**Step 3 — Restart after updates**

```powershell
Copy-Item rca-proxy\server.py "$env:APPDATA\rca-backend\server.py"
Stop-ScheduledTask  -TaskName "RCA-Backend-Server"
Start-ScheduledTask -TaskName "RCA-Backend-Server"
Get-Content "$env:APPDATA\rca-backend\server.log" -Wait
```

### Load the Chrome extension (both platforms)

1. Go to `chrome://extensions`
2. Enable **Developer mode** (toggle top-right)
3. Click **Load unpacked**
4. Select the `rca-extension/` folder from this repo

### Allow MCP tools in Claude Code

Add to `~/.claude/settings.local.json` under `permissions.allow`:

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
3. Optionally upload a PDF/TXT output template
4. Click **Generate RCA**
5. Wait ~2–5 minutes — RCA opens in a new tab with timezone switcher, edit mode, PDF export, Google Doc export

---

## Project structure

```
rca-extension/     Chrome extension (load unpacked from here)
rca-proxy/         Backend server source (cross-platform Python)
setup/             install.sh (macOS), install.ps1 (Windows)
START-HERE.command macOS double-click installer
START-HERE.bat     Windows double-click installer
```
