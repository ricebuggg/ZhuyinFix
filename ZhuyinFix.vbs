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
args = " run --with pynput --with pystray --with pillow --with uiautomation pythonw zhuyinfix.py"
' 先用 --offline（套件都在 uv 快取裡，不必連網；開機時網路/VPN 還沒好會讓 uv 解析失敗而啟動不了）
' 等它結束：正常常駐會一直等到程式關掉；只有 uv 起不來（非 0）才退回連網模式再試一次
rv = sh.Run("""" & uv & """ run --offline" & Mid(args, 5), 0, True)
If rv <> 0 Then sh.Run """" & uv & """" & args, 0, False
