# QQ group lottery - PowerShell launcher
#
# Usage (run from the project folder). This file's name is Chinese: the three
# characters mean "lottery". Example:
#   .\<this file> draw -n 3
#   .\<this file> stats
#   .\<this file> draw --source onebot --group 123456789 -n 1 --post
#
# It is just a thin wrapper around run_lottery.py, same as lottery.bat.
#
# If PowerShell refuses to run it, execute this once in the same window:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 decodes
# .ps1 files with the ANSI codepage (GBK on Chinese Windows), which corrupts
# non-ASCII string literals and breaks parsing. File NAMES may be Chinese, which
# is fine - cmd.exe and PowerShell both handle that.

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$candidates = @(
    (Join-Path $here '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
)

$python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if (-not $python) {
    throw 'Python 3.8+ not found. Install Python, or use lottery.bat instead.'
}

& $python (Join-Path $here 'run_lottery.py') @Args
exit $LASTEXITCODE
