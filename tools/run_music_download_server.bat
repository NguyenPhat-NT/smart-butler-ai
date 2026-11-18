@echo off
REM Khoi chay PC LAN Music Download Bridge cho OpenStick (cong 8091)
REM Doc MUSIC_DL_API_KEY hoac TTS_API_KEY tu bien moi truong hoac file .env (khong hardcode key trong .bat).
set "MUSIC_DL_PORT=8091"
set "_ENV_DL_KEY="
set "_ENV_TTS_KEY="
if "%MUSIC_DL_API_KEY%"=="" if exist "%~dp0..\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0..\.env") do (
        if /i "%%A"=="MUSIC_DL_API_KEY" set "_ENV_DL_KEY=%%B"
        if /i "%%A"=="TTS_API_KEY" set "_ENV_TTS_KEY=%%B"
    )
)
if "%MUSIC_DL_API_KEY%"=="" if not "%_ENV_DL_KEY%"=="" set "MUSIC_DL_API_KEY=%_ENV_DL_KEY%"
if "%MUSIC_DL_API_KEY%"=="" if not "%_ENV_TTS_KEY%"=="" set "MUSIC_DL_API_KEY=%_ENV_TTS_KEY%"
if "%MUSIC_DL_API_KEY%"=="" (
    echo [CANH BAO] Chua dat MUSIC_DL_API_KEY / TTS_API_KEY trong moi truong hoac .env - server se chay khong auth!
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0music_download_server.py" --port %MUSIC_DL_PORT% %*
    goto :eof
)
python "%~dp0music_download_server.py" --port %MUSIC_DL_PORT% %*
