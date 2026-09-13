' 隐藏窗口启动桌面宠物（python.exe，无可见控制台），日志写入 pet_run.log
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
ws.Run "cmd /c python pet.py >> pet_run.log 2>&1", 0, False
