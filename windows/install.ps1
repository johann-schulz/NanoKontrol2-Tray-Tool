<#
.SYNOPSIS
    Installs the nanoKONTROL2 mixer for Windows for the current user.

.DESCRIPTION
    - installs the Python packages from requirements.txt (user scope)
    - copies the script to %LOCALAPPDATA%\nanokontrol
    - creates an autostart shortcut (runs windowless via pythonw.exe)
    - starts it

    Safe to re-run: a running instance is stopped and replaced. Existing
    configuration in %APPDATA%\nanokontrol is never touched.

    Run it with:
        powershell -ExecutionPolicy Bypass -File .\windows\install.ps1

.PARAMETER NoAutostart
    Install everything, but do not create the autostart shortcut.

.PARAMETER NoStart
    Do not start the mixer at the end.

.PARAMETER Uninstall
    Stop the mixer, remove the autostart shortcut and the installed script.
    Configuration and Python packages are kept.
#>
param(
    [switch]$NoAutostart,
    [switch]$NoStart,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$ScriptName  = 'nanokontrol-windows.py'
$SourceDir   = $PSScriptRoot
$InstallDir  = Join-Path $env:LOCALAPPDATA 'nanokontrol'
$Target      = Join-Path $InstallDir $ScriptName
$StartupDir  = [Environment]::GetFolderPath('Startup')
$Shortcut    = Join-Path $StartupDir 'nanoKONTROL2 Mixer.lnk'
$ConfigDir   = Join-Path $env:APPDATA 'nanokontrol'
$MinPython   = [version]'3.11'

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host " warning: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host " error: $msg" -ForegroundColor Red; exit 1 }

function Stop-Mixer {
    # Supervisors first, otherwise they restart the service process we kill.
    $procs = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*' -and $_.CommandLine -and $_.CommandLine -like "*$ScriptName*"
    })
    $ordered = @($procs | Where-Object { $_.CommandLine -notlike '*--child*' }) +
               @($procs | Where-Object { $_.CommandLine -like '*--child*' })
    foreach ($proc in $ordered) {
        Info "Stopping running instance (PID $($proc.ProcessId))"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($ordered.Count -gt 0) { Start-Sleep -Seconds 1 }
}

function Find-Python {
    # The py launcher first, then whatever "python" is on PATH. The Microsoft
    # Store alias for "python" exists even without Python installed and only
    # opens the Store, so every candidate is actually asked for its version.
    # Windows PowerShell 5.1 turns stderr of native programs into terminating
    # errors under 'Stop' as soon as it is redirected; probing must not die.
    $ErrorActionPreference = 'Continue'
    $candidates = @(
        [pscustomobject]@{ Exe = 'py';      Extra = @('-3') },
        [pscustomobject]@{ Exe = 'python';  Extra = @() },
        [pscustomobject]@{ Exe = 'python3'; Extra = @() }
    )
    $probe = "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2]); print(int(sys.prefix != sys.base_prefix))"
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        $extra = $candidate.Extra
        try {
            $out = @(& $candidate.Exe @extra -c $probe 2>$null)
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out -or $out.Count -lt 3) { continue }
        return [pscustomobject]@{
            Executable = $out[0].Trim()
            Version    = [version]$out[1].Trim()
            InVenv     = ($out[2].Trim() -eq '1')
        }
    }
    return $null
}

if ($env:OS -ne 'Windows_NT') { Die "this installer is for Windows; on Linux use ./install.sh" }

# Anything not bound to a switch above ends up in $args. Without this check a
# mistyped option (e.g. "Uninstall" without the dash) would silently run a
# normal installation instead.
if ($args.Count -gt 0) {
    Die ("unknown argument(s): $($args -join ' ')`n" +
         "        options are -Uninstall, -NoAutostart, -NoStart (note the leading dash)")
}

# --- Uninstall ---------------------------------------------------------------

if ($Uninstall) {
    Stop-Mixer
    if (Test-Path $Shortcut) { Info "Removing autostart shortcut"; Remove-Item $Shortcut -Force }
    if (Test-Path $Target)   { Info "Removing $Target"; Remove-Item $Target -Force }
    Info "Done. Configuration in $ConfigDir was kept."
    exit 0
}

# --- Python ------------------------------------------------------------------

$python = Find-Python
if (-not $python) {
    Die "Python not found. Install Python $MinPython or newer from https://www.python.org/downloads/windows/ and run this again."
}
if ($python.Version -lt $MinPython) {
    Die "Python $($python.Version) found at $($python.Executable), but $MinPython or newer is needed."
}
$pythonw = Join-Path (Split-Path $python.Executable) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { Die "pythonw.exe not found next to $($python.Executable)" }
Info "Using Python $($python.Version): $($python.Executable)"

# --- Packages ----------------------------------------------------------------

$requirements = Join-Path $SourceDir 'requirements.txt'
if (-not (Test-Path $requirements)) { Die "requirements.txt not found in $SourceDir" }
Info "Installing Python packages"
$pipArgs = @('-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', '-r', $requirements)
if (-not $python.InVenv) { $pipArgs += '--user' }
& $python.Executable @pipArgs
if ($LASTEXITCODE -ne 0) { Die "pip install failed (see above)" }

# --- Script ------------------------------------------------------------------

Stop-Mixer
Info "Installing $ScriptName to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $SourceDir $ScriptName) -Destination $Target -Force

# --- Autostart ---------------------------------------------------------------

if ($NoAutostart) {
    if (Test-Path $Shortcut) { Info "Removing existing autostart shortcut"; Remove-Item $Shortcut -Force }
} else {
    Info "Creating autostart shortcut"
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Shortcut)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$Target`""
    $link.WorkingDirectory = $InstallDir
    $link.Description = 'nanoKONTROL2 mixer'
    $link.Save()
}

# --- Start -------------------------------------------------------------------

if (-not $NoStart) {
    Info "Starting the mixer"
    Start-Process -FilePath $pythonw -ArgumentList "`"$Target`"" -WorkingDirectory $InstallDir
}

Info "Done. Configuration and log: $ConfigDir"
if (Get-Process -Name 'NK2Tray' -ErrorAction SilentlyContinue) {
    Warn "NK2Tray is running. It holds the MIDI port - quit it (and remove it from autostart)."
}
