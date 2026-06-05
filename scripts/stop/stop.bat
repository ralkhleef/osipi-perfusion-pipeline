@echo off
setlocal

cd /d "%~dp0..\.."

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker is not installed.
  pause
  exit /b 1
)

docker compose version >nul 2>nul
if errorlevel 1 (
  where docker-compose >nul 2>nul
  if errorlevel 1 (
    echo Docker Compose is not available.
    pause
    exit /b 1
  ) else (
    docker-compose down
  )
) else (
  docker compose down
)

echo OSIPI Pipeline local app stopped.
pause
