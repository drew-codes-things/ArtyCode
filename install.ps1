[CmdletBinding()]
param([switch]$Uninstall)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MinPythonMinor = 11
$AppHome = if ($env:ARTY_CLI_HOME) { $env:ARTY_CLI_HOME } else { Join-Path $env:LOCALAPPDATA 'arty-cli' }
$VenvDir = Join-Path $AppHome 'venv'
$BinDir = Join-Path $AppHome 'bin'
$CommandNames = @('arty', 'arty-cli')
$UserEnvironmentKey = 'HKCU:\Environment'
$RefreshVariable = 'ARTY_CLI_PATH_REFRESH'
$PythonCandidates = @(@('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'), @('py', '-3'), @('python'), @('python3'))

function Stop-Install([string]$Message) {
    throw "arty-cli install: $Message"
}

function Invoke-Native([string]$Program, [string[]]$Arguments, [string]$Doing) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "$Doing failed (exit code $LASTEXITCODE)."
    }
}

function Test-PythonCommand([string[]]$Candidate) {
    $program = $Candidate[0]
    $rest = @($Candidate | Select-Object -Skip 1)
    if (-not (Get-Command $program -CommandType Application -ErrorAction SilentlyContinue)) {
        return $false
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $program @rest -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, $MinPythonMinor) else 1)" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Find-Python {
    foreach ($candidate in $PythonCandidates) {
        if (Test-PythonCommand $candidate) {
            $rest = @($candidate | Select-Object -Skip 1)
            $found = & $candidate[0] @rest -c 'import sys; print(sys.executable)'
            if ($LASTEXITCODE -eq 0 -and $found) {
                return ([string]$found).Trim()
            }
        }
    }
    Stop-Install "Python 3.$MinPythonMinor or newer is needed. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH') or with: winget install Python.Python.3.13"
}

function Get-ScriptFolder {
    if (-not $PSScriptRoot) {
        return ''
    }
    $hasProject = Test-Path (Join-Path $PSScriptRoot 'pyproject.toml')
    $hasPackage = Test-Path (Join-Path $PSScriptRoot 'arty_cli')
    if ($hasProject -and $hasPackage) {
        return $PSScriptRoot
    }
    return ''
}

function Get-Source {
    if ($env:ARTY_CLI_SOURCE) {
        return $env:ARTY_CLI_SOURCE
    }
    $here = Get-ScriptFolder
    if (-not $here) {
        Stop-Install 'no source to install from. Run install.ps1 from an arty-cli folder, or set ARTY_CLI_SOURCE to a folder, a .tar.gz/.zip URL or a git URL.'
    }
    return $here
}

function Get-PackageSpec([string]$Source) {
    if ($Source -match '^http://' -or $Source -match '^git\+http://') {
        Stop-Install "$Source uses plain http; use an https address so the download can't be changed on the way."
    }
    if ($Source -like 'git+*') {
        return "arty-cli @ $Source"
    }
    if ($Source -match '^https?://') {
        if ($Source -like '*.git') {
            return "arty-cli @ git+$Source"
        }
        return "arty-cli @ $Source"
    }
    if (-not (Test-Path (Join-Path $Source 'pyproject.toml'))) {
        Stop-Install "$Source is not an arty-cli folder (no pyproject.toml)."
    }
    $folder = (Resolve-Path -LiteralPath $Source).ProviderPath
    return "$folder"
}

function Get-UserPathEntries {
    $key = Get-Item -LiteralPath $UserEnvironmentKey
    $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    return @($raw -split ';' | Where-Object { $_.Trim() })
}

function Test-SameFolder([string]$Left, [string]$Right) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Left).Trim().TrimEnd('\')
    return [string]::Equals($expanded, $Right.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
}

function Save-UserPath([string[]]$Entries) {
    Set-ItemProperty -LiteralPath $UserEnvironmentKey -Name 'Path' -Value ($Entries -join ';') -Type ExpandString
    [Environment]::SetEnvironmentVariable($RefreshVariable, '1', 'User')
    [Environment]::SetEnvironmentVariable($RefreshVariable, $null, 'User')
}

function Add-BinToUserPath {
    $entries = Get-UserPathEntries
    $present = @($entries | Where-Object { Test-SameFolder $_ $BinDir })
    if ($present.Count -eq 0) {
        Save-UserPath (@($entries) + $BinDir)
        Write-Host "Added $BinDir to your PATH. Terminals opened from now on will find arty."
    }
    $sessionEntries = @($env:Path -split ';' | Where-Object { $_ -and -not (Test-SameFolder $_ $BinDir) })
    $env:Path = (@($BinDir) + $sessionEntries) -join ';'
}

function Remove-BinFromUserPath {
    $entries = Get-UserPathEntries
    $kept = @($entries | Where-Object { -not (Test-SameFolder $_ $BinDir) })
    if ($kept.Count -ne $entries.Count) {
        Save-UserPath $kept
    }
}

function Write-Shims {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    foreach ($name in $CommandNames) {
        $shim = "@echo off`r`n`"%~dp0..\venv\Scripts\$name.exe`" %*`r`n"
        [IO.File]::WriteAllText((Join-Path $BinDir "$name.cmd"), $shim, [Text.Encoding]::ASCII)
    }
}

function Write-ClashWarning {
    $other = Get-Command 'arty' -ErrorAction SilentlyContinue | Where-Object {
        $_.Source -and -not (Test-SameFolder (Split-Path -Parent $_.Source) $BinDir)
    } | Select-Object -First 1
    if ($other) {
        Write-Warning "Another 'arty' is already on your PATH at $($other.Source). New terminals put arty-cli's folder last, so that one may win; use arty-cli instead, or move $BinDir earlier in your PATH."
    }
}

function Install-ArtyCli {
    $python = Find-Python
    $source = Get-Source
    $spec = Get-PackageSpec $source
    Write-Host "Installing ArtyCode from $source into $AppHome"
    Write-ClashWarning
    New-Item -ItemType Directory -Force -Path $AppHome | Out-Null
    $venvPython = Join-Path $VenvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-Native $python @('-m', 'venv', $VenvDir) 'Creating the virtual environment'
    }
    Invoke-Native $venvPython @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip') 'Updating pip'
    Invoke-Native $venvPython @('-m', 'pip', 'install', '--quiet', '--upgrade', $spec) 'Installing arty-cli'
    Write-Shims
    Add-BinToUserPath
    $version = & (Join-Path $BinDir 'arty-cli.cmd') --version
    if ($LASTEXITCODE -ne 0) {
        Stop-Install 'ArtyCode was installed but does not start; run it from a new terminal to see why.'
    }
    Write-Host "Installed $version."
    Write-Host 'Type arty to start (the first run asks how to log in; arty login changes it later).'
}

function Uninstall-ArtyCli {
    Remove-BinFromUserPath
    foreach ($folder in @($BinDir, $VenvDir)) {
        if (Test-Path -LiteralPath $folder) {
            try {
                Remove-Item -LiteralPath $folder -Recurse -Force
            } catch {
                Stop-Install "couldn't remove $folder ($($_.Exception.Message)). Close every arty window and try again."
            }
        }
    }
    if ((Test-Path -LiteralPath $AppHome) -and -not (Get-ChildItem -LiteralPath $AppHome -Force)) {
        Remove-Item -LiteralPath $AppHome -Force
    }
    Write-Host 'Removed ArtyCode. Your settings and conversations in %APPDATA%\arty-cli were kept.'
}

if ($Uninstall) {
    Uninstall-ArtyCli
} else {
    Install-ArtyCli
}
