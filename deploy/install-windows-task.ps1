# Registers a Windows Scheduled Task that starts the app at logon (no console window).
# Run from the project root in PowerShell:  .\deploy\install-windows-task.ps1
$root   = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $root "backend\venv\Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m uvicorn main:app --host 0.0.0.0 --port 8000" `
    -WorkingDirectory (Join-Path $root "backend")
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "Personal Assistant" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "Personal Assistant"
Write-Host "Installed. Open http://localhost:8000/  (remove with: Unregister-ScheduledTask -TaskName 'Personal Assistant')"
