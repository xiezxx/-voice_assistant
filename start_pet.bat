@echo off
cd /d "%~dp0"
echo [小音] 检查服务器...
netstat -ano | findstr :7860 | findstr LISTENING >nul 2>&1
if errorlevel 1 (
    echo [小音] 启动语音服务器（后台无窗口，日志 app_run.log）...
    cscript //nologo "%~dp0start_hidden.vbs"
    timeout /t 28 /nobreak >nul
) else (
    echo [小音] 服务器已在运行
)
echo [小音] 启动桌面宠物...
cscript //nologo "%~dp0start_hidden_pet.vbs"
exit
