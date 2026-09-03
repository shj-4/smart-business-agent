# setup_services.ps1 - Install Smart Business Agent as permanent Windows services
# Run as Administrator:  powershell -ExecutionPolicy Bypass -File .\setup_services.ps1

$ErrorActionPreference = 'Stop'
$NSSM   = 'C:\nssm\win64\nssm.exe'
$ROOT   = 'C:\smart-business-agent'

if (-not (Test-Path $NSSM))                { Write-Error "nssm not found: $NSSM" }
if (-not (Test-Path "$ROOT\bot.py"))       { Write-Error "bot.py not found: $ROOT\bot.py" }
if (-not (Test-Path "$ROOT\start_bot.cmd")){ Write-Error "start_bot.cmd not found" }
if (-not (Test-Path "$ROOT\start_api.cmd")){ Write-Error "start_api.cmd not found" }

Write-Host "== Removing old services (if any) =="
foreach ($svc in 'SmartBusinessBot','SmartBot','SmartBotAPI') {
    $existing = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Stopping and removing $svc"
        & $NSSM stop $svc | Out-Null
        Start-Sleep -Milliseconds 800
        & $NSSM remove $svc confirm | Out-Null
        Write-Host "  Removed: $svc"
    } else {
        Write-Host "  Not present: $svc"
    }
}

Write-Host "== Creating bot service: SmartBot =="
& $NSSM install SmartBot "$ROOT\start_bot.cmd" | Out-Null
& $NSSM set SmartBot AppDirectory "$ROOT"
& $NSSM set SmartBot AppStdout  "$ROOT\logs\bot_out.log"
& $NSSM set SmartBot AppStderr  "$ROOT\logs\bot_err.log"
& $NSSM set SmartBot AppRotateFiles 1
& $NSSM set SmartBot AppRotateBytes 1048576
& $NSSM set SmartBot Start SERVICE_AUTO_START
& $NSSM set SmartBot ObjectName LocalSystem
Write-Host "  Created SmartBot"

Write-Host "== Creating API service: SmartBotAPI =="
& $NSSM install SmartBotAPI "$ROOT\start_api.cmd" | Out-Null
& $NSSM set SmartBotAPI AppDirectory "$ROOT"
& $NSSM set SmartBotAPI AppStdout  "$ROOT\logs\api_out.log"
& $NSSM set SmartBotAPI AppStderr  "$ROOT\logs\api_err.log"
& $NSSM set SmartBotAPI AppRotateFiles 1
& $NSSM set SmartBotAPI AppRotateBytes 1048576
& $NSSM set SmartBotAPI Start SERVICE_AUTO_START
& $NSSM set SmartBotAPI ObjectName LocalSystem
Write-Host "  Created SmartBotAPI"

Write-Host "== Starting services =="
& $NSSM start SmartBot | Out-Null
& $NSSM start SmartBotAPI | Out-Null

Write-Host "== Final status =="
Start-Sleep -Seconds 2
Get-Service -Name SmartBot, SmartBotAPI | Select-Object Name, Status, StartType | Format-Table -AutoSize
Write-Host "Setup completed successfully."
