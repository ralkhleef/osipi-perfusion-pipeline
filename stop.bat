@echo off
call "%~dp0scripts\stop\stop.bat" %*
exit /b %ERRORLEVEL%
