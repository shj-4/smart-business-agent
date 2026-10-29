# setup_services.ps1 - Install Smart Business Agent as permanent Windows services
# Run as Administrator:  powershell -ExecutionPolicy Bypass -File .\setup_services.ps1

$ErrorActionPreference = 'Stop'
# مسار النشر مرن: متغير بيئة SMART_BUSINESS_AGENT_ROOT أو SMART_BUSINESS_ROOT
# يتيح نقل المجلد دون تعديل السكربت؛ وإلا يُستنتج من مكان السكربت نفسه.
$NSSM   = if ($env:NSSM_PATH) { $env:NSSM_PATH } elseif ($env:SMART_BUSINESS_AGENT_ROOT) { Join-Path $env:SMART_BUSINESS_AGENT_ROOT "nssm\win64\nssm.exe" } else { 'C:\nssm\win64\nssm.exe' }
# بدائل: SMART_BUSINESS_AGENT_ROOT / SMART_BUSINESS_ROOT / $PSScriptRoot
$ROOT   = if ($env:SMART_BUSINESS_AGENT_ROOT) { $env:SMART_BUSINESS_AGENT_ROOT } elseif ($env:SMART_BUSINESS_ROOT) { $env:SMART_BUSINESS_ROOT } elseif ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ROOT) { $ROOT = 'C:\smart-business-agent' }

if (-not (Test-Path $NSSM))                { Write-Error "nssm not found: $NSSM" }
if (-not (Test-Path "$ROOT\bot.py"))       { Write-Error "bot.py not found: $ROOT\bot.py" }
if (-not (Test-Path "$ROOT\start_bot.cmd")){ Write-Error "start_bot.cmd not found" }
if (-not (Test-Path "$ROOT\start_api.cmd")){ Write-Error "start_api.cmd not found" }

# إنشاء مجلد logs المطلوب لـ NSSM (كان في .gitignore ولا يُنشأ)
if (-not (Test-Path "$ROOT\logs")) { New-Item -ItemType Directory -Path "$ROOT\logs" | Out-Null; Write-Host "  Created logs\" }
if (-not (Test-Path "$ROOT\data\backups")) { New-Item -ItemType Directory -Path "$ROOT\data\backups" | Out-Null; Write-Host "  Created data\backups\" }

Write-Host "== Creating bot service: SmartBot =="
if (-not (Get-Service -Name SmartBot -ErrorAction SilentlyContinue)) {
    & $NSSM install SmartBot "$ROOT\start_bot.cmd" | Out-Null
}
& $NSSM set SmartBot Application "$ROOT\start_bot.cmd"
& $NSSM set SmartBot AppDirectory "$ROOT"
& $NSSM set SmartBot AppStdout  "$ROOT\logs\bot_out.log"
& $NSSM set SmartBot AppStderr  "$ROOT\logs\bot_err.log"
& $NSSM set SmartBot AppRotateFiles 1
& $NSSM set SmartBot AppRotateBytes 1048576
& $NSSM set SmartBot Start SERVICE_AUTO_START
& $NSSM set SmartBot AppRestartDelay 15000
& $NSSM set SmartBot AppExit Default Restart
& $NSSM set SmartBot AppExit 0 Exit
& $NSSM set SmartBot AppExit 1 Exit
& $NSSM set SmartBot AppExit 78 Exit
# لا تشغّل كـ LocalSystem مع مدخلات غير موثوقة (صور/PDF) — استخدم حسابًا محدودًا إن وجد
if ($env:SMART_BOT_SERVICE_ACCOUNT) {
    & $NSSM set SmartBot ObjectName $env:SMART_BOT_SERVICE_ACCOUNT
}
Write-Host "  Created SmartBot"

Write-Host "== Creating API service: SmartBotAPI =="
if (-not (Get-Service -Name SmartBotAPI -ErrorAction SilentlyContinue)) {
    & $NSSM install SmartBotAPI "$ROOT\start_api.cmd" | Out-Null
}
& $NSSM set SmartBotAPI Application "$ROOT\start_api.cmd"
& $NSSM set SmartBotAPI AppDirectory "$ROOT"
& $NSSM set SmartBotAPI AppStdout  "$ROOT\logs\api_out.log"
& $NSSM set SmartBotAPI AppStderr  "$ROOT\logs\api_err.log"
& $NSSM set SmartBotAPI AppRotateFiles 1
& $NSSM set SmartBotAPI AppRotateBytes 1048576
& $NSSM set SmartBotAPI Start SERVICE_AUTO_START
& $NSSM set SmartBotAPI AppRestartDelay 15000
& $NSSM set SmartBotAPI AppExit Default Restart
& $NSSM set SmartBotAPI AppExit 1 Exit
& $NSSM set SmartBotAPI AppExit 78 Exit
if ($env:SMART_BOT_SERVICE_ACCOUNT) {
    & $NSSM set SmartBotAPI ObjectName $env:SMART_BOT_SERVICE_ACCOUNT
}
Write-Host "  Created SmartBotAPI"

Write-Host "== Creating daily backup task: SmartBotBackup =="
$backupTask = 'SmartBotBackup'
$existingTask = Get-ScheduledTask -TaskName $backupTask -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "  Removing old task: $backupTask"
    Unregister-ScheduledTask -TaskName $backupTask -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute "$ROOT\backup_db.cmd" -WorkingDirectory $ROOT
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00AM"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $backupTask -Action $action -Trigger $trigger -Settings $settings -Description "Smart Business Agent - daily SQLite backup" | Out-Null
Write-Host "  Created: $backupTask (daily at 03:00 AM)"

Write-Host "== Starting services =="
& $NSSM start SmartBot | Out-Null
& $NSSM start SmartBotAPI | Out-Null

Write-Host "== Final status =="
Start-Sleep -Seconds 2
Get-Service -Name SmartBot, SmartBotAPI | Select-Object Name, Status, StartType | Format-Table -AutoSize
Get-ScheduledTask -TaskName $backupTask | Select-Object TaskName, State, @{N='NextRun';E={$_.Triggers[0].StartBoundary}} | Format-Table -AutoSize
Write-Host "Setup completed successfully."
