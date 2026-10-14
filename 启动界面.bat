@echo off
rem QQ group lottery - launch the graphical interface
rem Note: this file is intentionally ASCII-only so cmd.exe parses it on any codepage.
chcp 65001 >nul
setlocal
set "HERE=%~dp0"

set "PY="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )

if not defined PY (
  echo [ERROR] Python 3.8+ not found. Please install Python, then run: python app.py
  pause
  exit /b 1
)

%PY% "%HERE%app.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo [exited with code %CODE%]
  pause
)
exit /b %CODE%
