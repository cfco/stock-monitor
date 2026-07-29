Set WshShell = CreateObject("WScript.Shell")
strDir = Replace(WScript.ScriptFullName, WScript.ScriptName, "")
strCmd = Chr(34) & strDir & ".venv\Scripts\pythonw.exe" & Chr(34) & " " & Chr(34) & strDir & "floating_window.py" & Chr(34)
WshShell.Run strCmd, 0, False
