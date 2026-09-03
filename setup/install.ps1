# ─────────────────────────────────────────────────────────────────────────────
#  RCA Analysis — Windows Installer
#  Run from PowerShell:  .\setup\install.ps1
# ─────────────────────────────────────────────────────────────────────────────
#Requires -Version 5.1

$ErrorActionPreference = 'Stop'

function ok($msg)   { Write-Host "  [OK]  $msg" -ForegroundColor Green }
function info($msg) { Write-Host "  [>>]  $msg" -ForegroundColor Cyan }
function warn($msg) { Write-Host "  [!!]  $msg" -ForegroundColor Yellow }
function fail($msg) { Write-Host "  [XX]  $msg" -ForegroundColor Red }
function step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Blue }
function hr()       { Write-Host ("─" * 60) -ForegroundColor Blue }

Clear-Host
hr
Write-Host "   RCA Analysis — Windows Installer" -ForegroundColor White
Write-Host "   Salesforce Support Intelligence Extension"
hr

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir  = Split-Path -Parent $ScriptDir
$ExtDir      = Join-Path $ProjectDir "rca-extension"
$ServerSrc   = Join-Path $ProjectDir "rca-proxy\server.py"
$AppData     = $env:APPDATA
$SupportDir  = Join-Path $AppData "rca-backend"
$TaskName    = "RCA-Backend-Server"

# ── STEP 1: Python ────────────────────────────────────────────────────────────
step "STEP 1 — Python 3"

$PythonBin = $null
foreach ($p in @("python", "python3", "py")) {
    try {
        $ver = & $p --version 2>&1
        if ($ver -match "Python 3") {
            $PythonBin = (Get-Command $p -ErrorAction SilentlyContinue).Source
            ok "Python found: $PythonBin ($ver)"
            break
        }
    } catch {}
}

if (-not $PythonBin) {
    info "Python 3 not found. Attempting install via winget..."
    try {
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
        $PythonBin = (Get-Command python -ErrorAction SilentlyContinue).Source
        if ($PythonBin) { ok "Python installed: $PythonBin" }
        else { throw "still not found" }
    } catch {
        fail "Could not install Python automatically."
        Write-Host "  Install manually from https://www.python.org/downloads/" -ForegroundColor Yellow
        Write-Host "  Check 'Add Python to PATH' during install, then re-run this script."
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# pdfplumber
info "Installing pdfplumber..."
& $PythonBin -m pip install pdfplumber --quiet
ok "pdfplumber ready"

# ── STEP 2: Node.js ───────────────────────────────────────────────────────────
step "STEP 2 — Node.js"

$NodeBin = (Get-Command node -ErrorAction SilentlyContinue)
if ($NodeBin) {
    ok "Node.js found: $($NodeBin.Source) ($(node --version))"
} else {
    info "Node.js not found. Installing via winget..."
    try {
        winget install --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
        ok "Node.js installed"
    } catch {
        fail "Could not install Node.js automatically."
        Write-Host "  Install from https://nodejs.org/ then re-run this script." -ForegroundColor Yellow
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# ── STEP 3: Claude Code CLI ───────────────────────────────────────────────────
step "STEP 3 — Claude Code CLI"

$ClaudeBin = $null
foreach ($p in @("claude", "claude.cmd")) {
    $found = (Get-Command $p -ErrorAction SilentlyContinue)
    if ($found) { $ClaudeBin = $found.Source; break }
}

if ($ClaudeBin) {
    ok "Claude CLI found: $ClaudeBin"
} else {
    info "Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
    foreach ($p in @("claude", "claude.cmd")) {
        $found = (Get-Command $p -ErrorAction SilentlyContinue)
        if ($found) { $ClaudeBin = $found.Source; break }
    }
    if (-not $ClaudeBin) {
        fail "Claude CLI install failed. Run manually: npm install -g @anthropic-ai/claude-code"
        Read-Host "Press Enter to exit"; exit 1
    }
    ok "Claude CLI installed: $ClaudeBin"
}

# ── STEP 4: Copy server ───────────────────────────────────────────────────────
step "STEP 4 — Installing backend server"

New-Item -ItemType Directory -Force -Path $SupportDir | Out-Null
Copy-Item $ServerSrc "$SupportDir\server.py" -Force
ok "Server copied to $SupportDir"

# ── STEP 5: Task Scheduler (auto-start at login) ──────────────────────────────
step "STEP 5 — Setting up auto-start (Task Scheduler)"

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    info "Removing existing scheduled task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$Action  = New-ScheduledTaskAction -Execute $PythonBin -Argument "`"$SupportDir\server.py`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "RCA Analysis backend server" | Out-Null

ok "Scheduled task '$TaskName' registered — starts at every login"

# ── STEP 6: Start server now ──────────────────────────────────────────────────
step "STEP 6 — Starting backend server"

# Kill any existing instance on port 3001
$existing = Get-NetTCPConnection -LocalPort 3001 -ErrorAction SilentlyContinue
if ($existing) {
    $pid = $existing.OwningProcess
    Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
    info "Stopped existing process on port 3001"
}

Start-Process -FilePath $PythonBin -ArgumentList "`"$SupportDir\server.py`"" -WindowStyle Hidden
info "Server started in background..."

# Wait for server
$started = $false
for ($i = 1; $i -le 15; $i++) {
    Start-Sleep 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:3001/health" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        if ($r.Content -match '"ok"') { $started = $true; break }
    } catch {}
    Write-Host "  Waiting... ($i/15)" -NoNewline
    Write-Host "`r" -NoNewline
}

if ($started) { ok "Backend server running on http://127.0.0.1:3001" }
else { warn "Server did not respond in 15s — check log: $SupportDir\server.log" }

# ── STEP 7: Load Chrome Extension ────────────────────────────────────────────
step "STEP 7 — Load Chrome Extension (manual)"

Write-Host ""
Write-Host "  Open Google Chrome and follow these steps:" -ForegroundColor White
Write-Host ""
Write-Host "  1. Go to:    chrome://extensions" -ForegroundColor Cyan
Write-Host "  2. Turn ON   Developer mode  (toggle, top-right)" -ForegroundColor Cyan
Write-Host "  3. Click     Load unpacked" -ForegroundColor Cyan
Write-Host "  4. Select:   $ExtDir" -ForegroundColor Cyan
Write-Host "  5. The RCA Analysis extension will appear in toolbar" -ForegroundColor Cyan
Write-Host ""

$chrome = @(
    "$env:PROGRAMFILES\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($chrome) {
    Start-Process $chrome "chrome://extensions"
    info "Opened Chrome extensions page"
} else {
    warn "Chrome not found — open it manually and go to chrome://extensions"
}

# ── Done ──────────────────────────────────────────────────────────────────────
hr
Write-Host "   Installation complete!" -ForegroundColor Green
hr
Write-Host ""
Write-Host "  How to use:" -ForegroundColor White
Write-Host "  • Click the RCA Analysis icon in Chrome toolbar"
Write-Host "  • Enter a Salesforce case number"
Write-Host "  • Click Generate RCA — wait ~3-5 minutes"
Write-Host ""
Write-Host "  Troubleshooting:" -ForegroundColor White
Write-Host "  • Log:      $SupportDir\server.log"
Write-Host "  • Restart:  Stop-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName"
Write-Host "  • Re-auth:  Open Terminal -> claude -> /mcp"
Write-Host ""
hr
Read-Host "Press Enter to close"
