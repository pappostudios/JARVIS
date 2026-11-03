# Creates a "JARVIS" shortcut on the Windows desktop.
$root = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $desktop "JARVIS.lnk"))
$lnk.TargetPath = Join-Path $root "JARVIS.bat"
$lnk.WorkingDirectory = $root
$lnk.IconLocation = (Join-Path $root "web\icons\jarvis.ico")
$lnk.WindowStyle = 7   # start the console minimised
$lnk.Description = "J.A.R.V.I.S. personal assistant"
$lnk.Save()
Write-Host "Desktop shortcut created."
