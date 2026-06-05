@echo off
setlocal

set APP_URL=http://localhost:8000
cd /d "%~dp0..\.."

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker is not installed.
  echo Please install Docker Desktop, then run this script again.
  pause
  exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
  where docker-compose >nul 2>nul
  if errorlevel 1 (
    echo Docker Compose is not available.
    echo Please install or update Docker Desktop, then run this script again.
    pause
    exit /b 1
  ) else (
    set COMPOSE=docker-compose
  )
) else (
  set COMPOSE=docker compose
)

docker info >nul 2>nul
if errorlevel 1 (
  echo Docker is installed but not running.
  echo Please open Docker Desktop, wait until it finishes starting, then run this script again.
  pause
  exit /b 1
)

if not exist data\outputs mkdir data\outputs
if not exist data\reference_data mkdir data\reference_data
if not exist submissions\incoming mkdir submissions\incoming
if not exist submissions\extracted mkdir submissions\extracted
if not exist submissions\validated mkdir submissions\validated

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo Port 8000 is already being used by another app.
  echo Please stop the other app, then run this script again.
  pause
  exit /b 1
)

echo Starting OSIPI Pipeline local app...
%COMPOSE% up --build -d
if errorlevel 1 (
  echo Docker could not start the app.
  pause
  exit /b 1
)

echo Waiting for the app to respond...
set /a COUNT=0
:waitloop
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing '%APP_URL%/api/health' | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto ready
set /a COUNT+=1
if %COUNT% GTR 60 (
  echo The app did not start in time.
  echo Try running: docker compose logs
  pause
  exit /b 1
)
timeout /t 2 >nul
goto waitloop

:ready
echo OSIPI Pipeline is running:
echo %APP_URL%
start "" "%APP_URL%"
pause
