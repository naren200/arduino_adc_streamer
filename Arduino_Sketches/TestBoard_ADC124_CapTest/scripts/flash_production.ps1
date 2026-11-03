<#
.SYNOPSIS
  Build and upload the production TestBoard_ADC124 firmware (env teensy41, pinned platform).
  Thin wrapper over CapTest/scripts/flash_firmware.ps1 (board and environment come from CapTest/boards.json).
.PARAMETER Port
  Serial port of the Teensy (default: auto-detect; asks when more than one).
.PARAMETER DryRun
  Print what would be executed, change nothing.
.PARAMETER Yes
  Skip the confirmation prompt.
#>
param([string]$Port = '', [switch]$DryRun, [switch]$Yes)
$shared = Join-Path $PSScriptRoot '../../../CapTest/scripts/flash_firmware.ps1'
& $shared -Board 'TestBoard_ADC124' -Firmware 'production' -Port $Port -DryRun:$DryRun -Yes:$Yes
exit $LASTEXITCODE
