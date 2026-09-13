@echo off
chcp 65001 >nul
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
del /q "%STARTUP%\小音宠物.bat" >nul 2>&1
echo [小音] 已取消开机自启动
pause
