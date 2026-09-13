@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [小音] 检查服务器...
netstat -ano | findstr :7860 | findstr LISTENING >nul 2>&1
if errorlevel 1 (
    echo [小音] 启动语音服务器（最小化窗口，首次需约 30 秒加载模型）...
    start "小音服务器" /min python app.py
    timeout /t 28 /nobreak >nul
) else (
    echo [小音] 服务器已在运行
)
echo [小音] 启动桌面宠物...
start "" pythonw pet.py
exit
