@echo off
setlocal
rem fry launcher shim for Windows (cmd / PowerShell).
rem Put this file on your PATH. It runs fry.py via python.
if defined FRY_PY (
  set "_FRY=%FRY_PY%"
) else (
  set "_FRY=%~dp0fry.py"
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "%_FRY%" %*
) else (
  py -3 "%_FRY%" %*
)
