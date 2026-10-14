<#
.SYNOPSIS
    NapCat connectivity check for the QQ group lottery tool.

.DESCRIPTION
    Run this AFTER installing NapCat and logging in a QQ account. It will:
      1. locate the NapCat install and read its version
      2. read webui.json and onebot11_<QQ>.json (WebUI port, token, HTTP server state)
      3. probe the WebUI port and OneBot HTTP ports
      4. really call get_login_info and get_group_msg_history
      5. print exactly what to type into the lottery GUI

    Read-only: it never modifies NapCat configuration.

    NOTE: this file is intentionally ASCII-only, because Windows PowerShell 5.1
    decodes .ps1 files using the ANSI codepage (GBK on Chinese Windows), which
    corrupts non-ASCII string literals. See docs/NapCat-guide.md for the
    Chinese walkthrough.

.PARAMETER Port
    OneBot HTTP port. Auto-detected from config when omitted.

.PARAMETER Token
    OneBot access token. Auto-detected from config when omitted.

.PARAMETER Group
    Group number to test get_group_msg_history against.

.PARAMETER Root
    NapCat install directory. Auto-searched when omitted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\napcat_check.ps1 -Group 123456789
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$Token = '',
    [string]$Group = '',
    [string]$Root = ''
)

$ErrorActionPreference = 'Continue'
$script:ok = 0
$script:warn = 0
$script:bad = 0

function Head($text) { Write-Host ''; Write-Host "== $text ==" -ForegroundColor Cyan }
function Good($text) { $script:ok++; Write-Host "  [OK]   $text" -ForegroundColor Green }
function Warn($text) { $script:warn++; Write-Host "  [WARN] $text" -ForegroundColor Yellow }
function Bad($text) { $script:bad++; Write-Host "  [FAIL] $text" -ForegroundColor Red }
function Info($text) { Write-Host "  .      $text" -ForegroundColor DarkGray }

function Test-Port($p) {
    if (-not $p) { return $false }
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', [int]$p, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(1200)
        if ($ok) { $client.EndConnect($iar) }
        $client.Close()
        return $ok
    } catch { return $false }
}

function Invoke-OneBot($action, $body, $port, $token) {
    $uri = "http://127.0.0.1:$port/$action"
    $headers = @{ 'Content-Type' = 'application/json' }
    if ($token) { $headers['Authorization'] = "Bearer $token" }
    $json = $body | ConvertTo-Json -Compress -Depth 5
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Post -Headers $headers `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 12
        return @{ ok = $true; data = $resp }
    } catch {
        return @{ ok = $false; error = $_.Exception.Message }
    }
}

function Format-UnixTime($seconds) {
    try { return ([DateTimeOffset]::FromUnixTimeSeconds([int64]$seconds)).LocalDateTime.ToString('yyyy-MM-dd HH:mm') }
    catch { return "?" }
}

Write-Host ''
Write-Host '  NapCat connectivity check - QQ group lottery'
Write-Host ('  ' + ('-' * 52))

# ------------------------------------------------------------------ 1
Head '1. Locate NapCat'

$candidates = @()
if ($Root) { $candidates += $Root }
$candidates += @(
    'D:\NapCat', 'E:\NapCat', 'C:\NapCat',
    "$env:USERPROFILE\NapCat", "$env:USERPROFILE\Desktop\NapCat",
    'D:\QQ\NapCat', 'D:\Tools\NapCat', 'E:\Tools\NapCat'
)
foreach ($drive in @('C:', 'D:', 'E:')) {
    foreach ($sub in @('', 'Tools', 'Program Files', 'Desktop', 'Downloads')) {
        $probe = if ($sub) { Join-Path "$drive\" $sub } else { "$drive\" }
        if (Test-Path $probe) {
            $candidates += (Get-ChildItem $probe -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^NapCat' } | Select-Object -ExpandProperty FullName)
        }
    }
}

$napcatDirs = @()
foreach ($c in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path $c)) { continue }
    $marks = @('launcher.bat', 'napcat.bat', 'launcher-win10.bat', 'NapCatInstaller.exe', 'package.json')
    if ($marks | Where-Object { Test-Path (Join-Path $c $_) }) { $napcatDirs += $c }
}

if ($napcatDirs) {
    foreach ($d in $napcatDirs) {
        Good "NapCat directory: $d"
        $verFile = Join-Path $d 'package.json'
        if (Test-Path $verFile) {
            try {
                $pkg = Get-Content $verFile -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($pkg.version) { Info "version: $($pkg.version)" }
            } catch { }
        }
        Get-ChildItem $d -Filter 'launcher*.bat' -ErrorAction SilentlyContinue |
            ForEach-Object { Info "launcher: $($_.Name)" }
        Get-ChildItem $d -Filter 'napcat*.bat' -ErrorAction SilentlyContinue |
            ForEach-Object { Info "launcher: $($_.Name)" }
    }
} else {
    Bad 'NapCat directory not found'
    Info 'Install it first (see docs\NapCat-guide.md), then run this script again'
}

# ------------------------------------------------------------------ 2
Head '2. Read configuration'

$configDirs = @()
foreach ($d in $napcatDirs) {
    $configDirs += (Join-Path $d 'config')
    $configDirs += (Join-Path $d 'resources\app\app_launcher\napcat\config')
    # OneKey installs keep the real config inside versions\<ver>\resources\app\napcat\config
    Get-ChildItem (Join-Path $d 'versions') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $configDirs += (Join-Path $_.FullName 'resources\app\napcat\config')
    }
    Get-ChildItem $d -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^NapCat\..*\.Shell$' } | ForEach-Object {
            $configDirs += (Join-Path $_.FullName 'config')
            Get-ChildItem (Join-Path $_.FullName 'versions') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $configDirs += (Join-Path $_.FullName 'resources\app\napcat\config')
            }
        }
}
$configDirs = $configDirs | Select-Object -Unique | Where-Object { Test-Path $_ }

$webuiPort = 0
$webuiToken = ''
$httpPorts = @()
$httpTokens = @()

if (-not $configDirs) {
    Warn 'config directory not found (it is created on first NapCat launch)'
} else {
    foreach ($cd in $configDirs) {
        Info "config dir: $cd"

        $webuiFile = Join-Path $cd 'webui.json'
        if (Test-Path $webuiFile) {
            try {
                $webui = Get-Content $webuiFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $webuiPort = [int]$webui.port
                $webuiToken = [string]$webui.token
                Good "WebUI port = $webuiPort (token read from webui.json)"
                Info "WebUI URL: http://127.0.0.1:$webuiPort/webui?token=$webuiToken"
            } catch { Bad "webui.json parse error: $($_.Exception.Message)" }
        } else {
            Warn 'webui.json not found'
        }

        Get-ChildItem $cd -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue | ForEach-Object {
            $qq = $_.BaseName -replace '^onebot11_', ''
            try {
                $cfg = Get-Content $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $servers = @($cfg.network.httpServers)
                Info "onebot11_$qq.json : $($servers.Count) httpServers entry(ies)"
                foreach ($s in $servers) {
                    $state = if ($s.enable) { 'enabled' } else { 'DISABLED' }
                    $tk = if ([string]::IsNullOrEmpty($s.token)) { 'empty' } else { 'set' }
                    Info "  - name=$($s.name) port=$($s.port) $state token=$tk"
                    if ($s.enable) {
                        $httpPorts += [int]$s.port
                        $httpTokens += [string]$s.token
                    }
                }
                if (-not ($servers | Where-Object { $_.enable })) {
                    Bad "QQ $qq has NO enabled HTTP server - this is exactly why the lottery tool cannot connect"
                    Info 'Open the WebUI, go to network config, create an HTTP server and tick "enable on save"'
                }
            } catch {
                Bad "onebot11_$qq.json parse error: $($_.Exception.Message)"
            }
        }
    }
}

if (-not $Port -and $httpPorts) {
    $Port = $httpPorts[0]
    Info "using HTTP port from config: $Port"
}
if (-not $Token -and $httpTokens) {
    $Token = ($httpTokens | Where-Object { $_ } | Select-Object -First 1)
    if ($Token) { Info 'access token taken from config' }
}

# ------------------------------------------------------------------ 3
Head '3. Listening ports'

if ($webuiPort -and (Test-Port $webuiPort)) { Good "WebUI port $webuiPort is listening" }
elseif ($webuiPort) { Warn "WebUI port $webuiPort is NOT listening (is NapCat running?)" }

foreach ($p in (($httpPorts + @(3000, 3001)) | Where-Object { $_ } | Select-Object -Unique)) {
    if (Test-Port $p) { Good "port $p is listening" } else { Info "port $p not listening" }
}

# ------------------------------------------------------------------ 4
Head '4. Live OneBot API test'

$testPorts = @()
if ($Port) { $testPorts += $Port }
$testPorts += ($httpPorts + @(3000, 3001))
$testPorts = $testPorts | Where-Object { $_ } | Select-Object -Unique

$working = 0
foreach ($p in $testPorts) {
    if (-not (Test-Port $p)) { continue }
    $r = Invoke-OneBot 'get_login_info' @{} $p $Token
    if ($r.ok -and $r.data -and $r.data.status -ne 'failed') {
        $d = $r.data.data
        $nick = if ($d) { $d.nickname } else { '' }
        $uid = if ($d) { $d.user_id } else { '' }
        Good "port $p works - bot is $nick ($uid)"
        if (-not $working) { $working = $p }
    } else {
        Warn "port $p is listening but get_login_info did not answer correctly"
        if ($r.error) { Info "error: $($r.error)" }
        elseif ($r.data) { Info "reply: $($r.data | ConvertTo-Json -Compress -Depth 3)" }
    }
}

if (-not $working) {
    Bad 'no working OneBot HTTP endpoint found'
    if ($httpPorts.Count -eq 0) {
        Info 'Make sure NapCat is running, QQ is logged in, and an HTTP server is ENABLED'
    }
} else {
    if (-not $Group) {
        Warn 'no -Group given, skipping the group history test (the API the lottery relies on)'
        Info 'Run again with your group number, e.g.  -Group 123456789'
    } else {
        $r = Invoke-OneBot 'get_group_msg_history' @{ group_id = [int]$Group; count = 20 } $working $Token
        $msgs = $null
        if ($r.ok -and $r.data) { $msgs = $r.data.data.messages }
        if ($msgs -and $msgs.Count -gt 0) {
            Good "get_group_msg_history OK for group $Group : $($msgs.Count) messages"
            $newest = $msgs | Select-Object -First 1
            $oldest = $msgs | Select-Object -Last 1
            Info "newest: $(Format-UnixTime $newest.time) from $($newest.sender.nickname)"
            Info "oldest: $(Format-UnixTime $oldest.time)"
        } elseif ($r.ok -and $r.data -and $r.data.status -ne 'failed') {
            Warn "API works but group $Group returned no messages"
            Info 'Check the bot account really joined that group and the group has history'
        } else {
            Bad 'get_group_msg_history failed'
            if ($r.error) { Info "error: $($r.error)" }
            elseif ($r.data) { Info "reply: $($r.data | ConvertTo-Json -Compress -Depth 3)" }
        }
    }

    Head '5. Fill these into the lottery GUI'
    Write-Host "  source       : NapCat / OneBot"
    Write-Host "  group id     : $(if ($Group) { $Group } else { '<your group number>' })"
    Write-Host "  HTTP address : http://127.0.0.1:$working"
    Write-Host "  access_token : $(if ($Token) { $Token } else { '<leave empty>' })"
    Write-Host ''
    Write-Host '  Then click the connectivity test button in the GUI.'
}

Head 'Summary'
Write-Host "  passed $script:ok, warnings $script:warn, problems $script:bad"
if ($script:bad -eq 0) { Write-Host '  Looks good - go draw a winner.' }
else { Write-Host '  Fix the [FAIL] items above, then run this script again.' }
Write-Host ''
exit $(if ($script:bad -gt 0) { 1 } else { 0 })
