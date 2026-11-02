param([string]$At)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $At) {
    $settingsPath = Join-Path $root 'data\settings.json'
    $At = if (Test-Path $settingsPath) { (Get-Content $settingsPath -Raw | ConvertFrom-Json).time } else { '09:00' }
}
if ($At -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw '时间格式必须是 HH:mm，例如 09:00' }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw '请先将 Windows 时区设为中国标准时间（UTC+08:00）' }
$projectPython = Join-Path $root '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $projectPython) { $projectPython } else { (Get-Command python).Source }
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$root\app.py`" --scheduled-once" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', $null))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable:$false -AllowStartIfOnBatteries:$true -DontStopIfGoingOnBatteries:$true -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'CSU Safety Review Readonly Check' -Action $action -Trigger $trigger -Settings $settings -Description 'Daily official H5 readonly safety-board roster comparison; never submits school records.' -Force | Out-Null
Write-Host "已注册每日 $At 执行只读名单比较的任务。错过时间不会补跑。"
