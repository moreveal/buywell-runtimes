$ErrorActionPreference = "Stop"

$runtimeDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $runtimeDirectory ".venv\Scripts\python.exe"
$runtime = Join-Path $runtimeDirectory "ggsel_runtime.py"
$config = Join-Path $runtimeDirectory "config.json"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Run install.bat first."
}
if (-not (Test-Path -LiteralPath $config)) {
    throw "config.json is missing. Run install.bat first."
}

$taskName = "Buywell GGSel Runtime"
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = ('"{0}" --config "{1}"' -f $runtime, $config)
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $runtimeDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.State -eq "Running") {
    Stop-ScheduledTask -TaskName $taskName
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 1
$installedTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
if ($installedTask.State -notin @("Running", "Ready")) {
    throw "The background task was installed but did not start. Current state: $($installedTask.State)"
}

Write-Host "Buywell GGSel is installed and running as $userId."
Write-Host "Open Task Scheduler and select '$taskName' to view or stop it."
