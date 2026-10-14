@echo off
rem QQ group lottery launcher (kept ASCII-only so cmd.exe parses it on any codepage)
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

if exist "%HERE%.venv\Scripts\python.exe" (
  "%HERE%.venv\Scripts\python.exe" "%HERE%run_lottery.py" %*
  exit /b %ERRORLEVEL%
)

if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" "%HERE%run_lottery.py" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python "%HERE%run_lottery.py" %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%HERE%run_lottery.py" %*
  exit /b %ERRORLEVEL%
)

echo [ERROR] Python 3.8+ not found. Please install Python or run with a full python path.
exit /b 1
