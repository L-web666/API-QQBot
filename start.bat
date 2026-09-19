@echo off
REM QQ AI Bot launcher for Windows - auto restart on exit/crash
cd /d "%~dp0"
:loop
echo ============================================
echo [%date% %time%] Starting QQ AI Bot ...
echo ============================================
python qqbot.py
echo.
echo [%date% %time%] Bot exited, restart in 3s (press Ctrl+C twice to stop)...
timeout /t 3 /nobreak >nul
goto loop
