@echo off
REM Phat truc tiep toan bo am thanh Windows cua may PC nay ra Loa ESP32-C3 (WebSocket)
REM Khong can mo cong Firewall inbound; bam Ctrl+C hoac tat cua so de dung.
if "%OPENSTICK_URL%"=="" set "OPENSTICK_URL=http://192.168.1.100:8080"
if "%DASHBOARD_PIN%"=="" if exist "%~dp0..\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
        if /i "%%A"=="DASHBOARD_PIN" set "DASHBOARD_PIN=%%B"
    )
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0music_download_server.py" --push-live --openstick-url "%OPENSTICK_URL%" %*
    goto :eof
)
python "%~dp0music_download_server.py" --push-live --openstick-url "%OPENSTICK_URL%" %*
