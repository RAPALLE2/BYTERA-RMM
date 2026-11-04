@echo off
REM =============================================================================
REM  fix.bat - Notfall-Neuinstallation von BYTERA RMM aus GitHub (Windows)
REM -----------------------------------------------------------------------------
REM  1. Container stoppen (docker compose down).
REM  2. Wichtige Daten sichern (data, storage, media, logs, frontend\image,
REM     src\files) nach src\debug\backup\<zeitstempel>\.
REM  3. Neuesten Commit aus GitHub laden (Standard RAPALLE2/BYTERA-RMM, eigene
REM     URL moeglich) und entpacken.
REM  4. Neuen Code einspielen - gesicherte Datenordner bleiben erhalten.
REM  5. Container wieder starten (docker compose up -d --build).
REM
REM  Aufruf:  im Projektordner  ->  src\debug\fix.bat   (am besten als Admin)
REM  Benoetigt: Docker Desktop und PowerShell (ab Windows 10 vorhanden).
REM =============================================================================
setlocal EnableDelayedExpansion

REM --- Projektwurzel (dieses Skript liegt in src\debug\) ---
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%\..\.." >nul
set "ROOT=%CD%"

echo ==============================================
echo  BYTERA RMM - Notfall-Neuinstallation (fix.bat)
echo  Projektordner: %ROOT%
echo ==============================================

REM Zu erhaltende Datenordner (identisch zum eingebauten Updater).
set "PRESERVE=src\backend\data src\backend\media src\backend\storage src\frontend\image src\backend\logs src\files"

REM --- docker compose Befehl ermitteln ---
set "DC="
docker compose version >nul 2>&1 && set "DC=docker compose"
if not defined DC ( docker-compose version >nul 2>&1 && set "DC=docker-compose" )

REM --- GitHub-URL abfragen ---
set "DEFAULT_URL=https://github.com/RAPALLE2/BYTERA-RMM"
set "URL="
set /p "URL=GitHub-URL eingeben, leer lassen fuer %DEFAULT_URL%: "
if "!URL!"=="" set "URL=%DEFAULT_URL%"

REM owner/repo aus der URL loesen (PowerShell, robust)
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$u='!URL!' -replace '\.git$','' -replace '^https?://','' -replace '^github.com/',''; $p=$u.Split('/'); if($p.Count -ge 2 -and $p[0] -and $p[1]){ \"$($p[0])/$($p[1])\" } else { '' }"`) do set "SLUG=%%A"
if "!SLUG!"=="" ( echo FEHLER: Konnte owner/repo aus '!URL!' nicht lesen.& popd & exit /b 1 )
echo -^> Quelle: !SLUG!

REM --- 1) Container stoppen ---
echo.
echo [1/5] Container stoppen ...
if defined DC if exist "%ROOT%\docker-compose.yml" ( %DC% down )
for %%C in (bytera-rmm bytera-rmm-guacd bytera-rmm-db bytera-guacd) do docker stop %%C >nul 2>&1

REM --- 2) Daten sichern ---
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"`) do set "STAMP=%%T"
set "BACKUP=%ROOT%\src\debug\backup\!STAMP!"
echo.
echo [2/5] Daten sichern nach !BACKUP! ...
md "!BACKUP!" >nul 2>&1
for %%R in (%PRESERVE%) do (
    if exist "%ROOT%\%%R" (
        for %%D in ("!BACKUP!\%%R\..") do md "%%~fD" >nul 2>&1
        xcopy /E /I /H /Y /Q "%ROOT%\%%R" "!BACKUP!\%%R\" >nul
        echo    gesichert: %%R
    )
)
echo    Sicherung abgeschlossen.

REM --- 3) Neuesten Commit laden + entpacken (alles in PowerShell) ---
echo.
echo [3/5] Neuesten Stand von GitHub laden ...
set "TMP=%TEMP%\bytera-fix-!RANDOM!"
md "!TMP!" >nul 2>&1
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$slug='!SLUG!'; $tmp='!TMP!'; $zip=Join-Path $tmp 'src.zip'; $ok=$false;" ^
  "foreach($ref in @('HEAD','main','master')){ try{ Invoke-WebRequest -UseBasicParsing \"https://codeload.github.com/$slug/zip/$ref\" -OutFile $zip; $ok=$true; break } catch {} }" ^
  "if(-not $ok){ Write-Error 'Download fehlgeschlagen' };" ^
  "Expand-Archive -Path $zip -DestinationPath $tmp -Force;"
if errorlevel 1 ( echo FEHLER: Download/Entpacken fehlgeschlagen.& rmdir /S /Q "!TMP!" >nul 2>&1 & popd & exit /b 1 )

REM Entpackten Repo-Ordner finden (<repo>-<ref>) und Code einspielen - PowerShell macht das Kopieren.
echo.
echo [4/5] Neuen Code einspielen ^(Datenordner bleiben erhalten^) ...
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$tmp='!TMP!'; $root='%ROOT%';" ^
  "$srcroot=Get-ChildItem -Directory $tmp | Where-Object { Test-Path (Join-Path $_.FullName 'src') } | Select-Object -First 1;" ^
  "if(-not $srcroot){ Write-Error 'Entpackter Ordner sieht falsch aus' };" ^
  "$preserve=@('src\backend\data','src\backend\media','src\backend\storage','src\frontend\image','src\backend\logs','src\files');" ^
  "Get-ChildItem -Recurse -File $srcroot.FullName | ForEach-Object {" ^
  "  $rel=$_.FullName.Substring($srcroot.FullName.Length).TrimStart('\');" ^
  "  $skip=$false; foreach($p in $preserve){ if($rel -eq $p -or $rel.StartsWith($p+'\')){ $skip=$true; break } }" ^
  "  if($skip){ return }" ^
  "  $dst=Join-Path $root $rel; $dir=Split-Path $dst -Parent;" ^
  "  if(-not (Test-Path $dir)){ New-Item -ItemType Directory -Force -Path $dir | Out-Null }" ^
  "  Copy-Item -Force $_.FullName $dst }"
if errorlevel 1 ( echo FEHLER: Einspielen fehlgeschlagen.& rmdir /S /Q "!TMP!" >nul 2>&1 & popd & exit /b 1 )
rmdir /S /Q "!TMP!" >nul 2>&1
echo    Code eingespielt.

REM --- 5) Container starten ---
echo.
echo [5/5] Container starten ...
if defined DC if exist "%ROOT%\docker-compose.yml" (
    %DC% up -d --build
    echo.
    echo Fertig. Logs ansehen mit:  %DC% logs -f rmm
) else (
    echo    ^(kein docker compose gefunden - bitte den Dienst von Hand starten^)
)
echo.
echo Sicherung liegt unter: !BACKUP!
echo Falls etwas schiefging, kann von dort zurueckkopiert werden.
popd
endlocal
