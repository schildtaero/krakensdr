#!/usr/bin/env bash
# Called from Windows setup.ps1 after WSL2 and a regular user are ready.
set -euo pipefail
umask 077

setup_arguments=()
for setup_argument in "$@"; do
  case "$setup_argument" in
    --app-only) setup_arguments+=(--app-only) ;;
    *) echo "Unbekannte WSL-Setup-Option: $setup_argument" >&2; exit 2 ;;
  esac
done

source_project="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
linux_project="${HOME}/.local/share/krakendf/project"
if [[ "$(id -u)" == "0" ]]; then
  echo "WSL-Setup bitte als regulaerer Linux-Benutzer ausfuehren." >&2
  exit 1
fi
if [[ -z "${WSL_DISTRO_NAME:-}" ]]; then
  echo "Dieses Hilfsskript wird aus setup.ps1 innerhalb von WSL aufgerufen." >&2
  exit 1
fi
mkdir -p -- "$linux_project"
if [[ "$source_project" != "$linux_project" ]]; then
  # Preserve the previously installed WSL runtime/venv and user data. These
  # must never be overwritten by a Windows interpreter's environment.
  tar -C "$source_project" \
    --exclude='./.venv' --exclude='./.runtime' --exclude='./.git' \
    --exclude='./build' --exclude='./dist' --exclude='./data' \
    --exclude='./.windows-setup.json' --exclude='./.windows-setup.json.tmp' \
    --exclude='*/__pycache__' --exclude='*.egg-info' \
    -cf - . | tar -C "$linux_project" -xf -
fi
cd -- "$linux_project"
bash ./setup.sh "${setup_arguments[@]}"
daq_installed=false
if [[ -f .runtime/manifest.json ]] && bash ./setup.sh --check >/dev/null 2>&1; then
  daq_installed=true
fi

# Only publish the dispatcher marker after all Linux setup steps succeed.
python3 - "$source_project/.windows-setup.json" "$linux_project" "$WSL_DISTRO_NAME" "$daq_installed" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tomllib

marker = Path(sys.argv[1])
temporary = marker.with_name(marker.name + ".tmp")
value = {
    "schema_version": 1,
    "mode": "wsl",
    "distribution": sys.argv[3],
    "project_linux_path": sys.argv[2],
    "source_version": tomllib.loads((Path(sys.argv[2]) / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
    "installation_complete": True,
    "daq_installed": sys.argv[4] == "true",
    "usb_ready": False,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
os.replace(temporary, marker)
PY
