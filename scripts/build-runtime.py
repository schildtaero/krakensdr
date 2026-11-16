#!/usr/bin/env python3
"""Reproducible local KrakenRF build; no downloads or privileged commands.

The shell bootstrap resolves OS/Python dependencies. This helper verifies the
shipped corresponding sources and writes ready=true only after a real build,
dynamic-link checks and Python import checks have all succeeded.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parent.parent
BINARIES = ("rtl_daq.out", "rebuffer.out", "decimate.out", "iq_server.out")
APP_MODULES = ("fastapi", "uvicorn", "pydantic", "numpy", "scipy", "openpyxl", "httpx", "serial")
DAQ_MODULES = ("zmq", "skrf", "numba")


def runtime_directory(root: Path = ROOT) -> Path:
    return Path(os.environ.get("KRAKENDF_RUNTIME", root / ".runtime")).expanduser().resolve()


def project_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def run(command, *, cwd=None, env=None, capture=False):
    print("+ " + shlex.join(map(str, command)), flush=True)
    return subprocess.run(list(map(str, command)), cwd=cwd, env=env, check=True,
                          text=True, capture_output=capture)


def verify_sources(root: Path = ROOT):
    manifest = json.loads((root / "vendor/SOURCE_MANIFEST.json").read_text())
    for source in manifest["sources"]:
        base = root / "vendor" / source["directory"]
        for relative, expected in source["sha256_files"].items():
            path = base / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"Vendored source differs from pinned manifest: {source['name']}/{relative}")
    return manifest


def build_fingerprint(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for relative in ("vendor/SOURCE_MANIFEST.json", "patches/heimdall-unity.patch",
                     "patches/krakendf_unity.h", "scripts/build-runtime.py",
                     "scripts/requirements-daq.txt"):
        digest.update((root / relative).read_bytes())
    digest.update(platform.machine().encode())
    return digest.hexdigest()


def python_errors(root: Path, app_only: bool) -> list[str]:
    executable = root / ".venv/bin/python"
    if not executable.is_file():
        return ["Missing project virtual environment (.venv/bin/python)"]
    modules = APP_MODULES + (() if app_only else DAQ_MODULES)
    code = (
        "import importlib, importlib.util, importlib.metadata, json; "
        f"names={modules!r}; "
        "missing=[n for n in names if importlib.util.find_spec(n) is None]; "
        "[importlib.import_module(n) for n in names if n not in missing]; "
        "print(json.dumps({'missing':missing,'version':importlib.metadata.version('kraken-df-demo')}))"
    )
    try:
        completed = subprocess.run([str(executable), "-c", code], text=True, capture_output=True, timeout=30)
        if completed.returncode:
            return ["App dependencies are incomplete: " + completed.stderr.strip()[-500:]]
        found = json.loads(completed.stdout)
        errors = ["Missing Python modules: " + ", ".join(found["missing"])] if found["missing"] else []
        if found["version"] != project_version(root):
            errors.append("Installed app version differs from project version")
        return errors
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return [f"Cannot verify project Python: {exc}"]


def runtime_errors(root: Path = ROOT, *, app_only=False) -> list[str]:
    errors = python_errors(root, app_only)
    if app_only:
        return errors
    runtime = runtime_directory(root)
    try:
        manifest = json.loads((runtime / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        return errors + [f"DAQ runtime is not installed: {exc}"]
    if manifest.get("schema_version") != 1 or manifest.get("ready") is not True:
        errors.append("DAQ runtime marker is not ready/schema 1")
    if manifest.get("build_fingerprint") != build_fingerprint(root):
        errors.append("DAQ runtime needs rebuilding for this source/patch/architecture")
    if manifest.get("architecture") != platform.machine():
        errors.append("DAQ runtime architecture differs from this machine")
    expected_python = (root / ".venv/bin/python").absolute()
    if Path(manifest.get("python", "")).absolute() != expected_python:
        errors.append("DAQ runtime points to a different virtual environment")
    firmware = runtime / manifest.get("firmware_dir", "firmware")
    for name in BINARIES:
        path = firmware / "_daq_core" / name
        if not path.is_file() or not os.access(path, os.X_OK):
            errors.append(f"Missing executable {path}")
    library = runtime / manifest.get("native_prefix", "native") / "lib/librtlsdr.so"
    if not library.is_file():
        errors.append("Missing bundled KrakenRF librtlsdr.so")
    return errors


def build(root: Path = ROOT, *, force=False):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise RuntimeError("Embedded native DAQ requires 64-bit Linux x86_64 or aarch64")
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
        raise RuntimeError("Embedded DAQ currently requires Python 3.11–3.13")
    source_manifest = verify_sources(root)
    runtime = runtime_directory(root)
    runtime.mkdir(parents=True, exist_ok=True)
    if not force and not runtime_errors(root):
        print("DAQ runtime already built and compatible.")
        return
    for program in ("cmake", "gcc", "pkg-config", "patch", "ldd"):
        if not shutil.which(program):
            raise RuntimeError(f"Missing build dependency {program}; run ./setup.sh")
    # Remove the readiness marker before mutation, so a failed build can never
    # be mistaken for a successfully prepared live runtime.
    (runtime / "manifest.json").unlink(missing_ok=True)
    native = runtime / "native"
    native.mkdir(exist_ok=True)
    build_dir = runtime / "build/librtlsdr"
    build_dir.mkdir(parents=True, exist_ok=True)
    run(["cmake", "-S", root / "vendor/librtlsdr", "-B", build_dir,
         f"-DCMAKE_INSTALL_PREFIX={native}", "-DLIB_INSTALL_DIR=lib",
         "-DINSTALL_UDEV_RULES=OFF", "-DDETACH_KERNEL_DRIVER=ON",
         "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib"])
    jobs = str(max(1, min(4, os.cpu_count() or 1)))
    run(["cmake", "--build", build_dir, "--parallel", jobs])
    run(["cmake", "--install", build_dir])
    staged = runtime / "build/heimdall"
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(root / "vendor/heimdall", staged)
    patched = staged / "Firmware/_daq_core/fir_decimate.c"
    patched.write_text(patched.read_text())  # Normalize upstream CRLF for the reviewable patch.
    run(["patch", "--batch", "--forward", "-p1", "-i", root / "patches/heimdall-unity.patch"], cwd=staged)
    shutil.copy2(root / "patches/krakendf_unity.h", patched.parent / "krakendf_unity.h")
    core = patched.parent
    pkg_flags = shlex.split(run(["pkg-config", "--cflags", "--libs", "libusb-1.0", "libzmq"], capture=True).stdout)
    include_flags = [f"-I{native / 'include'}", f"-L{native / 'lib'}", "-Wl,-rpath,$ORIGIN/../../native/lib"]
    common = ["gcc", "-Wall", "-std=gnu99", "-O2", "-I.", *include_flags]
    for unit in ("ini", "log", "iq_header", "sh_mem_util"):
        run([*common, "-c", f"{unit}.c", "-o", f"{unit}.o"], cwd=core)
    objects = ["sh_mem_util.o", "log.o", "ini.o", "iq_header.o"]
    run([*common, "rtl_daq.c", *objects, "-o", "rtl_daq.out", "-lpthread", "-lrtlsdr", "-lm", *pkg_flags], cwd=core)
    run([*common, "rebuffer.c", *objects, "-o", "rebuffer.out", "-lrt", "-lm"], cwd=core)
    run([*common, "iq_server.c", *objects, "-o", "iq_server.out", "-lrt"], cwd=core)
    run([*common, "-DKRAKENDF_UNITY_ONLY", "fir_decimate.c", *objects, "-o", "decimate.out", "-lrt", "-lm"], cwd=core)
    env = {**os.environ, "LD_LIBRARY_PATH": str(native / "lib") + ":" + os.environ.get("LD_LIBRARY_PATH", "")}
    for binary in BINARIES:
        result = run(["ldd", core / binary], env=env, capture=True)
        if "not found" in result.stdout:
            raise RuntimeError(f"Unresolved shared library in {binary}: {result.stdout}")
    python = root / ".venv/bin/python"
    run([python, "-c", "import numpy,scipy,zmq,skrf,numba,serial; print('DAQ Python imports: OK')"], env=env)
    firmware = runtime / "firmware"
    old = runtime / "firmware.previous"
    if old.exists():
        shutil.rmtree(old)
    if firmware.exists():
        firmware.rename(old)
    shutil.copytree(staged / "Firmware", firmware,
                    ignore=shutil.ignore_patterns("*.o", "*.log", "__pycache__"))
    marker = {
        "schema_version": 1, "ready": True, "firmware_dir": "firmware",
        "native_prefix": "native", "python": str(python.absolute()),
        "architecture": platform.machine(), "python_version": platform.python_version(),
        "build_fingerprint": build_fingerprint(root), "built_at": time.time(),
        "app_version": project_version(root), "native_decimation_ratio": 1,
        "source_pins": {s["name"]: s["commit"] for s in source_manifest["sources"]},
    }
    temporary = runtime / "manifest.json.tmp"
    temporary.write_text(json.dumps(marker, indent=2) + "\n")
    os.replace(temporary, runtime / "manifest.json")
    if old.exists():
        shutil.rmtree(old)
    print("Native DAQ build complete. USB/coherence readiness is checked at live start.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--app-only", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            errors = runtime_errors(app_only=args.app_only)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("App dependencies ready." if args.app_only else "App and embedded DAQ runtime ready (USB not checked).")
        elif not args.app_only:
            build(force=args.rebuild)
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"DAQ setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
