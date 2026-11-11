#!/usr/bin/env bash
# Build and upload the test or production firmware of a CapTest board (PlatformIO).
# usage: flash_firmware.sh --board ID --firmware test|production [--port PORT] [--dry-run] [--yes]
#
# Reads the firmware project, PlatformIO environment and production project of the board
# from CapTest/boards.json. Only uploads a firmware image: nothing else on the Teensy is
# erased and neither firmware keeps EEPROM/flash state, so switching loses nothing.
# Never flashes the teensy41_profile / teensy41_pcbdiag environments.
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
captest_dir="$(cd "$here/.." && pwd)"
repo_root="$(cd "$captest_dir/.." && pwd)"

BOARD=""; FIRMWARE=""; PORT=""; DRY_RUN=0; YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --board|-b) BOARD="$2"; shift 2 ;;
        --firmware|-f) FIRMWARE="$2"; shift 2 ;;
        --port|-p) PORT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --yes|-y) YES=1; shift ;;
        -h|--help) echo "usage: $0 --board ID --firmware test|production [--port PORT] [--dry-run] [--yes]"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$BOARD" ] && [ -n "$FIRMWARE" ] || { echo "--board and --firmware are required" >&2; exit 2; }
case "$FIRMWARE" in test|production) ;; *) echo "--firmware must be test or production" >&2; exit 2 ;; esac

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "python is needed to read boards.json" >&2; exit 2; }

# boards.json -> "project<TAB>env<TAB>title" of the requested firmware (or an error).
selection="$("$PY" - "$captest_dir/boards.json" "$BOARD" "$FIRMWARE" <<'PYEOF'
import json, sys
path, board, which = sys.argv[1:4]
entries = json.load(open(path, encoding="utf-8"))["boards"]
entry = next((e for e in entries if e["id"] == board), None)
if entry is None:
    sys.exit("Unknown board '%s'. Known: %s" % (board, ", ".join(e["id"] for e in entries)))
fw = entry["firmware"]
if which == "test":
    print("\t".join([fw["test_project"], fw["test_env"], fw["firmware_name"] + " (capacitance-test firmware)"]))
else:
    print("\t".join([fw["production_project"], fw["production_env"], board + " (PRODUCTION firmware)"]))
PYEOF
)" || { echo "$selection" >&2; exit 2; }
IFS=$'\t' read -r project env_name title <<<"$selection"
case "$env_name" in *pcbdiag*|*profile*) echo "Refusing to flash the '$env_name' environment from this script." >&2; exit 2 ;; esac
project_dir="$(cd "$repo_root/$project" && { pwd -W 2>/dev/null || pwd; })"
platform="$(grep -E '^\s*platform\s*=' "$project_dir/platformio.ini" | head -n1 | tr -d ' ')"

teensy_ports() {
    command -v pio >/dev/null 2>&1 || return 0
    pio device list --json-output 2>/dev/null | "$PY" -c '
import json, sys
try:
    for d in json.load(sys.stdin):
        if "VID:PID=16C0:0483" in d.get("hwid", ""):
            print(d["port"])
except Exception:
    pass
'
}

if [ -z "$PORT" ]; then
    ports="$(teensy_ports)"
    count="$(printf '%s' "$ports" | grep -c . || true)"
    if [ "$count" -eq 1 ]; then
        PORT="$ports"
    elif [ "$count" -gt 1 ]; then
        if [ "$YES" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
            echo "More than one Teensy found ($(echo $ports)). Pass --port." >&2
            exit 2
        fi
        echo "More than one Teensy found: $(echo $ports)"
        read -r -p "Port to flash: " PORT
        printf '%s\n' "$ports" | grep -qx "$PORT" || { echo "Unknown port '$PORT'." >&2; exit 2; }
    else
        echo "No Teensy serial port detected. The uploader still finds a Teensy that is in"
        echo "bootloader mode (press its program button once)."
    fi
fi

args=(run -d "$project_dir" -e "$env_name" -t upload)
[ -n "$PORT" ] && args+=(--upload-port "$PORT")

echo "=============================================================="
echo "Flashing : $title"
echo "Project  : $project_dir"
echo "Env      : $env_name ($platform)"
echo "Port     : ${PORT:-(auto: Teensy bootloader)}"
echo "Scope    : builds and uploads the firmware image only; nothing else is erased."
echo "=============================================================="
echo "Command  : pio ${args[*]}"

if [ "$DRY_RUN" -eq 1 ]; then echo "(dry run: nothing executed)"; exit 0; fi

if [ "$YES" -ne 1 ]; then
    read -r -p "Proceed? [y/N] " answer
    case "$answer" in [yY]*) ;; *) echo "Cancelled."; exit 1 ;; esac
fi
code=0
pio "${args[@]}" || code=$?
hex="$project_dir/.pio/build/$env_name/firmware.hex"
if [ -f "$hex" ]; then
    echo "Image    : $hex"
    echo "SHA-256  : $(sha256sum "$hex" | cut -d' ' -f1)"
fi
if [ "$code" -ne 0 ]; then
    echo
    echo "UPLOAD FAILED. If the Teensy bootloader did not respond, press the program"
    echo "button on the Teensy once (the LED turns off) and run this script again."
    exit "$code"
fi
echo "Upload finished. The board re-enumerates as a USB serial port in a few seconds."
