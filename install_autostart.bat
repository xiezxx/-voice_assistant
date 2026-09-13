@echo off
chcp 65001 >nul
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
copy /y "%~dp0start_pet.bat" "%STARTUP%\小音宠物.bat" >nul
if exist "%STARTUP%\小音宠物.bat" (
    echo [小音] 已设置开机自启动（登录 Windows 后自动启动服务器 + 宠物）
    echo        想取消时运行 uninstall_autostart.bat
) else (
    echo [小音] 设置失败，请检查权限
)
pause
