<#
.SYNOPSIS
  Build and upload the test or production firmware of a CapTest board (PlatformIO).
.DESCRIPTION
  Reads the firmware project, PlatformIO environment and production project of the board
  from CapTest/boards.json. Only uploads a firmware image: nothing else on the Teensy is
  erased and neither firmware keeps EEPROM/flash state, so switching back and forth loses
  nothing. Never flashes the teensy41_profile / teensy41_pcbdiag environments.
.PARAMETER Board
  Board id from boards.json (for example TestBoard_ADC124).
.PARAMETER Firmware
  test (the CapTest firmware) or production.
.PARAMETER Port
  Serial port of the Teensy (default: auto-detect; asks when more than one).
.PARAMETER DryRun
  Print what would be executed, change nothing.
.PARAMETER Yes
  Skip the confirmation prompt.
#>
param(
    [Parameter(Mandatory = $true)][string]$Board,
    [Parameter(Mandatory = $true)][ValidateSet('test', 'production')][string]$Firmware,
    [string]$Port = '',
    [switch]$DryRun,
    [switch]$Yes
)

$captestDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repoRoot = (Resolve-Path (Join-Path $captestDir '..')).Path
$registry = Get-Content -Raw -Path (Join-Path $captestDir 'boards.json') | ConvertFrom-Json
$entry = $registry.boards | Where-Object { $_.id -eq $Board } | Select-Object -First 1
if (-not $entry) {
    Write-Error ("Unknown board '$Board'. Known: " + (($registry.boards | ForEach-Object { $_.id }) -join ', '))
    exit 2
}
$fw = $entry.firmware
if ($Firmware -eq 'test') {
    $project = $fw.test_project; $environment = $fw.test_env
    $title = "$($fw.firmware_name) (capacitance-test firmware)"
} else {
    $project = $fw.production_project; $environment = $fw.production_env
    $title = "$Board (PRODUCTION firmware)"
}
if ($environment -match 'pcbdiag|profile') {
    Write-Error "Refusing to flash the '$environment' environment from this script."
    exit 2
}
$projectDir = (Resolve-Path (Join-Path $repoRoot $project)).Path
$platform = (Select-String -Path (Join-Path $projectDir 'platformio.ini') -Pattern '^\s*platform\s*=' |
    Select-Object -First 1).Line.Trim()

function Get-TeensyPorts {
    $pio = Get-Command pio -ErrorAction SilentlyContinue
    if (-not $pio) { return @() }
    try {
        $json = & pio device list --json-output 2>$null | Out-String
        $devices = $json | ConvertFrom-Json
    } catch { return @() }
    # PJRC Teensy USB serial: VID 16C0, PID 0483.
    return @($devices | Where-Object { $_.hwid -match 'VID:PID=16C0:0483' } | ForEach-Object { $_.port })
}

if (-not $Port) {
    $ports = @(Get-TeensyPorts)
    if ($ports.Count -eq 1) {
        $Port = $ports[0]
    } elseif ($ports.Count -gt 1) {
        if ($Yes -or $DryRun) {
            Write-Error "More than one Teensy found ($($ports -join ', ')). Pass -Port COMx."
            exit 2
        }
        Write-Host "More than one Teensy found: $($ports -join ', ')"
        $Port = Read-Host 'Port to flash'
        if ($ports -notcontains $Port) { Write-Error "Unknown port '$Port'."; exit 2 }
    } else {
        Write-Host 'No Teensy serial port detected. The uploader still finds a Teensy that is in'
        Write-Host 'bootloader mode (press its program button once).'
    }
}

$uploadArgs = @('run', '-d', $projectDir, '-e', $environment, '-t', 'upload')
if ($Port) { $uploadArgs += @('--upload-port', $Port) }

Write-Host '=============================================================='
Write-Host "Flashing : $title"
Write-Host "Project  : $projectDir"
Write-Host "Env      : $environment ($platform)"
Write-Host "Port     : $(if ($Port) { $Port } else { '(auto: Teensy bootloader)' })"
Write-Host 'Scope    : builds and uploads the firmware image only; nothing else is erased.'
Write-Host '=============================================================='
Write-Host ("Command  : pio " + ($uploadArgs -join ' '))

if ($DryRun) { Write-Host '(dry run: nothing executed)'; exit 0 }

if (-not $Yes) {
    $answer = Read-Host 'Proceed? [y/N]'
    if ($answer -notmatch '^[yY]') { Write-Host 'Cancelled.'; exit 1 }
}
& pio @uploadArgs
$code = $LASTEXITCODE
$hex = Join-Path $projectDir ".pio/build/$environment/firmware.hex"
if (Test-Path $hex) {
    $hash = (Get-FileHash -Algorithm SHA256 $hex).Hash.ToLower()
    Write-Host "Image    : $hex"
    Write-Host "SHA-256  : $hash"
}
if ($code -ne 0) {
    Write-Host ''
    Write-Host 'UPLOAD FAILED. If the Teensy bootloader did not respond, press the program'
    Write-Host 'button on the Teensy once (the LED turns off) and run this script again.'
    exit $code
}
Write-Host 'Upload finished. The board re-enumerates as a USB serial port in a few seconds.'
exit 0
