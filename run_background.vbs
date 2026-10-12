' run_background.vbs
' Launches ocr_daemon.py with NO console window and NO focus stealing.
'
' Double-click to start the daemon silently in the background.
' Check ocr_daemon.log (in this folder) for output.
' To stop: press Ctrl+Shift+F10, or end pythonw.exe in Task Manager.
'
' FULLSCREEN NOTE: this launcher deliberately shows no dialog on success.
' Any new window — even a message box — takes foreground focus, and an
' exclusive-fullscreen Direct3D application that loses focus drops to
' windowed mode. Errors still raise a dialog, since at that point there is
' nothing running to disturb.

Option Explicit

Dim fso, scriptDir, pyScript, shell, exe

Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pyScript  = scriptDir & "\ocr_daemon.py"

If Not fso.FileExists(pyScript) Then
    MsgBox "Cannot find ocr_daemon.py in:" & vbCrLf & scriptDir, _
           vbCritical, "OCR Daemon"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir

' pythonw.exe never allocates a console at all. Fall back to python.exe,
' which is still hidden by window style 0 below.
exe = "pythonw"
On Error Resume Next
shell.Run exe & " """ & pyScript & """", 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "python """ & pyScript & """", 0, False
    If Err.Number <> 0 Then
        MsgBox "Could not start Python. Is it on your PATH?" & vbCrLf & _
               Err.Description, vbCritical, "OCR Daemon"
        WScript.Quit 1
    End If
End If
On Error Goto 0

' No success dialog by design - see FULLSCREEN NOTE above.
' Confirm it started by checking ocr_daemon.log.
