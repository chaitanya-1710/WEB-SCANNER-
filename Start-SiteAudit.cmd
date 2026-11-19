@echo off
setlocal
pushd "%~dp0" || exit /b 1
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto use_python
echo SiteAudit needs Python 3.10 or newer.
echo Install Python for Windows from https://www.python.org/downloads/windows/
echo If using the classic installer, select "Add python.exe to PATH".
set "result=1"
goto finish

:use_py
py -3 -X utf8 "%~dp0site_audit.py" --demo
set "result=%errorlevel%"
goto finish

:use_python
python -X utf8 "%~dp0site_audit.py" --demo
set "result=%errorlevel%"

:finish
popd
if not "%result%"=="0" pause
exit /b %result%
