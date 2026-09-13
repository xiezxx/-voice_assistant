' 隐藏窗口启动语音服务器（无任务栏图标），日志写入 app_run.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
ws.Run "cmd /c python app.py >> app_run.log 2>&1", 0, False
