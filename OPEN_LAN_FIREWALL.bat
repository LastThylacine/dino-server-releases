@echo off
net session >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
set "DINO_EXE=%~dp0DinoServer.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\configure_firewall.ps1" -ProgramPath "%DINO_EXE%"
if errorlevel 1 (
  echo.
  echo Failed to add the Windows Firewall rule.
  echo Use DinoServer.exe for normal automatic setup, or run this fallback as administrator.
  pause
  exit /b 1
)
echo.
echo Configured Dino Server for Private and Public networks, restricted to LocalSubnet.
echo TCP: 53, 80, 9933, 9943. UDP: 53.
pause
