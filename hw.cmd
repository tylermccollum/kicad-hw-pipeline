@echo off
rem hw: project tooling (Windows cmd/PowerShell). See `hw help`.
rem Uses the py launcher from python.org installers, else python on PATH.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0site\hw.py" %*
) else (
  python "%~dp0site\hw.py" %*
)
exit /b %errorlevel%
