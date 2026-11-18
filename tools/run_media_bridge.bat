@echo off
REM Khoi chay Windows PC Media Bridge gui bai nhac dang nghe ve OpenStick LAN Dashboard.
REM Dat OPENSTICK_URL / DASHBOARD_PIN trong moi truong hoac file .env.
if "%OPENSTICK_URL%"=="" set "OPENSTICK_URL=http://192.168.1.100:8080"
if "%DASHBOARD_PIN%"=="" if exist "%~dp0..\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
        if /i "%%A"=="DASHBOARD_PIN" set "DASHBOARD_PIN=%%B"
    )
)
if "%DASHBOARD_PIN%"=="" (
    echo [LOI] Chua cau hinh DASHBOARD_PIN trong moi truong hoac .env.
    exit /b 1
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0pc_media_bridge.py" --interval 8 %*
    goto :eof
)
python "%~dp0pc_media_bridge.py" --interval 8 %*
