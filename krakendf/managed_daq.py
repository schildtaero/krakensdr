"""Own-process launcher for the bundled, locally installed Heimdall runtime.

The application's supervisor replaces the upstream shell launcher's global
pkill, sudo, host-cache flushing and unconditional shared-memory removal. It
does not claim RF coherence: the existing IQ adapter validates actual frames.
"""
from __future__ import annotations

import asyncio
from configparser import ConfigParser
import ctypes
try:
    import fcntl
except ImportError:  # Native Windows can still use a remote Linux DAQ.
    fcntl = None
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import time
from typing import Any
import uuid

from .hardware import HardwareError, MANUAL_GAINS_DB


SHM_NAMES = tuple(f"{name}_{suffix}" for name in (
    "decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc"
) for suffix in ("A", "B"))
FIFO_NAMES = tuple(f"{direction}_{name}" for name in (
    "decimator_in", "decimator_out", "delay_sync_iq", "delay_sync_hwc"
) for direction in ("fw", "bw"))
REQUIRED_BINARIES = ("rtl_daq.out", "rebuffer.out", "decimate.out", "iq_server.out")
REQUIRED_PYTHON = ("delay_sync.py", "hw_controller.py", "shmemIface.py", "iq_header.py")
DAQ_PORTS = (5000, 5001, 1130)


def _resolve(base: Path, value: str | None, default: str) -> Path:
    path = Path(value if value else default)
    return path if path.is_absolute() else base / path


def listening_ports() -> set[int]:
    """Read local LISTEN sockets without connecting to the single-client IQ port."""
    ports = set()
    read_any = False
    for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(filename).read_text().splitlines()[1:]
            read_any = True
        except (FileNotFoundError, PermissionError):
            continue
        for row in rows:
            fields = row.split()
            if len(fields) > 3 and fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    if not read_any:
        raise HardwareError("Lokale TCP-Portbelegung kann nicht aus /proc/net/tcp geprüft werden.")
    return ports


def discover_rtl_devices(native_prefix: Path) -> list[dict[str, Any]]:
    """Enumerate the pinned librtlsdr without opening, tuning or writing EEPROM."""
    candidates = (native_prefix / "lib" / "librtlsdr.so", native_prefix / "lib64" / "librtlsdr.so")
    library_path = next((p for p in candidates if p.is_file()), None)
    if library_path is None:
        raise HardwareError("Kraken-librtlsdr fehlt. Bitte setup.sh erneut ausführen.")
    try:
        library = ctypes.CDLL(str(library_path))
        count_fn = library.rtlsdr_get_device_count
        count_fn.restype = ctypes.c_uint32
        strings = library.rtlsdr_get_device_usb_strings
        strings.argtypes = [ctypes.c_uint32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
        strings.restype = ctypes.c_int
        count = int(count_fn())
        if count > 128:
            raise HardwareError("Unplausible RTL-SDR-Geräteanzahl; Runtimeinstallation prüfen.")
        devices = []
        for index in range(count):
            maker, product, serial = (ctypes.create_string_buffer(256) for _ in range(3))
            result = strings(index, maker, product, serial)
            devices.append({"index": index, "accessible": result == 0,
                            "manufacturer": maker.value.decode("utf-8", "replace"),
                            "product": product.value.decode("utf-8", "replace"),
                            "serial": serial.value.decode("ascii", "replace")})
        return devices
    except (OSError, AttributeError) as exc:
        raise HardwareError(f"Kraken-librtlsdr konnte nicht geladen werden: {exc}") from exc


class ManagedDAQ:
    """Supervise only the children launched by this instance, without sudo.

    The runtime manifest is produced only after setup succeeds. A separate
    per-user file lock, TCP and shared-memory preflight avoid trampling another
    DAQ. Unknown existing shared-memory segments are never removed. Segments
    first observed during this launch are removed at stop only if the exact
    inode still exists and was also held open by one of our direct children.
    """

    def __init__(self, runtime_dir: Path, log_dir: Path):
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.log_dir = Path(log_dir).expanduser().resolve()
        self.shm_dir = Path("/dev/shm")
        self.startup_timeout = 45.0
        self.stop_timeout = 3.0
        self._state = "stopped"
        self._message = "Integrierter DAQ-Stack ist angehalten."
        self._children: dict[str, asyncio.subprocess.Process] = {}
        self._logs: list[Any] = []
        self._lock_stream = None
        self._owned_shm: dict[Path, tuple[int, int]] = {}
        self._config = None
        self._run_dir = None
        self._manifest = None
        self._cleanup_warnings = []
        self._lifecycle = asyncio.Lock()

    def status(self) -> dict:
        installed = (self.runtime_dir / "manifest.json").is_file()
        process_state = {name: {"pid": process.pid, "returncode": process.returncode}
                         for name, process in self._children.items()}
        failed = [name for name, process in self._children.items() if process.returncode is not None]
        if self._state == "running" and failed:
            self._state = "error"
            self._message = "DAQ-Prozess beendet: " + ", ".join(failed) + ". Diagnoseprotokolle prüfen."
        return {"managed": True, "installed": installed, "running": self._state == "running",
                "state": self._state, "status": self._state, "message": self._message,
                "runtime_dir": str(self.runtime_dir), "run_dir": str(self._run_dir) if self._run_dir else None,
                "log_dir": str(self.log_dir), "processes": process_state,
                "cleanup_warnings": list(self._cleanup_warnings), "hardware_validated": False}

    def _read_manifest(self):
        if platform.system() != "Linux":
            raise HardwareError("Der integrierte USB-DAQ benötigt Linux. Unter Windows start.cmd/WSL2 verwenden oder einen entfernten DAQ verbinden.")
        if platform.machine().lower() not in ("x86_64", "amd64", "aarch64", "arm64"):
            raise HardwareError("Der integrierte DAQ unterstützt 64-Bit x86 oder ARM-Linux.")
        try:
            manifest = json.loads((self.runtime_dir / "manifest.json").read_text())
        except (OSError, ValueError) as exc:
            raise HardwareError("Integrierter Kraken-Stack fehlt oder ist unvollständig. Bitte setup.sh ausführen.") from exc
        if manifest.get("schema_version") != 1 or manifest.get("ready") is not True:
            raise HardwareError("Kraken-Runtime ist nicht fertig installiert. Bitte setup.sh erneut ausführen.")
        firmware = _resolve(self.runtime_dir, manifest.get("firmware_dir"), "firmware").resolve()
        native = _resolve(self.runtime_dir, manifest.get("native_prefix"), "native").resolve()
        # Preserve the venv entry point: resolving its symlink invokes the
        # system interpreter and loses the installed DAQ Python packages.
        python = _resolve(self.runtime_dir, manifest.get("python"), "../.venv/bin/python").absolute()
        missing = [str(firmware / "_daq_core" / name)
                   for name in (*REQUIRED_BINARIES, *REQUIRED_PYTHON)
                   if not (firmware / "_daq_core" / name).is_file()]
        if not python.is_file():
            missing.append(str(python))
        if not (firmware / "daq_chain_config.ini").is_file():
            missing.append(str(firmware / "daq_chain_config.ini"))
        if missing:
            raise HardwareError("Kraken-Runtime unvollständig: " + ", ".join(missing) + ". setup.sh erneut ausführen.")
        for name in REQUIRED_BINARIES:
            if not os.access(firmware / "_daq_core" / name, os.X_OK):
                raise HardwareError(f"DAQ-Programm ist nicht ausführbar: {name}. setup.sh erneut ausführen.")
        self._manifest = {**manifest, "firmware": firmware, "native": native, "python_path": python}
        return self._manifest

    def _preflight(self, config: dict):
        manifest = self._read_manifest()
        for field in ("center_frequency_hz", "sample_rate_hz", "gain_db"):
            try:
                value = float(config[field])
            except (KeyError, ValueError, TypeError) as exc:
                raise HardwareError(f"Ungültige DAQ-Einstellung: {field}") from exc
            if not math.isfinite(value):
                raise HardwareError(f"Ungültige DAQ-Einstellung: {field}")
        frequency, rate, gain = (float(config[key]) for key in ("center_frequency_hz", "sample_rate_hz", "gain_db"))
        if not 24e6 <= frequency <= 1766e6:
            raise HardwareError("DAQ-Mittenfrequenz muss zwischen 24 und 1766 MHz liegen.")
        if rate != round(rate) or not (225000 < rate <= 300000 or 900000 < rate <= 2400000):
            raise HardwareError("RTL-SDR-Abtastrate muss zwischen 225001–300000 oder 900001–2400000 Hz liegen.")
        if gain not in MANUAL_GAINS_DB:
            raise HardwareError("Kraken benötigt einen unterstützten diskreten R820T-Gain.")
        conflicts = sorted(set(DAQ_PORTS) & listening_ports())
        if conflicts:
            raise HardwareError("DAQ-Port bereits belegt: " + ", ".join(map(str, conflicts)) + ". Bestehenden Empfänger beenden oder externen DAQ-Modus verwenden.")
        existing = [name for name in SHM_NAMES if (self.shm_dir / name).exists()]
        if existing:
            raise HardwareError("Vorhandene fremde oder verwaiste DAQ-Speichersegmente: " + ", ".join(existing) + ". Sie werden nicht automatisch gelöscht; laufenden DAQ prüfen oder Host neu starten.")
        if not self.shm_dir.is_dir() or not os.access(self.shm_dir, os.W_OK):
            raise HardwareError("/dev/shm ist nicht schreibbar; normale Linux-Shared-Memory-Umgebung erforderlich.")
        # Only touched pages consume tmpfs storage: 2 uint8 IQ buffers plus
        # 6 complex64 buffers at the fixed 1M-sample CPI, plus some reserve.
        minimum = 2 * 1048576 * 5 * 2 + 6 * 1048576 * 5 * 8 + 32 * 1024**2
        if shutil.disk_usage(self.shm_dir).free < minimum:
            raise HardwareError("Zu wenig freier /dev/shm-Speicher für den Fünfkanal-DAQ (mindestens 292 MiB empfohlen).")
        devices = discover_rtl_devices(manifest["native"])
        if not devices:
            raise HardwareError("Kein KrakenSDR gefunden. USB-Datenkabel und Stromversorgung prüfen; unter WSL USB zuerst mit usbipd verbinden.")
        inaccessible = [d for d in devices if not d.get("accessible")]
        if inaccessible:
            raise HardwareError("RTL-SDR-Geräte gefunden, aber USB-Zugriff fehlt. udev-Regeln aus setup.sh anwenden, Gerät neu einstecken und Gruppenzugehörigkeit prüfen.")
        serials = [d.get("serial") for d in devices]
        missing = sorted(set(map(str, range(1000, 1005))) - set(serials))
        if missing or any(serials.count(str(number)) != 1 for number in range(1000, 1005)):
            raise HardwareError("Kraken benötigt fünf eindeutige Geräte mit Seriennummern 1000–1004. Gefunden: " + ", ".join(map(str, serials)) + ". Es wird kein EEPROM automatisch verändert.")
        return manifest

    def _acquire_lock(self):
        if fcntl is None or platform.system() != "Linux":
            raise HardwareError("Der integrierte USB-DAQ benötigt Linux. Unter Windows WSL2 oder einen entfernten Linux-DAQ verwenden.")
        # Cross-check across project copies owned by the same user. The
        # marker file never grants authority over any unrecorded process.
        lock_path = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"krakendf-daq-{os.getuid()}.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        self._lock_stream = os.fdopen(fd, "a+")
        try:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_stream.close()
            self._lock_stream = None
            raise HardwareError("Eine andere KrakenDF-Instanz verwaltet bereits den lokalen DAQ.") from exc

    def _prepare_run(self, manifest, config):
        self._run_dir = self.runtime_dir / "runs" / uuid.uuid4().hex
        self._run_dir.mkdir(parents=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "_data_control").mkdir(mode=0o700)
        (self._run_dir / "_logs").symlink_to(self.log_dir, target_is_directory=True)
        (self._run_dir / "_daq_core").symlink_to(manifest["firmware"] / "_daq_core", target_is_directory=True)
        calibration = manifest["firmware"] / "_calibration"
        if calibration.is_dir():
            (self._run_dir / "_calibration").symlink_to(calibration, target_is_directory=True)
        for name in FIFO_NAMES:
            os.mkfifo(self._run_dir / "_data_control" / name, 0o600)
        # Upstream treats this track-lock control as a regular one-byte file.
        (self._run_dir / "_data_control" / "iq_track_lock").write_text("0")
        (self._run_dir / "_data_control" / "fir_coeffs.txt").write_text("1.0\n")
        parser = ConfigParser()
        parser.read(manifest["firmware"] / "daq_chain_config.ini")
        for section in ("meta", "hw", "daq", "pre_processing", "calibration", "adpis", "data_interface"):
            if not parser.has_section(section):
                raise HardwareError(f"Heimdall-Vorlage hat keinen Abschnitt [{section}]. setup.sh erneut ausführen.")
        updates = {
            "hw": {"num_ch": "5", "en_bias_tee": "0,0,0,0,0"},
            "daq": {"center_freq": str(round(config["center_frequency_hz"])),
                    "sample_rate": str(round(config["sample_rate_hz"])),
                    "gain": str(round(config["gain_db"] * 10)),
                    "en_noise_source_ctr": "1", "ctr_channel_serial_no": "1000"},
            "pre_processing": {"cpi_size": "1048576", "decimation_ratio": "1", "fir_tap_size": "1", "fir_relative_bandwidth": "1.0"},
            "calibration": {"en_iq_cal": "1", "std_ch_ind": "0", "en_gain_tune_init": "0", "unified_gain_control": "1"},
            "adpis": {"en_adpis": "0", "adpis_gains_init": ",".join([str(round(config["gain_db"] * 10))] * 5)},
            "data_interface": {"out_data_iface_type": "eth"},
        }
        for section, values in updates.items():
            for key, value in values.items():
                parser.set(section, key, value)
        with (self._run_dir / "daq_chain_config.ini").open("w") as stream:
            parser.write(stream)
        return self._run_dir

    def _environment(self, manifest):
        env = os.environ.copy()
        libs = str(manifest["native"] / "lib")
        env["LD_LIBRARY_PATH"] = libs + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        env["PATH"] = str(manifest["native"] / "bin") + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"
        env["NUMBA_CACHE_DIR"] = str(self.runtime_dir / "numba-cache")
        env["NUMBA_NUM_THREADS"] = str(max(1, min(4, os.cpu_count() or 1)))
        return env

    async def _spawn(self, name, args, env, *, stdin=None, stdout=None):
        stream = (self.log_dir / f"{self._run_dir.name}-{name}.log").open("ab", buffering=0)
        self._logs.append(stream)
        process = await asyncio.create_subprocess_exec(*map(str, args), cwd=self._run_dir,
            env=env, stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=stdout if stdout is not None else stream, stderr=stream,
            start_new_session=True)
        self._children[name] = process
        return process

    def _capture_owned_memory(self):
        for process in self._children.values():
            if process.returncode is not None:
                continue
            fd_dir = Path(f"/proc/{process.pid}/fd")
            try:
                fds = list(fd_dir.iterdir())
            except (FileNotFoundError, PermissionError):
                continue
            for fd in fds:
                try:
                    target = Path(os.readlink(fd))
                    if target.parent != self.shm_dir or target.name not in SHM_NAMES:
                        continue
                    fd_info, path_info = fd.stat(), target.stat()
                    identity = (fd_info.st_dev, fd_info.st_ino)
                    if identity == (path_info.st_dev, path_info.st_ino) and path_info.st_uid == os.getuid():
                        self._owned_shm.setdefault(target, identity)
                except (FileNotFoundError, PermissionError, OSError):
                    continue

    async def start(self, config: dict) -> dict:
        async with self._lifecycle:
            if self._state == "running" and config == self._config:
                return self.status()
            if self._children or self._lock_stream:
                await self._stop()
            self._state, self._message = "starting", "Kraken-DAQ wird vorbereitet."
            try:
                self._acquire_lock()
                self._cleanup_warnings = []
                manifest = await asyncio.to_thread(self._preflight, config)
                self._prepare_run(manifest, config)
                env = self._environment(manifest)
                core = manifest["firmware"] / "_daq_core"
                python = manifest["python_path"]
                read_fd, write_fd = os.pipe()
                try:
                    await self._spawn("rtl_daq", [core / "rtl_daq.out"], env, stdout=write_fd)
                    await self._spawn("rebuffer", [core / "rebuffer.out", "0"], env, stdin=read_fd)
                finally:
                    os.close(read_fd)
                    os.close(write_fd)
                await self._spawn("decimator", [core / "decimate.out"], env)
                await self._spawn("delay_sync", [python, core / "delay_sync.py"], env)
                await self._spawn("controller", [python, core / "hw_controller.py"], env)
                await self._spawn("iq_server", [core / "iq_server.out"], env)
                deadline = time.monotonic() + self.startup_timeout
                while time.monotonic() < deadline:
                    self._capture_owned_memory()
                    failed = [f"{name} (Exit {p.returncode})" for name, p in self._children.items() if p.returncode is not None]
                    if failed:
                        raise HardwareError("Kraken-DAQ konnte nicht starten: " + ", ".join(failed) + f". Protokolle: {self.log_dir}")
                    if {5000, 5001}.issubset(listening_ports()):
                        self._config = dict(config)
                        self._state = "running"
                        self._message = "Integrierter Heimdall läuft; RF-Kohärenz wird anhand der IQ-Header geprüft."
                        return self.status()
                    await asyncio.sleep(.1)
                raise HardwareError(f"Kraken-DAQ ist nach {self.startup_timeout:g} s nicht bereit. USB, Rechte und Protokolle prüfen: {self.log_dir}")
            except BaseException as exc:
                await self._stop()
                self._state = "error"
                self._message = str(exc) or "DAQ-Start wurde abgebrochen."
                raise

    async def _stop(self):
        self._capture_owned_memory()
        for process in reversed(list(self._children.values())):
            if process.returncode is None:
                with _ignore_missing_process():
                    os.killpg(process.pid, signal.SIGTERM)
        for process in reversed(list(self._children.values())):
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), self.stop_timeout)
                except asyncio.TimeoutError:
                    with _ignore_missing_process():
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        self._children.clear()
        for stream in self._logs:
            stream.close()
        self._logs.clear()
        for path, identity in self._owned_shm.items():
            try:
                info = path.stat()
                if (info.st_dev, info.st_ino) == identity and info.st_uid == os.getuid():
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._cleanup_warnings.append(f"Eigenes Shared-Memory-Segment konnte nicht entfernt werden: {path.name}: {exc}")
        self._owned_shm.clear()
        if self._lock_stream:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None
        self._config = None
        self._state, self._message = "stopped", "Integrierter DAQ-Stack ist angehalten."

    async def stop(self):
        async with self._lifecycle:
            await self._stop()


class _ignore_missing_process:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, ProcessLookupError)
