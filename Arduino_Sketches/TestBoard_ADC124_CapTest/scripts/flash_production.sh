#!/usr/bin/env bash
# Build and upload the production TestBoard_ADC124 firmware (env teensy41, pinned platform).
# Thin wrapper over CapTest/scripts/flash_firmware.sh (board and environment come from CapTest/boards.json).
# usage: flash_production.sh [--port PORT] [--dry-run] [--yes]
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$here/../../../CapTest/scripts/flash_firmware.sh" --board TestBoard_ADC124 --firmware production "$@"
