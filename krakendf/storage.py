"""Local, crash-recoverable recording and Excel export.

The newline-terminated event log is authoritative. IQ is committed before its
event, and the small manifest is replaced atomically after the event is durable.
An interruption may therefore leave an orphan IQ file or stale manifest counters,
but cannot turn an incomplete event into a valid recorded observation.
"""
from __future__ import annotations

import copy
import io
import json
import math
import os
from pathlib import Path
import re
import threading
from datetime import datetime, timezone
from typing import Any, Iterator
import uuid

import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill


SCHEMA_VERSION = 1
MAX_IQ_SAMPLES = 10_000_000
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_KINDS = {"measurement", "calibration", "validation"}
_XML_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class SessionCorruptError(ValueError):
    """A committed log entry or manifest cannot be interpreted safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, Path)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported recording value: {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _sync_directory(path: Path) -> None:
    # Windows does not support opening a directory with os.open in this way.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(_json_bytes(value))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _check_iq(iq: np.ndarray) -> None:
    if iq.ndim != 2 or iq.shape[0] != 5 or not 0 < iq.shape[1] <= MAX_IQ_SAMPLES:
        raise ValueError("IQ must have shape (5, N), with 1 <= N <= 10000000")
    if iq.dtype != np.dtype("complex64"):
        raise ValueError("Recorded IQ must be complex64")
    if not np.isfinite(iq).all():
        raise ValueError("Recorded IQ contains non-finite samples")


class SessionStore:
    """Single-process session storage with thread-safe readers and writers.

    Use one instance for a data directory. The thread lock deliberately does not
    pretend to provide distributed or multi-process writer coordination.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[tuple, dict, list[dict], int]] = {}

    def _signature(self, session_id: str) -> tuple:
        directory = self._directory(session_id)
        signature = []
        for name in ("manifest.json", "events.jsonl"):
            path = directory / name
            if path.is_symlink():
                raise SessionCorruptError(f"Symlinked {name} is not allowed")
            try:
                stat = path.stat()
            except FileNotFoundError:
                signature.extend((None, None, None))
            else:
                signature.extend((stat.st_ino, stat.st_size, stat.st_mtime_ns))
        return tuple(signature)

    def _cache_put(self, session_id: str, metadata: dict, events: list[dict], committed_end: int) -> None:
        self._cache.pop(session_id, None)
        self._cache[session_id] = (self._signature(session_id), copy.deepcopy(metadata), events, committed_end)
        # State polling should not repeatedly rescan large logs. Limit retained
        # history so inspecting many old sessions does not retain all their data.
        if len(self._cache) > 8:
            self._cache.pop(next(iter(self._cache)))

    def _directory(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid session ID")
        directory = self.root / session_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
            raise ValueError("Session path escapes recording directory")
        if not directory.is_dir():
            raise FileNotFoundError(f"Unknown session: {session_id}")
        return directory

    def _manifest(self, session_id: str) -> dict:
        directory = self._directory(session_id)
        path = directory / "manifest.json"
        if path.is_symlink():
            raise SessionCorruptError("Symlinked manifest is not allowed")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SessionCorruptError(f"Unreadable manifest for {session_id}") from exc
        if not isinstance(result, dict) or result.get("id") != session_id:
            raise SessionCorruptError("Manifest session ID mismatch")
        if result.get("schema_version") != SCHEMA_VERSION:
            raise SessionCorruptError("Unsupported recording schema")
        return result

    def _read_events(self, session_id: str) -> tuple[list[dict], int, list[str]]:
        path = self._directory(session_id) / "events.jsonl"
        if path.is_symlink():
            raise SessionCorruptError("Symlinked event log is not allowed")
        events: list[dict] = []
        committed_end = 0
        warnings: list[str] = []
        if not path.exists():
            return events, committed_end, warnings
        with path.open("rb") as stream:
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    warnings.append("Incomplete final event ignored; original committed events retained")
                    break
                try:
                    event = json.loads(line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise SessionCorruptError(f"Malformed committed event {len(events) + 1}") from exc
                if (not isinstance(event, dict) or event.get("session_id") != session_id
                        or event.get("sequence") != len(events)
                        or not isinstance(event.get("observation"), dict)
                        or not isinstance(event.get("iq_bytes", 0), int)
                        or event.get("iq_bytes", 0) < 0):
                    raise SessionCorruptError(f"Invalid committed event {len(events) + 1}")
                events.append(event)
                committed_end = stream.tell()
        return events, committed_end, warnings

    def _recover(self, session_id: str) -> tuple[dict, list[dict], int]:
        cached = self._cache.get(session_id)
        if cached is not None and cached[0] == self._signature(session_id):
            return copy.deepcopy(cached[1]), cached[2], cached[3]
        metadata = self._manifest(session_id)
        events, committed_end, warnings = self._read_events(session_id)
        packet_count = len(events)
        iq_bytes = sum(event.get("iq_bytes", 0) for event in events)
        changed = metadata.get("packet_count") != packet_count or metadata.get("iq_bytes") != iq_bytes
        metadata.update(packet_count=packet_count, iq_bytes=iq_bytes)
        previous_warnings = metadata.get("recovery_warnings", [])
        if warnings:
            metadata["recovery_warnings"] = list(dict.fromkeys(previous_warnings + warnings))
            changed |= metadata["recovery_warnings"] != previous_warnings
        if changed:
            _atomic_json(self._directory(session_id) / "manifest.json", metadata)
        self._cache_put(session_id, metadata, events, committed_end)
        return metadata, events, committed_end

    def start(self, config: dict, name: str, kind: str = "measurement", simulated: bool = False) -> dict:
        if kind not in _KINDS:
            raise ValueError("Session kind must be measurement, calibration or validation")
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError("Session name must be a string of at most 200 characters")
        if not isinstance(config, dict):
            raise ValueError("Configuration must be an object")
        with self._lock:
            session_id = uuid.uuid4().hex
            directory = self.root / session_id
            directory.mkdir()
            (directory / "iq").mkdir()
            metadata = {
                "schema_version": SCHEMA_VERSION, "id": session_id,
                "name": name.strip() or "Messung", "kind": kind,
                "started_at": _now(), "stopped_at": None, "state": "recording",
                "packet_count": 0, "iq_bytes": 0, "simulated": bool(simulated),
                "config": _jsonable(copy.deepcopy(config)),
            }
            (directory / "events.jsonl").touch()
            _atomic_json(directory / "manifest.json", metadata)
            self._cache_put(session_id, metadata, [], 0)
            _sync_directory(self.root)
            return copy.deepcopy(metadata)

    def append(self, session_id: str, observation: dict, iq: np.ndarray | None = None,
               spectrum: dict | None = None) -> None:
        if not isinstance(observation, dict):
            raise ValueError("Observation must be an object")
        # Validate JSON and samples before creating any new file.
        observation = _jsonable(observation)
        spectrum = _jsonable(spectrum)
        _json_bytes(observation)
        if iq is not None:
            iq = np.asarray(iq)
            _check_iq(iq)
        with self._lock:
            metadata, events, committed_end = self._recover(session_id)
            if metadata.get("state") != "recording":
                raise ValueError("Session has already been stopped")
            directory = self._directory(session_id)
            sequence = len(events)
            iq_relative = None
            iq_bytes = 0
            if iq is not None:
                iq_relative = f"iq/{sequence:08d}-{uuid.uuid4().hex}.npy"
                iq_path = directory / iq_relative
                temporary = iq_path.with_suffix(".tmp")
                try:
                    with temporary.open("xb") as stream:
                        np.save(stream, iq, allow_pickle=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, iq_path)
                    _sync_directory(iq_path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
                iq_bytes = iq_path.stat().st_size
            event = {
                "sequence": sequence, "session_id": session_id,
                "recorded_at": _now(), "observation": observation,
                "iq_path": iq_relative, "iq_bytes": iq_bytes, "spectrum": spectrum,
            }
            encoded = _json_bytes(event) + b"\n"
            log_path = directory / "events.jsonl"
            # Remove a crashed partial tail before adding the next whole event.
            with log_path.open("r+b") as stream:
                stream.truncate(committed_end)
                stream.seek(committed_end)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            metadata.update(packet_count=sequence + 1,
                            iq_bytes=metadata["iq_bytes"] + iq_bytes,
                            updated_at=_now())
            _atomic_json(directory / "manifest.json", metadata)
            self._cache_put(session_id, metadata, events + [event], committed_end + len(encoded))

    def stop(self, session_id: str) -> dict:
        with self._lock:
            metadata, _, _ = self._recover(session_id)
            if metadata.get("state") != "stopped":
                metadata.update(state="stopped", stopped_at=_now())
                _atomic_json(self._directory(session_id) / "manifest.json", metadata)
            return copy.deepcopy(metadata)

    def get(self, session_id: str) -> dict:
        with self._lock:
            metadata, _, _ = self._recover(session_id)
            return copy.deepcopy(metadata)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            result = []
            for directory in self.root.iterdir():
                if _SESSION_ID.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink():
                    try:
                        result.append(self.get(directory.name))
                    except SessionCorruptError as exc:
                        result.append({"id": directory.name, "name": "Beschädigte Aufnahme",
                                       "state": "corrupt", "started_at": "", "error": str(exc)})
            return sorted(result, key=lambda row: row.get("started_at", ""), reverse=True)

    def observations(self, session_id: str) -> list[dict]:
        return [event["observation"] for event in self.iter_events(session_id)]

    def iter_events(self, session_id: str) -> Iterator[dict]:
        # Snapshot under the lock; no lock is held while a consumer sleeps in replay.
        with self._lock:
            _, events, _ = self._recover(session_id)
            snapshot = copy.deepcopy(events)
        return iter(snapshot)

    def load_iq(self, event: dict) -> np.ndarray:
        if not isinstance(event, dict):
            raise ValueError("Event must be an object")
        with self._lock:
            directory = self._directory(event.get("session_id"))
            relative = event.get("iq_path")
            if not isinstance(relative, str) or not relative:
                raise ValueError("Event has no recorded IQ")
            candidate = directory / relative
            if (Path(relative).is_absolute() or ".." in Path(relative).parts
                    or candidate.is_symlink() or not candidate.resolve().is_relative_to(directory / "iq")):
                raise ValueError("Invalid IQ path")
            if candidate.stat().st_size > 5 * MAX_IQ_SAMPLES * 8 + 65536:
                raise ValueError("IQ clip exceeds size limit")
            # mmap reads the header without allocating untrusted claimed dimensions.
            mapped = np.load(candidate, mmap_mode="r", allow_pickle=False, max_header_size=16384)
            _check_iq(mapped)
            return np.array(mapped, dtype=np.complex64, copy=True)

    def export_xlsx(self, session_id: str | None = None, observations: list[dict] | None = None,
                    config: dict | None = None, stats: dict | None = None,
                    calibrations: list[dict] | None = None) -> bytes:
        metadata: dict = {"name": "Aktuelle Messung", "exported_at": _now(), "schema_version": SCHEMA_VERSION}
        if session_id is not None:
            metadata.update(self.get(session_id))
            if observations is None:
                observations = self.observations(session_id)
            if config is None:
                config = metadata.get("config", {})
        observations = observations or []
        config = config or {}
        if stats is None:
            try:
                from .dsp import statistics
            except ImportError:
                stats = _fallback_stats(observations)
            else:
                stats = statistics(observations)
        return _make_workbook(metadata, observations, config, stats, calibrations or [])


def _fallback_stats(observations: list[dict]) -> dict:
    def rms(values):
        valid = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
        return math.sqrt(sum(value ** 2 for value in valid) / len(valid)) if valid else None
    accepted = [o for o in observations if o.get("accepted", True)]
    return {"count": len(accepted), "rms_raw_deg": rms([o.get("error_raw_deg") for o in accepted]),
            "rms_corrected_deg": rms([o.get("error_corrected_deg") for o in accepted]), "bins": []}


def _excel_value(value: Any) -> Any:
    value = _jsonable(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if isinstance(value, str):
        value = _XML_CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", value)
        # Excel's cell limit; do not silently emit an invalid workbook.
        return value[:32767]
    return value


def _append_row(sheet, values: list[Any]) -> None:
    row_number = sheet.max_row + 1
    for column, value in enumerate(values, start=1):
        value = _excel_value(value)
        cell = sheet.cell(row=row_number, column=column)
        cell.value = value
        if isinstance(value, str):
            # Explicit text type prevents =,+,-,@ received data becoming formulas.
            cell.data_type = "s"
            if value.startswith(("=", "+", "-", "@")):
                cell.quotePrefix = True


def _table(workbook: Workbook, name: str, columns: list[tuple[str, str]], rows: list[dict]):
    sheet = workbook.create_sheet(name)
    # A new openpyxl sheet reports max_row=1 although it has no cells.
    for column, (_, title) in enumerate(columns, 1):
        sheet.cell(1, column, title)
    for item in rows:
        _append_row(sheet, [item.get(key) for key, _ in columns])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="17384A")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(vertical="center")
    for column, (_, title) in enumerate(columns, 1):
        from openpyxl.utils import get_column_letter
        sheet.column_dimensions[get_column_letter(column)].width = min(48, max(15, len(title) + 3))
    return sheet


def _make_workbook(metadata: dict, observations: list[dict], config: dict,
                   stats: dict, calibrations: list[dict]) -> bytes:
    if len(observations) > 1_048_575:
        raise ValueError("Too many observations for one Excel worksheet")
    workbook = Workbook()
    workbook.remove(workbook.active)
    summary = [{"key": key, "value": value} for key, value in metadata.items() if key != "config"]
    summary.extend({"key": f"config.{key}", "value": value} for key, value in config.items())
    _table(workbook, "Sitzung", [("key", "Schlüssel"), ("value", "Wert")], summary)
    fields = [
        ("timestamp", "Empfangszeit UTC"), ("callsign", "Rufzeichen"), ("modulation", "Modulation"),
        ("frequency_hz", "Frequenz Hz"), ("raw_deg", "Richtung roh °"),
        ("corrected_deg", "Richtung korrigiert °"), ("reference_deg", "GPS-Sollrichtung °"),
        ("error_raw_deg", "Fehler roh °"), ("error_corrected_deg", "Fehler korrigiert °"),
        ("accepted", "Akzeptiert"), ("power_db", "Leistung dB"), ("confidence", "DF-Qualitätswert"),
        ("latitude", "Emitter Breite"), ("longitude", "Emitter Länge"),
        ("station_latitude", "Station Breite"), ("station_longitude", "Station Länge"),
        ("position_utc", "Positionszeit UTC"), ("satellites", "Satelliten"),
        ("source_mode", "Quelle"), ("simulated", "Simuliert"), ("profile_id", "Messprofil"),
        ("run_id", "Messlauf"), ("stream_epoch", "Stream-Epoche"), ("burst_id", "Burst-ID"),
        ("sample_start", "Sample Start"), ("sample_end", "Sample Ende"),
        ("sample_rate_hz", "Abtastrate Hz"), ("payload", "Originaltelegramm"),
        ("quality", "Qualitätsmerkmale"), ("details_json", "Vollständiger Messwert JSON"),
    ]
    rows = [dict(observation, details_json=observation) for observation in observations]
    _table(workbook, "Messungen", fields, rows)
    _table(workbook, "Gesamt", [("key", "Kennwert"), ("value", "Wert")],
           [{"key": key, "value": value} for key, value in stats.items() if key != "bins"])
    _table(workbook, "Azimut", [
        ("start_deg", "Von °"), ("end_deg", "Bis °"), ("count", "Messungen"),
        ("rms_raw_deg", "RMS roh °"), ("rms_corrected_deg", "RMS korrigiert °"),
        ("bias_raw_deg", "Bias roh °"), ("bias_corrected_deg", "Bias korrigiert °"),
    ], stats.get("bins", []))
    _table(workbook, "Kalibrierung", [
        ("id", "Kalibrier-ID"), ("eligible", "Daten ausreichend"), ("validated", "Validiert"),
        ("improved", "Verbessert"), ("reason", "Ergebnis"), ("training_count", "Trainingsmessungen"),
        ("validation_count", "Prüfmessungen"), ("coverage_complete", "Richtungsabdeckung vollständig"),
        ("validation_before_deg", "Prüf-RMS vorher °"), ("validation_after_deg", "Prüf-RMS nachher °"),
        ("coefficients", "Koeffizienten"), ("coverage", "Abdeckung"),
    ], calibrations)
    workbook.properties.title = "KrakenDF Messauswertung"
    workbook.properties.description = "Messungen, Richtungsfehler, Profile und unabhängige Kalibrierprüfung"
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
