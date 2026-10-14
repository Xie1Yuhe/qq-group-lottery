@echo off
rem QQ group lottery - headless archive watcher (autostart).
rem
rem This keeps the message archive growing in the background, independent of the
rem GUI: it syncs new group messages into data\archive.db on a fixed interval.
rem
rem Keep this file ASCII-only: cmd.exe parses .bat files with the ANSI codepage,
rem so non-ASCII characters inside would be corrupted.
rem
rem Log:  data\archive-watch.log
rem Stop: close this window / kill the python process, or delete the startup shortcut.

chcp 65001 >nul
cd /d "%~dp0.."

set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )

if not defined PY (
  echo [ERROR] Python 3.8+ not found. Cannot start the archive watcher.
  pause
  exit /b 1
)

rem --silent : only write to data\archive-watch.log, keep the console quiet
%PY% "%~dp0..\run_lottery.py" watch --silent
