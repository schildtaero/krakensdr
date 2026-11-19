"""Bounded local diagnostic logging and a deliberately scoped support export.

The archive contains log tails and receiver settings, never recordings, map
data, full configuration files or arbitrary caller-selected paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import re
import stat
import sys
import threading
import time
import traceback
import zipfile

from . import __version__

LOG_BYTES = 512 * 1024
LOG_BACKUPS = 3
TAIL_BYTES = 256 * 1024
TOTAL_LOG_BYTES = 4 * 1024 * 1024
MAX_DAQ_LOGS = 24
MAX_DIRECTORY_ENTRIES = 4096
_DAQ_NAME = re.compile(r"^[A-Za-z0-9_-]{1,160}\.log$")
_SECRET = re.compile(r"(?i)\b(authorization|api[-_ ]?key|token|password|passwd|secret)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;\"']+")


def _redact(text: object, limit: int = 32768) -> str:
    return _SECRET.sub(r"\1\2[redacted]", str(text)[:limit])


class DiagnosticLog:
    """Small rotating application log; logging failure never stops reception."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "logs" / "application.log"
        self._lock = threading.RLock()
        self._handler = None
        self.last_error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise OSError("Diagnostic log directory/file must not be a symlink")
            self._handler = RotatingFileHandler(self.path, maxBytes=LOG_BYTES,
                                               backupCount=LOG_BACKUPS,
                                               encoding="utf-8", delay=True)
            formatter = logging.Formatter("%(asctime)sZ [%(levelname)s] %(message)s",
                                          datefmt="%Y-%m-%dT%H:%M:%S")
            formatter.converter = time.gmtime
            self._handler.setFormatter(formatter)
            self._handler.handleError = self._logging_failure
        except OSError as exc:
            self.last_error = str(exc)

    def _logging_failure(self, record) -> None:
        self.last_error = str(sys.exc_info()[1] or "Diagnostic log write failed")

    def write(self, message: str, level: str = "info") -> None:
        with self._lock:
            if self._handler is None:
                return
            try:
                if self.path.is_symlink() or self.path.parent.is_symlink():
                    self.last_error = "Diagnostic log directory/file became a symlink"
                    return
                numeric_level = getattr(logging, str(level).upper(), logging.INFO)
                if not isinstance(numeric_level, int):
                    numeric_level = logging.INFO
                record = logging.LogRecord("krakendf.diagnostics", numeric_level, "", 0,
                                           _redact(message), (), None)
                self._handler.emit(record)
            except (OSError, ValueError) as exc:
                self.last_error = str(exc)

    def exception(self, context: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.write(f"{context}\n{detail}", "error")

    def close(self) -> None:
        with self._lock:
            if self._handler:
                self._handler.close()
                self._handler = None


@contextmanager
def _directory(data_dir: Path, components: tuple[str, ...]):
    """Open each log directory without following substituted symlinks."""
    descriptors = []
    opened = None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            current = os.open(Path(data_dir).resolve(), flags)
            descriptors.append(current)
            for component in components:
                current = os.open(component, flags, dir_fd=current)
                descriptors.append(current)
            opened = current
        else:
            current = Path(data_dir).resolve()
            for component in components:
                current /= component
                if current.is_symlink() or not current.is_dir():
                    raise OSError("Missing or linked log directory")
            opened = current
    except OSError:
        pass
    try:
        yield opened
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _entries(directory) -> list[tuple[str, float]]:
    if directory is None:
        return []
    result = []
    try:
        with os.scandir(directory) as entries:
            for index, item in enumerate(entries):
                if index >= MAX_DIRECTORY_ENTRIES:
                    break
                try:
                    metadata = item.stat(follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode):
                        result.append((item.name, metadata.st_mtime))
                except OSError:
                    continue
    except OSError:
        pass
    return result


def _tail(directory, name: str, limit: int) -> bytes | None:
    if directory is None:
        return None
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        if isinstance(directory, int):
            descriptor = os.open(name, flags, dir_fd=directory)
        else:
            path = directory / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or directory.is_symlink():
                return None
            descriptor = os.open(path, flags)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                return None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        start = max(0, metadata.st_size - limit)
        os.lseek(descriptor, start, os.SEEK_SET)
        data = os.read(descriptor, limit)
        # Drop a clipped first line so a partial token is not misinterpreted.
        if start and b"\n" in data:
            data = data.split(b"\n", 1)[1]
        return _redact(data.decode("utf-8", errors="replace"), limit).encode("utf-8")[:limit].decode("utf-8", errors="ignore").encode("utf-8")
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _numbers(value, depth: int = 0):
    """Allow bounded numeric performance metrics, excluding arbitrary objects."""
    if depth > 3:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key)[:80]: _numbers(item, depth + 1)
                for key, item in list(value.items())[:64]
                if isinstance(item, (int, float, dict)) and not isinstance(item, bool)}
    return None


def diagnostic_snapshot(service) -> dict:
    """Collect receiver diagnostics without positions, payloads or TX notes."""
    configuration = service.config.model_dump()
    source = service.source
    daq = service.daq.status()
    gps = service._gps_state()
    radar = getattr(service, "radar", None)
    radar_state = radar.snapshot() if radar is not None else {}
    versions = {}
    for package in ("numpy", "scipy", "fastapi", "pyzmq", "numba", "scikit-rf"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "application_version": __version__,
        "system": {"platform": platform.system(), "release": platform.release(),
                   "machine": platform.machine(), "python": platform.python_version(),
                   "cpu_count": os.cpu_count(), "virtualenv": sys.prefix != sys.base_prefix,
                   "packages": versions,
                   "thread_environment": {key: os.environ[key] for key in
                       ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS") if key in os.environ}},
        "receiver": {key: configuration.get(key) for key in
                     ("center_frequency_hz", "sample_rate_hz", "gain_db", "fft_size", "array_id", "heading_deg", "station_mode")},
        "channels": [{key: channel.get(key) for key in
                      ("id", "enabled", "frequency_hz", "bandwidth_hz", "modulation", "lora_sf", "lora_bandwidth_hz")}
                     for channel in configuration.get("channels", [])[:16]],
        "source": {**{key: source.get(key) for key in
                   ("mode", "status", "managed", "simulated", "received_frames", "dropped_frames", "dsp_backend")},
                   "dsp_fallback_reason": _redact(source.get("dsp_fallback_reason", ""), 8192),
                   "message": _redact(source.get("message", ""), 8192)},
        "performance": _numbers(source.get("performance", getattr(service, "performance", {}))),
        "radar": {"config": radar_state.get("config", {}),
                  **{key: radar_state.get(key) for key in
                     ("mode", "status", "simulated", "received_frames", "dropped_frames",
                      "processed_cpis", "processing_ms", "rf_duration_ms", "hardware_validated")},
                  "message": _redact(radar_state.get("message", ""), 8192),
                  "quality": _numbers(radar_state.get("quality", {}))},
        "gps": {**{key: gps.get(key) for key in (
            "model", "status", "connected", "detected", "port", "baud", "valid",
            "bytes_received", "lines_received", "nmea_received", "invalid_sentences",
            "data_age_s", "nmea_age_s", "fix_age_s", "connection_age_s", "satellite_age_s", "satellites_used_age_s",
            "satellites_used", "satellites_in_view", "satellites_with_signal",
            "fix_quality", "fix_type", "hdop", "anchor_ready", "anchor_samples", "anchor_target", "sentence_types")},
            "message": _redact(gps.get("message", ""), 8192),
            "satellites_detail": [{key: satellite.get(key) for key in (
                "prn", "talker", "elevation_deg", "azimuth_deg", "cn0_dbhz", "used")}
                for satellite in gps.get("satellites_detail", [])[:128]]},
        "daq": {key: daq.get(key) for key in
                ("installed", "running", "state", "message", "processes", "hardware_validated")},
        "logging_error": getattr(getattr(service, "diagnostic_log", None), "last_error", None),
    }


def build_diagnostic_archive(data_dir: Path, snapshot: dict, events: list[dict]) -> bytes:
    """Build a bounded ZIP from fixed log locations; callers supply no paths."""
    output = io.BytesIO()
    inventory = []
    remaining = TOTAL_LOG_BYTES
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
        recent = [{"time": _redact(event.get("time", ""), 80),
                   "level": _redact(event.get("level", ""), 32),
                   "message": _redact(event.get("message", ""), 2048)} for event in events[:80]]
        archive.writestr("recent-events.json", json.dumps(recent, ensure_ascii=False, indent=2))
        with _directory(data_dir, ("logs",)) as application:
            for name in ["application.log"] + [f"application.log.{index}" for index in range(1, LOG_BACKUPS + 1)]:
                if remaining <= 0:
                    break
                content = _tail(application, name, min(TAIL_BYTES, remaining))
                if content is not None:
                    archive.writestr(f"logs/{name}", content)
                    inventory.append(f"logs/{name}: {len(content)} bytes")
                    remaining -= len(content)
        with _directory(data_dir, ("logs", "daq")) as daq:
            candidates = [(name, modified) for name, modified in _entries(daq) if _DAQ_NAME.fullmatch(name)]
            for name, _ in sorted(candidates, key=lambda item: item[1], reverse=True)[:MAX_DAQ_LOGS]:
                if remaining <= 0:
                    break
                content = _tail(daq, name, min(TAIL_BYTES, remaining))
                if content is not None:
                    archive.writestr(f"logs/daq/{name}", content)
                    inventory.append(f"logs/daq/{name}: {len(content)} bytes")
                    remaining -= len(content)
        readme = (
            "KrakenDF – Diagnosepaket\n\n"
            "diagnostics.json: Version, Host, RX-Konfiguration, Prozess- und Leistungsdaten.\n"
            "recent-events.json: die letzten bis zu 80 Anwendungsereignisse.\n"
            "logs/: begrenzte Ausschnitte des persistenten Anwendungslogs und der neuesten DAQ-Logs.\n"
            "Keine Aufnahmen, IQ-Daten, Karten, Symbole, GPS-Koordinaten aus der Konfiguration oder TX-Notizen.\n"
            "Logtexte können vom Programm ausgegebene Pfade und Ereignisdetails enthalten.\n"
            f"Begrenzung: maximal {TAIL_BYTES} Bytes je Log, {TOTAL_LOG_BYTES} Bytes insgesamt; "
            f"maximal {MAX_DAQ_LOGS} DAQ-Dateien, {MAX_DIRECTORY_ENTRIES} Verzeichniseinträge untersucht.\n"
            "Fehlende/gesperrte Logs und Verknüpfungen werden ausgelassen.\n\n"
            "Enthaltene Logausschnitte:\n" + ("\n".join(inventory) or "Noch keine lesbaren Logdateien vorhanden.") + "\n"
        )
        archive.writestr("README.txt", readme)
    return output.getvalue()
