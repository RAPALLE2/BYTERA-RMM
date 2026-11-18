# ======================================================================
#  BYTERA Client-Provisioning (Windows)
#  Erzeugt vom Skript-Modul. Läuft als SYSTEM (geplante Aufgabe), kann aber
#  auch von Hand gestartet werden:  powershell -ExecutionPolicy Bypass -File run.ps1
#  Fortschritt: %ProgramData%\BYTERA\provision\status.json + log.txt
# ======================================================================
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch {}

$Cfg = @'
__BYTERA_CONFIG__
'@ | ConvertFrom-Json

$Base = Join-Path $env:ProgramData 'BYTERA\provision'
New-Item -ItemType Directory -Force -Path $Base | Out-Null
$LogFile = Join-Path $Base 'log.txt'
$StatusFile = Join-Path $Base 'status.json'
$script:StepNo = 0
$script:Errors = 0
$script:Total = [int]$Cfg._steps
$script:NeedReboot = $false
$script:Hives = @()
$script:Mounted = @()

function Log([string]$m) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}
function Status([string]$state, [string]$step) {
    $o = [ordered]@{ state = $state; step = $step; n = $script:StepNo; total = $script:Total;
                     errors = $script:Errors; reboot = $script:NeedReboot; time = (Get-Date).ToString('s') }
    ($o | ConvertTo-Json -Compress) | Set-Content -Path $StatusFile -Encoding UTF8
}
function Step([string]$title, [scriptblock]$sb) {
    $script:StepNo++
    Log "=== [$($script:StepNo)/$($script:Total)] $title"
    Status 'running' $title
    try { & $sb } catch { $script:Errors++; Log "FEHLER: $($_.Exception.Message)" }
}
function Warn([string]$m) { $script:Errors++; Log "FEHLER: $m" }
function Has($list, $item) { return @($list) -contains $item }

# ---------------------------------------------------------------- Registry
function Set-Reg([string]$path, [string]$name, [string]$type, $value) {
    if (-not (Test-Path -LiteralPath $path)) { New-Item -Path $path -Force | Out-Null }
    New-ItemProperty -LiteralPath $path -Name $name -PropertyType $type -Value $value -Force | Out-Null
}
function Mount-UserHives {
    # Alle Benutzer: angemeldete (HKU\SID), nicht angemeldete (NTUSER.DAT laden) und
    # das Standardprofil (gilt für künftig angelegte Benutzer).
    $loaded = @(Get-ChildItem 'Registry::HKEY_USERS' | Where-Object { $_.PSChildName -match '^S-1-5-21-[\d-]+$' } |
                ForEach-Object { $_.PSChildName })
    foreach ($sid in $loaded) { $script:Hives += "Registry::HKEY_USERS\$sid" }
    $profiles = @(Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.SID -match '^S-1-5-21-' })
    $targets = @()
    foreach ($p in $profiles) { if ($loaded -notcontains $p.SID) { $targets += ,@("BYTERA_$($p.SID)", (Join-Path $p.LocalPath 'NTUSER.DAT')) } }
    $targets += ,@('BYTERA_Default', (Join-Path $env:SystemDrive 'Users\Default\NTUSER.DAT'))
    foreach ($t in $targets) {
        if (-not (Test-Path -LiteralPath $t[1])) { continue }
        & reg.exe load "HKU\$($t[0])" "$($t[1])" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $script:Hives += "Registry::HKEY_USERS\$($t[0])"; $script:Mounted += $t[0] }
    }
    Log "Benutzerprofile für Einstellungen: $($script:Hives.Count) (inkl. Standardprofil)"
}
function Dismount-UserHives {
    [gc]::Collect(); [gc]::WaitForPendingFinalizers(); Start-Sleep -Seconds 1
    foreach ($k in $script:Mounted) { & reg.exe unload "HKU\$k" 2>&1 | Out-Null }
}
function Set-UserReg([string]$sub, [string]$name, [string]$type, $value) {
    foreach ($h in $script:Hives) {
        try { Set-Reg "$h\$sub" $name $type $value } catch { Log "  ${h}\${sub}\${name}: $($_.Exception.Message)" }
    }
}
function Expand-Name([string]$n) {
    $serial = ''
    try { $serial = (Get-CimInstance Win32_BIOS).SerialNumber } catch {}
    $serial = ($serial -replace '[^A-Za-z0-9]', '')
    $n = $n -replace '\{SERIAL\}', $serial
    $n = ($n -replace '[^A-Za-z0-9-]', '')
    if ($n.Length -gt 15) { $n = $n.Substring(0, 15) }
    return $n.Trim('-')
}

# ======================================================================
Log "BYTERA Provisioning gestartet (erzeugt $($Cfg._generated)) auf $env:COMPUTERNAME"
Status 'running' 'Vorbereitung'
Step 'Vorbereitung' {
    $os = Get-CimInstance Win32_OperatingSystem
    Log "System: $($os.Caption) $($os.Version) · Benutzer: $(whoami)"
    Mount-UserHives
}

if ($Cfg.general.restore_point) {
    Step 'Wiederherstellungspunkt' {
        Enable-ComputerRestore -Drive "$env:SystemDrive\" -ErrorAction SilentlyContinue
        Set-Reg 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore' 'SystemRestorePointCreationFrequency' 'DWord' 0
        Checkpoint-Computer -Description 'BYTERA Provisioning' -RestorePointType MODIFY_SETTINGS -ErrorAction Stop
        Log 'Wiederherstellungspunkt erstellt'
    }
}

if ($Cfg.general.computer_name -or $Cfg.general.timezone) {
    Step 'Name & Zeitzone' {
        if ($Cfg.general.timezone) { Set-TimeZone -Id $Cfg.general.timezone; Log "Zeitzone: $($Cfg.general.timezone)" }
        if ($Cfg.general.computer_name) {
            $n = Expand-Name $Cfg.general.computer_name
            if ($n -and $n -ne $env:COMPUTERNAME) {
                Rename-Computer -NewName $n -Force -ErrorAction Stop
                $script:NeedReboot = $true
                Log "Computer wird umbenannt in $n (nach Neustart)"
            }
        }
    }
}

if ($Cfg.custom.pre.Trim()) {
    Step 'Eigene Befehle (vorher)' { & ([scriptblock]::Create($Cfg.custom.pre)) 2>&1 | ForEach-Object { Log "  $_" } }
}

# ---------------------------------------------------------------- Debloat
$appx = @($Cfg.debloat.appx) + @($Cfg.debloat.appx_custom) | Where-Object { $_ }
if ($appx.Count) {
    Step "Vorinstallierte Apps entfernen ($($appx.Count))" {
        $prov = @(Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue)
        foreach ($a in $appx) {
            $hit = $false
            foreach ($p in @(Get-AppxPackage -AllUsers -Name $a -ErrorAction SilentlyContinue)) {
                $hit = $true
                try { Remove-AppxPackage -Package $p.PackageFullName -AllUsers -ErrorAction Stop; Log "  entfernt: $($p.Name)" }
                catch { Log "  $($p.Name): $($_.Exception.Message)" }
            }
            foreach ($p in @($prov | Where-Object { $_.DisplayName -like $a })) {
                $hit = $true
                try { Remove-AppxProvisionedPackage -Online -PackageName $p.PackageName -ErrorAction Stop | Out-Null; Log "  aus Vorlage entfernt: $($p.DisplayName)" }
                catch { Log "  $($p.DisplayName): $($_.Exception.Message)" }
            }
            if (-not $hit) { Log "  nicht vorhanden: $a" }
        }
    }
}

if ($Cfg.debloat.onedrive) {
    Step 'OneDrive deinstallieren' {
        Get-Process OneDrive -ErrorAction SilentlyContinue | Stop-Process -Force
        foreach ($exe in @("$env:SystemRoot\System32\OneDriveSetup.exe", "$env:SystemRoot\SysWOW64\OneDriveSetup.exe")) {
            if (Test-Path $exe) { Start-Process $exe -ArgumentList '/uninstall' -Wait -NoNewWindow; Log "  $exe /uninstall" }
        }
        Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive' 'DisableFileSyncNGSC' 'DWord' 1
        Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Run' 'OneDriveSetup' 'String' ''
    }
}

$tw = @($Cfg.debloat.tweaks)
if ($tw.Count) {
    Step "Datenschutz & Oberfläche ($($tw.Count) Einstellungen)" {
        $adv = 'Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'
        $cdm = 'Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager'
        if (Has $tw 'telemetry') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection' 'AllowTelemetry' 'DWord' 1
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection' 'DoNotShowFeedbackNotifications' 'DWord' 1
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AdvertisingInfo' 'DisabledByGroupPolicy' 'DWord' 1
            Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Privacy' 'TailoredExperiencesWithDiagnosticDataEnabled' 'DWord' 0
            Stop-Service DiagTrack -Force -ErrorAction SilentlyContinue
            Set-Service DiagTrack -StartupType Disabled -ErrorAction SilentlyContinue
            Log '  Telemetrie minimiert'
        }
        if (Has $tw 'consumer') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' 'DisableWindowsConsumerFeatures' 'DWord' 1
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' 'DisableConsumerAccountStateContent' 'DWord' 1
            foreach ($v in 'SilentInstalledAppsEnabled', 'SystemPaneSuggestionsEnabled', 'SoftLandingEnabled', 'PreInstalledAppsEnabled',
                           'OemPreInstalledAppsEnabled', 'SubscribedContent-338388Enabled', 'SubscribedContent-338389Enabled',
                           'SubscribedContent-353694Enabled', 'SubscribedContent-353696Enabled') { Set-UserReg $cdm $v 'DWord' 0 }
            Set-UserReg $adv 'Start_IrisRecommendations' 'DWord' 0
            Log '  Werbung & Vorschläge aus'
        }
        if (Has $tw 'bing') {
            Set-UserReg 'Software\Policies\Microsoft\Windows\Explorer' 'DisableSearchBoxSuggestions' 'DWord' 1
            Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Search' 'BingSearchEnabled' 'DWord' 0
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search' 'ConnectedSearchUseWeb' 'DWord' 0
            Log '  Websuche im Startmenü aus'
        }
        if (Has $tw 'copilot') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsCopilot' 'TurnOffWindowsCopilot' 'DWord' 1
            Set-UserReg 'Software\Policies\Microsoft\Windows\WindowsCopilot' 'TurnOffWindowsCopilot' 'DWord' 1
            Set-UserReg $adv 'ShowCopilotButton' 'DWord' 0
            Log '  Copilot aus'
        }
        if (Has $tw 'recall') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsAI' 'DisableAIDataAnalysis' 'DWord' 1
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsAI' 'AllowRecallEnablement' 'DWord' 0
            Log '  Recall aus'
        }
        if (Has $tw 'widgets') { Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Dsh' 'AllowNewsAndInterests' 'DWord' 0; Log '  Widgets aus' }
        if (Has $tw 'chat') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Chat' 'ChatIcon' 'DWord' 3
            Set-UserReg $adv 'TaskbarMn' 'DWord' 0
            Log '  Chat-Symbol aus'
        }
        if (Has $tw 'lockscreen_tips') {
            Set-UserReg $cdm 'RotatingLockScreenOverlayEnabled' 'DWord' 0
            Set-UserReg $cdm 'SubscribedContent-338387Enabled' 'DWord' 0
            Log '  Sperrbildschirm-Tipps aus'
        }
        if (Has $tw 'edge_firstrun') { Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Edge' 'HideFirstRunExperience' 'DWord' 1; Log '  Edge-Ersteinrichtung übersprungen' }
        if (Has $tw 'file_ext') { Set-UserReg $adv 'HideFileExt' 'DWord' 0; Log '  Dateiendungen sichtbar' }
        if (Has $tw 'hidden_files') { Set-UserReg $adv 'Hidden' 'DWord' 1; Log '  versteckte Dateien sichtbar' }
        if (Has $tw 'taskbar_left') { Set-UserReg $adv 'TaskbarAl' 'DWord' 0; Log '  Taskleiste links' }
        if (Has $tw 'classic_menu') {
            Set-UserReg 'Software\Classes\CLSID\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\InprocServer32' '(default)' 'String' ''
            Log '  klassisches Kontextmenü'
        }
        if (Has $tw 'gamedvr') {
            Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\GameDVR' 'AllowGameDVR' 'DWord' 0
            Set-UserReg 'System\GameConfigStore' 'GameDVR_Enabled' 'DWord' 0
            Log '  Game DVR aus'
        }
        if (Has $tw 'darkmode') {
            Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' 'AppsUseLightTheme' 'DWord' 0
            Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' 'SystemUsesLightTheme' 'DWord' 0
            Log '  dunkles Design'
        }
    }
}

if ($Cfg.w11d.enabled) {
    Step 'Win11Debloat (Raphire)' {
        $code = Invoke-RestMethod -Uri 'https://debloat.raphi.re/' -UseBasicParsing
        $sb = [scriptblock]::Create($code)
        $common = @{ Silent = $true; SkipExplorerRestart = $true }
        foreach ($p in @($Cfg.w11d.params) + @($Cfg.w11d.extra)) { if ($p) { $common[$p] = $true } }
        $runs = @()
        if ($Cfg.w11d.target -ne 'users') { $runs += ,@('Standardprofil (neue Benutzer)', @{ Sysprep = $true }) }
        if ($Cfg.w11d.target -ne 'default') {
            $active = @(Get-CimInstance Win32_UserProfile | Where-Object { $_.Loaded } | ForEach-Object { $_.SID })
            foreach ($u in @(Get-CimInstance Win32_UserProfile | Where-Object { -not $_.Special -and $_.SID -match '^S-1-5-21-' })) {
                $name = Split-Path $u.LocalPath -Leaf
                if ($active -contains $u.SID) { Log "  übersprungen: $name ist gerade angemeldet (Win11Debloat braucht abgemeldete Benutzer)"; continue }
                $runs += ,@("Benutzer $name", @{ User = $name })
            }
        }
        $first = $true
        foreach ($r in $runs) {
            $args2 = $common.Clone()
            foreach ($k in $r[1].Keys) { $args2[$k] = $r[1][$k] }
            if (-not $first) { [void]$args2.Remove('RemoveApps'); [void]$args2.Remove('RemoveGamingApps'); [void]$args2.Remove('RemoveHPApps'); [void]$args2.Remove('CreateRestorePoint') }
            Log "  Lauf für $($r[0]): -$((@($args2.Keys) | Sort-Object) -join ' -')"
            & $sb @args2 *>&1 | ForEach-Object { if ("$_".Trim()) { Log "    $_" } }
            $first = $false
        }
    }
}

# ---------------------------------------------------------------- Effizienz
Step 'Energie & Effizienz' {
    $e = $Cfg.efficiency
    $o = @($e.options)
    $plans = @{ balanced = '381b4222-f694-41f0-9685-ff5bb260df2e'; high = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c' }
    if ($e.power_plan -eq 'ultimate') {
        $out = (& powercfg.exe -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61) 2>&1 | Out-String
        if ($out -match '([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})') { & powercfg.exe -setactive $Matches[1]; Log '  Energieplan: Ultimative Leistung' }
        else { & powercfg.exe -setactive $plans.high; Log '  Ultimative Leistung nicht verfügbar → Höchstleistung' }
    } elseif ($e.power_plan) {
        & powercfg.exe -setactive $plans[$e.power_plan] 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0 -and $e.power_plan -eq 'high') {
            & powercfg.exe -duplicatescheme $plans.high 2>&1 | Out-Null; & powercfg.exe -setactive $plans.high
        }
        Log "  Energieplan: $($e.power_plan)"
    }
    if ($null -ne $e.monitor_ac) { & powercfg.exe /change monitor-timeout-ac $e.monitor_ac; Log "  Bildschirm aus (Netz): $($e.monitor_ac) Min." }
    if ($null -ne $e.standby_ac) { & powercfg.exe /change standby-timeout-ac $e.standby_ac; Log "  Energiesparen (Netz): $(if ($e.standby_ac -eq 0) { 'nie' } else { "$($e.standby_ac) Min." })" }
    if (Has $o 'fast_startup_off') { Set-Reg 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled' 'DWord' 0; Log '  Schnellstart aus' }
    if (Has $o 'hibernate_off') { & powercfg.exe /hibernate off; Log '  Ruhezustand aus' }
    if (Has $o 'visual_perf') { Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects' 'VisualFXSetting' 'DWord' 2; Log '  visuelle Effekte: Leistung' }
    if (Has $o 'transparency_off') { Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' 'EnableTransparency' 'DWord' 0; Log '  Transparenz aus' }
    if (Has $o 'startup_delay_off') { Set-UserReg 'Software\Microsoft\Windows\CurrentVersion\Explorer\Serialize' 'StartupDelayInMSec' 'DWord' 0; Log '  Autostart-Verzögerung aus' }
    if (Has $o 'background_apps_off') { Set-Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\AppPrivacy' 'LetAppsRunInBackground' 'DWord' 2; Log '  Hintergrund-Apps aus' }
}

if ($Cfg.optimizer.enabled) {
    Step 'Optimizer (hellzerg) mit Vorlage' {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/hellzerg/optimizer/releases/latest' -Headers @{ 'User-Agent' = 'BYTERA' } -UseBasicParsing
        $asset = @($rel.assets | Where-Object { $_.name -like '*.exe' }) | Select-Object -First 1
        if (-not $asset) { throw 'Keine Optimizer-EXE im neuesten Release gefunden' }
        $exe = Join-Path $Base 'optimizer.exe'
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $exe -UseBasicParsing
        $tpl = Join-Path $Base 'optimizer.json'
        [IO.File]::WriteAllText($tpl, [string]$Cfg.optimizer.template, (New-Object Text.UTF8Encoding($false)))
        Log "  $($asset.name) ($($rel.tag_name)) mit Vorlage gestartet"
        $p = Start-Process -FilePath $exe -ArgumentList "/config=`"$tpl`"" -PassThru
        if (-not $p.WaitForExit(1200000)) { $p.Kill(); Warn 'Optimizer nach 20 Minuten beendet' } else { Log "  Optimizer beendet (Exit $($p.ExitCode))" }
    }
}

if (@($Cfg.features.enable).Count -or @($Cfg.features.disable).Count) {
    Step 'Windows-Features' {
        foreach ($f in @($Cfg.features.enable)) {
            try { $r = Enable-WindowsOptionalFeature -Online -FeatureName $f -All -NoRestart -ErrorAction Stop; if ($r.RestartNeeded) { $script:NeedReboot = $true }; Log "  aktiviert: $f" }
            catch { Warn "$f aktivieren: $($_.Exception.Message)" }
        }
        foreach ($f in @($Cfg.features.disable)) {
            try { $r = Disable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart -ErrorAction Stop; if ($r.RestartNeeded) { $script:NeedReboot = $true }; Log "  deaktiviert: $f" }
            catch { Warn "$f deaktivieren: $($_.Exception.Message)" }
        }
    }
}

# ---------------------------------------------------------------- Apps
function Find-Winget {
    $c = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Resolve-Path "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe\winget.exe" -ErrorAction SilentlyContinue |
         Sort-Object { $_.Path } | Select-Object -Last 1
    if ($p) { return $p.Path }
    return $null
}
function Ensure-Choco {
    $c = Get-Command choco.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    Log '  installiere Chocolatey …'
    Set-ExecutionPolicy Bypass -Scope Process -Force
    Invoke-Expression ((New-Object Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1')) *>&1 | Out-Null
    $p = Join-Path $env:ProgramData 'chocolatey\bin\choco.exe'
    if (Test-Path $p) { return $p }
    throw 'Chocolatey konnte nicht installiert werden'
}
$apps = @($Cfg._apps)
$custom = @($Cfg.apps.custom) | Where-Object { $_ }
if ($apps.Count -or $custom.Count -or $Cfg.apps.upgrade_all) {
    Step "Standard-Apps ($($apps.Count + $custom.Count), $($Cfg.apps.engine))" {
        $engine = $Cfg.apps.engine
        $wg = $null
        if ($engine -eq 'winget') {
            $wg = Find-Winget
            if (-not $wg) { Log '  winget nicht gefunden → Chocolatey'; $engine = 'choco' }
            else { & $wg source update --disable-interactivity 2>&1 | Out-Null }
        }
        if ($engine -eq 'winget') {
            $ids = @($apps | Where-Object { $_.winget } | ForEach-Object { $_.winget }) + $custom
            foreach ($id in $ids) {
                $out = & $wg install --id $id -e --silent --scope machine --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-String
                $rc = $LASTEXITCODE
                if ($rc -ne 0 -and $out -match 'scope|Bereich|No applicable installer') {
                    $out = & $wg install --id $id -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-String
                    $rc = $LASTEXITCODE
                }
                # -1978335189 = bereits installiert/keine Aktualisierung
                if ($rc -eq 0 -or $rc -eq -1978335189 -or $rc -eq -1978335135) { Log "  ok: $id" } else { Warn "$id (winget $rc): $(($out -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' ')" }
            }
            if ($Cfg.apps.upgrade_all) {
                Log '  aktualisiere alle Programme (winget upgrade --all) …'
                & $wg upgrade --all --silent --include-unknown --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-Null
                Log "  winget upgrade beendet ($LASTEXITCODE)"
            }
        } elseif ($engine -eq 'choco') {
            $choco = Ensure-Choco
            $ids = @($apps | Where-Object { $_.choco } | ForEach-Object { $_.choco }) + $custom
            foreach ($id in $ids) {
                & $choco install $id -y --no-progress --limit-output 2>&1 | Out-Null
                if ($LASTEXITCODE -in 0, 1641, 3010) { Log "  ok: $id"; if ($LASTEXITCODE -ne 0) { $script:NeedReboot = $true } } else { Warn "$id (choco $LASTEXITCODE)" }
            }
            if ($Cfg.apps.upgrade_all) { & $choco upgrade all -y --no-progress --limit-output 2>&1 | Out-Null; Log "  choco upgrade all beendet ($LASTEXITCODE)" }
        } else {
            $slugs = @($apps | Where-Object { $_.ninite } | ForEach-Object { $_.ninite }) + $custom
            $skip = @($apps | Where-Object { -not $_.ninite } | ForEach-Object { $_.label })
            if ($skip.Count) { Log "  bei Ninite nicht verfügbar (übersprungen): $($skip -join ', ')" }
            if ($slugs.Count) {
                $exe = Join-Path $Base 'ninite.exe'
                Invoke-WebRequest -Uri ("https://ninite.com/{0}/ninite.exe" -f ($slugs -join '-')) -OutFile $exe -UseBasicParsing
                Log "  Ninite: $($slugs -join ', ')"
                $p = Start-Process -FilePath $exe -PassThru
                # Ninite (kostenlos) schliesst sein Fenster nicht selbst: warten, bis keine
                # Installer-Unterprozesse mehr laufen, dann beenden (max. 60 Min.).
                $idle = 0; $t0 = Get-Date
                while (-not $p.HasExited -and ((Get-Date) - $t0).TotalMinutes -lt 60) {
                    Start-Sleep -Seconds 15
                    $kids = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)" -ErrorAction SilentlyContinue)
                    if ($kids.Count -eq 0 -and ((Get-Date) - $t0).TotalMinutes -gt 2) { $idle++ } else { $idle = 0 }
                    if ($idle -ge 6) { break }
                }
                if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
                Log '  Ninite fertig'
            }
            if ($Cfg.apps.upgrade_all) {
                $wg = Find-Winget
                if ($wg) { & $wg upgrade --all --silent --include-unknown --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-Null; Log '  winget upgrade --all beendet' }
            }
        }
    }
}

# ---------------------------------------------------------------- Updates
if ($Cfg.updates.install) {
    Step 'Windows-Updates' {
        $s = New-Object -ComObject Microsoft.Update.Session
        $s.ClientApplicationID = 'BYTERA Provisioning'
        $crit = 'IsInstalled=0 and IsHidden=0'
        if (-not $Cfg.updates.drivers) { $crit += " and Type='Software'" }
        $res = $s.CreateUpdateSearcher().Search($crit)
        Log "  $($res.Updates.Count) Updates gefunden"
        if ($res.Updates.Count -gt 0) {
            $coll = New-Object -ComObject Microsoft.Update.UpdateColl
            foreach ($u in $res.Updates) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$coll.Add($u); Log "    $($u.Title)" }
            $dl = $s.CreateUpdateDownloader(); $dl.Updates = $coll
            Log '  lade herunter …'; [void]$dl.Download()
            $in = $s.CreateUpdateInstaller(); $in.Updates = $coll
            Log '  installiere …'; $r = $in.Install()
            Log "  Ergebnis: $(@{2='erfolgreich';3='teilweise';4='fehlgeschlagen';5='abgebrochen'}[[int]$r.ResultCode]) · Neustart nötig: $($r.RebootRequired)"
            if ($r.RebootRequired) { $script:NeedReboot = $true }
            if ([int]$r.ResultCode -ge 4) { $script:Errors++ }
        }
    }
}

if ($Cfg.custom.post.Trim()) {
    Step 'Eigene Befehle (nachher)' { & ([scriptblock]::Create($Cfg.custom.post)) 2>&1 | ForEach-Object { Log "  $_" } }
}

Step 'Aufräumen' {
    if (Has $Cfg.efficiency.options 'cleanup') {
        foreach ($d in @("$env:SystemRoot\Temp", "$env:TEMP")) { Get-ChildItem $d -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
        Log '  Temp-Dateien gelöscht'
        & dism.exe /Online /Cleanup-Image /StartComponentCleanup /Quiet 2>&1 | Out-Null
        Log "  Komponentenspeicher bereinigt ($LASTEXITCODE)"
    }
    Dismount-UserHives
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $script:NeedReboot = $true }
}

Step 'Abschluss' {
    Log "Fertig · $($script:Errors) Fehler · Neustart nötig: $($script:NeedReboot)"
    & schtasks.exe /Delete /TN 'BYTERA-Provision' /F 2>&1 | Out-Null
}
$doReboot = ($Cfg.general.reboot -eq 'always') -or ($Cfg.general.reboot -eq 'needed' -and $script:NeedReboot)
if ($doReboot) { Log 'Neustart in 60 Sekunden' }
Status 'done' 'Fertig'
if ($doReboot) { & shutdown.exe /r /t 60 /c 'BYTERA: Einrichtung abgeschlossen - Neustart' }
