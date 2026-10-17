' Open the chat page without a console window.
' Detect the server (retrying briefly so a just-started server is not mistaken
' for absent); start it hidden if truly missing; then open the browser.
Option Explicit

Dim sh, fso, scriptDir, http, serverUp, attempt

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

serverUp = False
For attempt = 1 To 3
    On Error Resume Next
    Set http = CreateObject("Microsoft.XMLHTTP")
    http.Open "GET", "http://127.0.0.1:8787/", False
    http.Send
    If Err.Number = 0 Then
        serverUp = True
        Exit For
    End If
    On Error GoTo 0
    WScript.Sleep 1000
Next

If Not serverUp Then
    sh.CurrentDirectory = scriptDir
    sh.Run "cmd /c python app.py --no-browser", 0, False
    WScript.Sleep 2000
End If

sh.Run "http://127.0.0.1:8787/", 1, False
