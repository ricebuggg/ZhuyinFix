' ZhuyinFix 隱藏視窗啟動器（開機捷徑指到這個檔）
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
home = sh.ExpandEnvironmentStrings("%USERPROFILE%")
uv = ""
For Each c In Array(home & "\.local\bin\uv.exe", home & "\.aki\bin\uv.exe")
  If uv = "" And fso.FileExists(c) Then uv = c
Next
If uv = "" Then uv = "uv"
sh.CurrentDirectory = dir
sh.Run """" & uv & """ run --with pynput --with pystray --with pillow pythonw zhuyinfix.py", 0, False
