param([string]$BackupRoot = (Join-Path $env:USERPROFILE 'SalonMaxBackups'))
$ErrorActionPreference = 'Stop'
[IO.Directory]::CreateDirectory($BackupRoot) | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity.User.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl',
        'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $BackupRoot -AclObject $acl
$tools = Join-Path $BackupRoot 'tools'
[IO.Directory]::CreateDirectory($tools) | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'pc_backup.ps1') -Destination (Join-Path $tools 'backup.ps1') -Force
$worker = Join-Path $tools 'backup.ps1'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $worker + '" -BackupRoot "' + $BackupRoot + '"')
$triggers = @((New-ScheduledTaskTrigger -Daily -At '02:45'), (New-ScheduledTaskTrigger -AtLogOn -User $identity.Name))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 20)
$taskName = 'Salon Max Daily Database Backup'
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Limited
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Description 'Encrypted daily Pi and hosted account database recovery backup. Catches up after missed runs.' -Force | Out-Null
    $mode = 'S4U'
} catch {
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
        -Principal $principal -Description 'Encrypted daily Pi and hosted account database backup. Requires user signed in; catches up at sign-in.' -Force | Out-Null
    $mode = 'Interactive'
}
@{task=$taskName;logon_mode=$mode;daily_time='02:45';retention_days=30;backup_root=$BackupRoot} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BackupRoot 'schedule.json') -Encoding UTF8
Write-Output ('Registered daily backup at 02:45; logon mode: ' + $mode)
Start-ScheduledTask -TaskName $taskName
