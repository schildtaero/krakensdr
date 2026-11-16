#!/usr/bin/env bash
# First-use bootstrap for Debian/Ubuntu Linux. Normal launches do not need root.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$task_root"
check_only=0
app_only=0
skip_system=0
no_device_rules=0
rebuild=0
dry_run=0
for argument in "$@"; do
  case "$argument" in
    --check) check_only=1 ;;
    --app-only|--python-only) app_only=1 ;;
    --skip-system) skip_system=1 ;;
    --no-device-rules) no_device_rules=1 ;;
    --rebuild) rebuild=1 ;;
    --dry-run) dry_run=1 ;;
    --help|-h)
      cat <<'HELP'
Usage: bash ./setup.sh [--check] [--app-only] [--skip-system] [--rebuild]
  default            Resolve missing OS/Python packages and build embedded DAQ.
  --check            Read-only readiness check; no USB required, no installation.
  --app-only         Python/UI/GPS only, for simulation or external Heimdall.
  --skip-system      Do not use apt or install udev rules (expert/CI mode).
  --no-device-rules  Resolve/build normally but skip USB/GPS permission rules.
  --rebuild          Recompile the native DAQ even if its marker matches.
  --dry-run          Print intended steps without changing files or packages.
Supported full stack: 64-bit Linux x86_64/aarch64, Debian 12+/Ubuntu 24.04+,
Python 3.11–3.13. Start this script as the normal user; it requests sudo only
for missing system packages and narrowly scoped USB permission rules.
HELP
      exit 0 ;;
    *) printf 'Unknown setup option: %s\n' "$argument" >&2; exit 2 ;;
  esac
done

task_python="${KRAKENDF_PYTHON:-python3}"
if [[ -x "$task_root/.venv/bin/python" ]]; then task_python="$task_root/.venv/bin/python"; fi
if [[ "$check_only" == 1 ]]; then
  command -v "$task_python" >/dev/null 2>&1 || { echo 'Python 3.11+ is missing.' >&2; exit 1; }
  flags=(--check)
  if [[ "$app_only" == 1 ]]; then flags+=(--app-only); fi
  exec "$task_python" "$task_root/scripts/build-runtime.py" "${flags[@]}"
fi

trap 'code=$?; printf "Setup stopped (exit %s, line %s). No live-readiness success is implied.\n" "$code" "$LINENO" >&2; exit "$code"' ERR
if [[ "$(uname -s)" != Linux ]]; then
  echo 'Use setup.ps1 on Windows (WSL2), or run this script on Linux.' >&2; exit 1
fi
if [[ "$app_only" == 0 ]]; then
  case "$(uname -m)" in x86_64|aarch64) ;; *) echo 'Embedded DAQ requires x86_64 or aarch64 Linux.' >&2; exit 1 ;; esac
fi
if [[ "$dry_run" == 1 ]]; then
  echo "Project: $task_root"
  echo '1. Install missing apt build/Python packages (sudo only if needed).'
  echo '2. Create .venv and resolve project Python dependencies.'
  if [[ "$app_only" == 0 ]]; then
    echo '3. Verify bundled source hashes and build Kraken librtlsdr + Heimdall in .runtime.'
    echo '4. Install targeted Kraken/u-blox udev rules and finite USB memory helper.'
  fi
  echo 'No files, packages, USB devices or system settings were changed.'
  exit 0
fi

run_root() {
  if [[ "$(id -u)" == 0 ]]; then "$@"; else
    command -v sudo >/dev/null || { echo 'sudo is required for missing system packages; ask the host administrator or use --skip-system.' >&2; return 1; }
    sudo "$@"
  fi
}

if [[ "$skip_system" == 0 ]]; then
  if ! command -v apt-get >/dev/null; then echo 'Automatic package resolution currently needs Debian/Ubuntu apt. Use --skip-system after installing documented dependencies.' >&2; exit 1; fi
  # No repository or distribution upgrade: install only absent package names.
  packages=(python3 python3-venv ca-certificates)
  if [[ "$app_only" == 0 ]]; then packages+=(build-essential cmake pkg-config patch libusb-1.0-0-dev libzmq3-dev python3-dev udev usbutils); fi
  missing=()
  for package in "${packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then missing+=("$package"); fi
  done
  if ((${#missing[@]})); then
    printf 'Installing missing system packages: %s\n' "${missing[*]}"
    run_root apt-get update
    run_root apt-get install -y --no-install-recommends "${missing[@]}"
  fi
fi

command -v "$task_python" >/dev/null || { echo 'python3 is missing.' >&2; exit 1; }
"$task_python" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required (Debian12+/Ubuntu24.04+)."'
if [[ "$app_only" == 0 ]]; then "$task_python" -c 'import sys; assert sys.version_info < (3,14), "Embedded DAQ currently supports Python3.11–3.13; select KRAKENDF_PYTHON."'; fi
if [[ ! -x "$task_root/.venv/bin/python" ]]; then "$task_python" -m venv "$task_root/.venv"; fi
task_python="$task_root/.venv/bin/python"
if ! "$task_python" -m pip --version >/dev/null 2>&1; then
  "$task_python" -m ensurepip --upgrade
fi
if [[ "$app_only" == 1 ]]; then
  "$task_python" -m pip install --disable-pip-version-check "$task_root[gps]"
else
  "$task_python" -m pip install --disable-pip-version-check "$task_root[daq]" -r "$task_root/scripts/requirements-daq.txt"
  build_flags=()
  if [[ "$rebuild" == 1 ]]; then build_flags+=(--rebuild); fi
  "$task_python" "$task_root/scripts/build-runtime.py" "${build_flags[@]}"
fi

if [[ "$app_only" == 0 && "$skip_system" == 0 && "$no_device_rules" == 0 ]]; then
  task_user="${SUDO_USER:-$(id -un)}"
  if [[ "$task_user" == root ]]; then
    echo 'Device rules need a normal operator account. Rerun setup without sudo, or use --no-device-rules.' >&2; exit 1
  fi
  task_rules="$(mktemp)"
  "$task_python" "$task_root/scripts/device-rules.py" --user "$task_user" > "$task_rules"
  echo 'Installing /etc/udev/rules.d/70-krakendf.rules (factory serials1000..1004 and u-blox1546 only).'
  echo 'Installing /usr/lib/udev/krakendf-usb-memory (raises USB-transfer budget to256MiB when Kraken is attached).'
  run_root install -m 0644 "$task_rules" /etc/udev/rules.d/70-krakendf.rules
  rm -f "$task_rules"
  run_root install -m 0755 "$task_root/scripts/krakendf-usb-memory" /usr/lib/udev/krakendf-usb-memory
  run_root /usr/lib/udev/krakendf-usb-memory
  if run_root udevadm control --reload-rules; then
    run_root udevadm trigger --subsystem-match=usb --action=add
    run_root udevadm trigger --subsystem-match=tty --action=add
    run_root udevadm settle --timeout=10
  else
    echo 'udev is not running; start its service/reconnect USB before live use. WSL2 needs USB attachment via usbipd.' >&2
  fi
fi

check_flags=(--check)
if [[ "$app_only" == 1 ]]; then check_flags+=(--app-only); fi
"$task_python" "$task_root/scripts/build-runtime.py" "${check_flags[@]}"
echo 'Setup complete. Dependencies installed; physical USB/coherence readiness is checked when starting Live.'
