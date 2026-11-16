# Project instructions

## Repository and synchronization

- Canonical repository: https://github.com/schildtaero/krakensdr.git, branch `main`.
- The user explicitly requested that this project be kept synchronized there. Commit and push completed, appropriately verified changes as part of future work on this project. This authorization persists; no additional publishing confirmation is needed for ordinary project updates.
- Synchronize during active work. Do not claim a background daemon or continuous synchronization exists.
- Read the current checkout and remote state before changing it. Preserve user edits and remote history. Do not force-push or silently replace unrelated changes.
- If authentication prevents pushing, finish and preserve the local commit, report the actual failure, and request reconnection. Never request credentials in chat or embed tokens in commands, files or logs.

## Implementation and verification

- Local FastAPI application with an offline-capable web UI. The default source is **live**. Simulation is an explicit selection; do not restore the abandoned automatic simulation-fallback change without user authorization.
- Windows local USB reception uses WSL2; native Windows mode is for an external Linux DAQ. Do not claim native Windows USB support or physically validated RF accuracy.
- DAQ subprocesses must use the venv interpreter path without resolving its symlink to system Python.
- Keep vendored sources and license notices intact. Changes belong in documented patches; verify `vendor/SOURCE_MANIFEST.json`.
- Run relevant tests. Full software suite: `OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python -m unittest discover -s tests -v`. Actual hardware is not required. Optional external capture tests may skip when their fixture is absent.
- Frontend syntax: `node --check krakendf/static/app.js` and `node --check krakendf/static/map.js`. Shell syntax: `bash -n start.sh setup.sh scripts/setup-wsl.sh`.
- Native DAQ build verification: `./setup.sh --skip-system` after required system development dependencies have been installed; normal first use is `./setup.sh`.
- Keep README, package version and verification notes consistent. Changes to WSL-staged application sources need an appropriate project version bump so Windows updates its Linux copy.

## Files to retain locally

- Never commit `.venv`, `.runtime`, recordings, IQ captures, maps/cache, setup markers, machine-specific configuration, credentials, logs, backup or temporary repair files.
- The `config/` antenna presets and their packaged `krakendf/resources/` copies are source files and must stay consistent.
