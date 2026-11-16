#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
for argument in "$@"; do
  if [[ "$argument" == --help || "$argument" == -h ]]; then
    if command -v python3 >/dev/null 2>&1; then
      exec python3 -m krakendf --help
    fi
    echo "KrakenDF: ./start.sh [--source live|simulation|idle] [--daq-mode managed|external]"
    echo "Erstinstallation separat: ./setup.sh. --no-setup überspringt den Abhängigkeitscheck."
    exit 0
  fi
done
app_args=()
bootstrap=true
source_mode="${KRAKENDF_SOURCE:-live}"
daq_mode="${KRAKENDF_DAQ_MODE:-managed}"
for argument in "$@"; do
  if [[ "$argument" == --no-setup ]]; then
    bootstrap=false
  else
    app_args+=("$argument")
  fi
done
for ((index=0; index<${#app_args[@]}; index++)); do
  case "${app_args[index]}" in
    --source) source_mode="${app_args[index+1]:-}" ;;
    --source=*) source_mode="${app_args[index]#--source=}" ;;
    --daq-mode) daq_mode="${app_args[index+1]:-}" ;;
    --daq-mode=*) daq_mode="${app_args[index]#--daq-mode=}" ;;
  esac
done
setup_args=()
if [[ "$source_mode" == simulation || "$source_mode" == idle || "$daq_mode" == external ]]; then
  setup_args+=(--app-only)
fi
if [[ "$bootstrap" == true ]]; then
  if ! bash ./setup.sh --check "${setup_args[@]}" >/dev/null 2>&1; then
    echo "Erststart: fehlende Abhängigkeiten werden eingerichtet."
    echo "Systempakete/Geräterechte benötigen gegebenenfalls Ihr sudo-Passwort."
    bash ./setup.sh "${setup_args[@]}"
  fi
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Die Python-Umgebung fehlt. Bitte ./setup.sh ausführen." >&2
  exit 1
fi
exec .venv/bin/python -m krakendf "${app_args[@]}"
