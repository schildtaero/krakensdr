"""Bounded passive-radar IQ-pair recording, replay and numerical export.

Each committed JSONL event points to an atomic two-channel NumPy file and an
optional result JSON. The event log, not manifest counters, is authoritative.
Maps are kept outside the event log so state polling need not retain every
range/Doppler matrix in memory. Recordings preserve their exact IQ stage;
channelized pairs with delay prefix must be replayed without filtering again.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
from typing import Any, Iterator
import uuid

import numpy as np
from openpyxl import Workbook

from .storage import _atomic_json, _json_bytes, _jsonable, _sync_directory, _table

SCHEMA_VERSION = 1
MAX_PAIR_SAMPLES = 4_800_512
MAX_RESULT_BYTES = 8 * 1024**2
MAX_METADATA_BYTES = 128 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENT_LOG_BYTES = 64 * 1024**2
MAX_FRAMES = 100_000
DEFAULT_SESSION_BYTES = 4 * 1024**3
MIN_FREE_BYTES = 256 * 1024**2
_ID = re.compile(r"^[0-9a-f]{32}$")
_IQ_PATH = re.compile(r"^iq/[0-9]{8}-[0-9a-f]{32}\.npy$")
_RESULT_PATH = re.compile(r"^results/[0-9]{8}-[0-9a-f]{32}\.json$")
_STAGES = {"raw_pair", "channelized_pair_with_prefix"}


class RadarRecordingCorruptError(ValueError):
    """A committed passive-radar event or file is corrupt."""


class RadarStorageLimitError(ValueError):
    """Stop recording rather than filling the host's disk."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_json(value: Any, maximum: int) -> tuple[Any, bytes]:
    # Guard oversized NumPy matrices before converting them into Python lists.
    remaining = [600_000]
    characters = [maximum]

    def check(item, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 20:
            raise ValueError("Radar JSON structure exceeds its bounded size")
        if isinstance(item, np.ndarray):
            if item.size > remaining[0]:
                raise ValueError("Radar matrix exceeds its bounded size")
            remaining[0] -= item.size
        elif isinstance(item, dict):
            if len(item) > 2048:
                raise ValueError("Too many radar JSON keys")
            for key, v in item.items():
                characters[0] -= len(str(key))
                check(v, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > remaining[0]:
                raise ValueError("Radar JSON array exceeds its bounded size")
            for v in item:
                check(v, depth + 1)
        elif isinstance(item, str):
            characters[0] -= len(item)
        if characters[0] < 0:
            raise ValueError("Radar JSON strings exceed their bounded size")

    check(value)
    try:
        normalized = _jsonable(value)
        encoded = _json_bytes(normalized)
    except (TypeError, OverflowError) as exc:
        raise ValueError("Unsupported radar JSON value") from exc
    if len(encoded) > maximum:
        raise ValueError("Radar JSON exceeds its bounded byte size")
    return normalized, encoded


def _summary(result: dict) -> dict:
    """Keep scalar metadata and bounded candidates, but exclude large maps."""
    summary = {}
    for key, value in result.items():
        if isinstance(value, dict):
            summary[key] = _summary(value)
        elif isinstance(value, list):
            if key in ("candidates", "detections", "targets") and len(value) <= 128:
                summary[key] = value
            elif len(value) <= 16 and all(not isinstance(v, (list, dict)) for v in value):
                summary[key] = value
        else:
            summary[key] = value
    return summary


def _pair(iq: Any) -> np.ndarray:
    if not isinstance(iq, np.ndarray):
        raise ValueError("Radar IQ must be a NumPy complex64 array")
    data = iq
    if (data.ndim != 2 or data.shape[0] != 2
            or not 0 < data.shape[1] <= MAX_PAIR_SAMPLES):
        raise ValueError(f"Radar IQ must have shape (2, N), 1 <= N <= {MAX_PAIR_SAMPLES}")
    if data.dtype != np.dtype("complex64"):
        raise ValueError("Radar recording requires complex64 IQ")
    if not np.isfinite(data).all():
        raise ValueError("Radar IQ contains non-finite samples")
    return data


def _metadata(metadata: dict, count: int, simulated: bool) -> dict:
    if not isinstance(metadata, dict):
        raise ValueError("Radar frame metadata must be an object")
    metadata = dict(metadata)
    metadata.setdefault("iq_stage", metadata.get("stage"))
    if metadata.get("iq_stage") not in _STAGES:
        raise ValueError("Radar IQ stage must be explicit (raw_pair or channelized_pair_with_prefix)")
    for key in ("sample_rate_hz", "frequency_hz"):
        try:
            number = float(metadata[key])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Radar metadata requires finite positive {key}") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"Radar metadata requires finite positive {key}")
        metadata[key] = number
    for key in ("sample_start", "sample_end", "decimation_factor"):
        value = metadata.get(key, 1 if key == "decimation_factor" else None)
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise ValueError(f"Radar metadata requires integer {key}")
        metadata[key] = int(value)
    if (metadata["sample_start"] < 0 or metadata["decimation_factor"] < 1
            or metadata["sample_end"] - metadata["sample_start"] != count * metadata["decimation_factor"]):
        raise ValueError("Radar sample span does not match IQ length and decimation factor")
    if "cpi_sample_start" in metadata or "cpi_sample_end" in metadata:
        bounds = [metadata.get("cpi_sample_start"), metadata.get("cpi_sample_end")]
        if (any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) for v in bounds)
                or not metadata["sample_start"] <= bounds[0] < bounds[1] <= metadata["sample_end"]
                or any((int(v) - metadata["sample_start"]) % metadata["decimation_factor"] for v in bounds)):
            raise ValueError("Radar CPI bounds must lie on samples within the saved IQ pair")
    epoch = metadata.get("stream_epoch")
    if not isinstance(epoch, str) or not epoch or len(epoch) > 200:
        raise ValueError("Radar metadata requires a stream epoch")
    source = metadata.setdefault("source_mode", "simulation" if simulated else "live")
    if source != ("simulation" if simulated else "live"):
        raise ValueError("Recording cannot mix simulated and live radar frames")
    if metadata.get("simulated", simulated) != simulated:
        raise ValueError("Radar source marker does not match recording")
    metadata["simulated"] = simulated
    metadata.setdefault("timestamp", _now())
    for key in ("reference_channel", "surveillance_channel"):
        if key in metadata and (isinstance(metadata[key], bool)
                                or not isinstance(metadata[key], (int, np.integer))
                                or not 0 <= metadata[key] < 5):
            raise ValueError("Invalid physical radar channel number")
    if ("reference_channel" in metadata and "surveillance_channel" in metadata
            and metadata["reference_channel"] == metadata["surveillance_channel"]):
        raise ValueError("Reference and surveillance channels must differ")
    return _bounded_json(metadata, MAX_METADATA_BYTES)[0]


class RadarStore:
    """Single-writer-process, thread-safe recording store with explicit quota."""

    def __init__(self, root: Path | str, *, max_session_bytes: int = DEFAULT_SESSION_BYTES,
                 min_free_bytes: int = MIN_FREE_BYTES):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if max_session_bytes <= 0 or min_free_bytes < 0:
            raise ValueError("Invalid radar recording storage budget")
        self.max_session_bytes = int(max_session_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self._lock = threading.RLock()
        self._cache = {}

    def _directory(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            raise ValueError("Invalid radar session ID")
        directory = self.root / session_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
            raise ValueError("Radar session path escapes recording directory")
        if not directory.is_dir():
            raise FileNotFoundError("Unknown radar session")
        return directory

    def _file(self, event: dict, key: str, pattern) -> Path:
        directory = self._directory(event.get("session_id"))
        relative = event.get(key)
        if not isinstance(relative, str) or not pattern.fullmatch(relative):
            raise RadarRecordingCorruptError("Invalid radar file reference")
        path = directory / relative
        if path.parent.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise RadarRecordingCorruptError("Radar file escapes its session")
        return path

    def _signature(self, directory):
        result = []
        for name in ("manifest.json", "events.jsonl"):
            path = directory / name
            if path.is_symlink():
                raise RadarRecordingCorruptError("Symlinked radar metadata is not allowed")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise RadarRecordingCorruptError("Radar metadata must be a regular file")
            if name == "events.jsonl" and info.st_size > MAX_EVENT_LOG_BYTES + MAX_EVENT_BYTES:
                raise RadarRecordingCorruptError("Radar event log exceeds memory limit")
            result.extend((info.st_ino, info.st_size, info.st_mtime_ns))
        return tuple(result)

    def _load(self, session_id: str):
        directory = self._directory(session_id)
        signature = self._signature(directory)
        cached = self._cache.get(session_id)
        if cached is not None and cached[0] == signature:
            return copy.deepcopy(cached[1]), cached[2], cached[3]
        manifest_path = directory / "manifest.json"
        if manifest_path.stat().st_size > MAX_METADATA_BYTES:
            raise RadarRecordingCorruptError("Oversized radar manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RadarRecordingCorruptError("Unreadable radar manifest") from exc
        if (not isinstance(manifest, dict) or manifest.get("id") != session_id
                or manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("kind") != "passive_radar"
                or not isinstance(manifest.get("config"), dict)
                or not isinstance(manifest.get("simulated"), bool)
                or not isinstance(manifest.get("max_session_bytes"), int)
                or manifest["max_session_bytes"] <= 0
                or manifest.get("state") not in ("recording", "stopped")):
            raise RadarRecordingCorruptError("Invalid radar manifest")
        events, committed_end, warnings = [], 0, []
        with (directory / "events.jsonl").open("rb") as stream:
            while line := stream.readline(MAX_EVENT_BYTES + 1):
                if len(line) > MAX_EVENT_BYTES:
                    raise RadarRecordingCorruptError("Oversized radar event")
                if not line.endswith(b"\n"):
                    warnings.append("Incomplete final event ignored")
                    break
                try:
                    event = json.loads(line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise RadarRecordingCorruptError("Malformed committed radar event") from exc
                if (not isinstance(event, dict) or event.get("session_id") != session_id
                        or event.get("sequence") != len(events)
                        or not isinstance(event.get("metadata"), dict)
                        or not isinstance(event.get("iq_bytes"), int) or event["iq_bytes"] < 0
                        or not isinstance(event.get("stored_bytes"), int) or event["stored_bytes"] < event["iq_bytes"]
                        or not _IQ_PATH.fullmatch(str(event.get("iq_path", "")))
                        or (event.get("result_path") is not None
                            and not _RESULT_PATH.fullmatch(str(event["result_path"])))
                        or (event.get("result") is not None and not isinstance(event["result"], dict))):
                    raise RadarRecordingCorruptError("Invalid committed radar event")
                try:
                    meta = event["metadata"]
                    count = (meta["sample_end"] - meta["sample_start"]) // meta.get("decimation_factor", 1)
                    if not 0 < count <= MAX_PAIR_SAMPLES:
                        raise ValueError("Invalid committed radar sample span")
                    _metadata(meta, count, manifest["simulated"])
                except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                    raise RadarRecordingCorruptError("Invalid committed radar metadata") from exc
                events.append(event)
                if len(events) > MAX_FRAMES:
                    raise RadarRecordingCorruptError("Radar frame count exceeds limit")
                committed_end = stream.tell()
        counts = {"frame_count": len(events), "iq_bytes": sum(e["iq_bytes"] for e in events),
                  "stored_bytes": sum(e["stored_bytes"] for e in events)}
        changed = any(manifest.get(k) != v for k, v in counts.items())
        manifest.update(counts)
        if warnings:
            prior = manifest.get("recovery_warnings", [])
            merged = list(dict.fromkeys(prior + warnings))
            changed |= prior != merged
            manifest["recovery_warnings"] = merged
        if changed:
            _atomic_json(manifest_path, manifest)
        self._cache[session_id] = (self._signature(directory), copy.deepcopy(manifest), events, committed_end)
        if len(self._cache) > 4:
            self._cache.pop(next(iter(self._cache)))
        return manifest, events, committed_end

    def start(self, config: dict, name: str = "", simulated: bool = False) -> dict:
        if not isinstance(config, dict) or not isinstance(name, str) or len(name) > 200:
            raise ValueError("Invalid radar recording name/configuration")
        config = _bounded_json(config, MAX_METADATA_BYTES // 2)[0]
        with self._lock:
            session_id = uuid.uuid4().hex
            directory = self.root / session_id
            directory.mkdir(mode=0o700)
            (directory / "iq").mkdir(mode=0o700)
            (directory / "results").mkdir(mode=0o700)
            manifest = {"schema_version": SCHEMA_VERSION, "kind": "passive_radar", "id": session_id,
                        "name": name.strip() or "Passivradar", "config": config, "simulated": bool(simulated),
                        "source_mode": "simulation" if simulated else "live",
                        "started_at": _now(), "stopped_at": None, "state": "recording", "frame_count": 0,
                        "iq_bytes": 0, "stored_bytes": 0, "max_session_bytes": self.max_session_bytes}
            (directory / "events.jsonl").touch()
            _atomic_json(directory / "manifest.json", manifest)
            _sync_directory(self.root)
            return copy.deepcopy(manifest)

    def append(self, session_id: str, iq: np.ndarray, metadata: dict,
               result: dict | None = None) -> dict:
        pair = _pair(iq)
        if result is not None and not isinstance(result, dict):
            raise ValueError("Radar result must be an object")
        normalized_result, result_bytes = _bounded_json(result, MAX_RESULT_BYTES) if result is not None else (None, b"")
        with self._lock:
            manifest, events, committed_end = self._load(session_id)
            if manifest["state"] != "recording":
                raise ValueError("Radar recording has already stopped")
            if len(events) >= MAX_FRAMES:
                raise RadarStorageLimitError("Maximale Zahl gespeicherter Radarfenster erreicht.")
            if committed_end + MAX_EVENT_BYTES > MAX_EVENT_LOG_BYTES:
                raise RadarStorageLimitError("Radar-Ereignisprotokoll hat das Aufnahmelimit erreicht.")
            metadata = _metadata(metadata, pair.shape[1], manifest["simulated"])
            if events:
                previous = events[-1]["metadata"]
                contiguous = (previous.get("stream_epoch") == metadata["stream_epoch"]
                              and previous.get("cpi_sample_end") == metadata.get("cpi_sample_start"))
                # Delay-prefix windows intentionally overlap; only explicit
                # CPI bounds establish whether adjacent results are contiguous.
                if "cpi_sample_end" in previous and "cpi_sample_start" in metadata:
                    metadata["recording_discontinuity"] = not contiguous
            needed = pair.nbytes + len(result_bytes) + 4096
            budget = min(self.max_session_bytes, manifest.get("max_session_bytes", self.max_session_bytes))
            if manifest["stored_bytes"] + needed > budget:
                raise RadarStorageLimitError("Radar-Aufnahmelimit erreicht; Aufnahme stoppen, Live-Verarbeitung kann weiterlaufen.")
            if shutil.disk_usage(self.root).free < needed + self.min_free_bytes:
                raise RadarStorageLimitError("Zu wenig freier Speicher für die nächste Radaraufnahme.")
            directory = self._directory(session_id)
            for subdir in (directory / "iq", directory / "results"):
                if subdir.is_symlink() or not subdir.is_dir():
                    raise RadarRecordingCorruptError("Invalid radar data directory")
            sequence = len(events)
            token = f"{sequence:08d}-{uuid.uuid4().hex}"
            iq_relative = f"iq/{token}.npy"
            iq_path = directory / iq_relative
            temporary = iq_path.with_suffix(".tmp")
            try:
                with temporary.open("xb") as stream:
                    np.save(stream, pair, allow_pickle=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, iq_path)
                _sync_directory(iq_path.parent)
            finally:
                temporary.unlink(missing_ok=True)
            result_relative = None
            result_size = 0
            if normalized_result is not None:
                result_relative = f"results/{token}.json"
                _atomic_json(directory / result_relative, normalized_result)
                result_size = (directory / result_relative).stat().st_size
            iq_size = iq_path.stat().st_size
            event = {"session_id": session_id, "sequence": sequence, "metadata": metadata,
                     "iq_path": iq_relative, "iq_bytes": iq_size, "result_path": result_relative,
                     "result": _summary(normalized_result) if normalized_result is not None else None,
                     "stored_bytes": iq_size + result_size}
            _, event_bytes = _bounded_json(event, MAX_EVENT_BYTES - 1)
            with (directory / "events.jsonl").open("r+b") as stream:
                stream.truncate(committed_end)
                stream.seek(committed_end)
                stream.write(event_bytes + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
                new_end = stream.tell()
            manifest.update(frame_count=sequence + 1, iq_bytes=manifest["iq_bytes"] + iq_size,
                            stored_bytes=manifest["stored_bytes"] + event["stored_bytes"])
            # The event is already committed if this final manifest write fails.
            self._cache.pop(session_id, None)
            _atomic_json(directory / "manifest.json", manifest)
            events = events + [event]
            self._cache[session_id] = (self._signature(directory), copy.deepcopy(manifest), events, new_end)
            return copy.deepcopy(event)

    def get(self, session_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._load(session_id)[0])

    def stop(self, session_id: str) -> dict:
        with self._lock:
            manifest, _, _ = self._load(session_id)
            if manifest["state"] == "recording":
                manifest.update(state="stopped", stopped_at=_now())
                _atomic_json(self._directory(session_id) / "manifest.json", manifest)
                self._cache.pop(session_id, None)
            return copy.deepcopy(manifest)

    def list_sessions(self) -> list[dict]:
        sessions = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not _ID.fullmatch(directory.name):
                continue
            try:
                sessions.append(self.get(directory.name))
            except (OSError, ValueError) as exc:
                sessions.append({"id": directory.name, "kind": "passive_radar", "state": "corrupt",
                                 "name": "Beschädigte Radaraufnahme", "error": str(exc)})
        return sorted(sessions, key=lambda row: row.get("started_at", ""), reverse=True)

    def iter_events(self, session_id: str) -> Iterator[dict]:
        with self._lock:
            events = copy.deepcopy(self._load(session_id)[1])
        yield from events

    def load_pair(self, event: dict) -> np.ndarray:
        with self._lock:
            manifest = self._manifest_for_event(event)
            path = self._file(event, "iq_path", _IQ_PATH)
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size != event["iq_bytes"]
                    or info.st_size > 2 * MAX_PAIR_SAMPLES * 8 + 65536):
                raise RadarRecordingCorruptError("Oversized radar IQ file")
            try:
                # Mapping checks the header against the file size before any
                # shape-dependent allocation; malicious headers cannot ask the
                # replay loader to allocate an unbounded array.
                mapped = np.load(path, allow_pickle=False, mmap_mode="r")
                try:
                    _pair(mapped)
                    _metadata(event["metadata"], mapped.shape[1], manifest["simulated"])
                    pair = np.array(mapped, copy=True)
                finally:
                    if isinstance(mapped, np.memmap):
                        mapped._mmap.close()
            except (OSError, ValueError, EOFError, KeyError) as exc:
                raise RadarRecordingCorruptError("Unreadable or inconsistent radar IQ") from exc
            return pair

    def _manifest_for_event(self, event):
        session_id = event.get("session_id")
        manifest, events, _ = self._load(session_id)
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or not 0 <= sequence < len(events) or events[sequence] != event:
            raise RadarRecordingCorruptError("Radar event does not match its committed session")
        return manifest

    def load_result(self, event: dict) -> dict | None:
        with self._lock:
            self._manifest_for_event(event)
            if event.get("result_path") is None:
                return None
            path = self._file(event, "result_path", _RESULT_PATH)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES + 1:
                raise RadarRecordingCorruptError("Oversized radar result")
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise RadarRecordingCorruptError("Unreadable radar result") from exc
            if not isinstance(result, dict):
                raise RadarRecordingCorruptError("Radar result is not an object")
            return result

    def export_xlsx(self, session_id: str) -> bytes:
        with self._lock:
            manifest = self.get(session_id)
            events = list(self.iter_events(session_id))
        workbook = Workbook()
        workbook.remove(workbook.active)
        summary = [{"key": key, "value": value} for key, value in manifest.items()]
        _table(workbook, "Radarsitzung", [("key", "Schlüssel"), ("value", "Wert")], summary)
        frames, candidates = [], []
        for event in events:
            row = {**event["metadata"], "sequence": event["sequence"], "iq_bytes": event["iq_bytes"],
                   "result": event.get("result")}
            frames.append(row)
            result = event.get("result") or {}
            found = result.get("candidates", result.get("detections", result.get("targets", [])))
            for candidate in found:
                if isinstance(candidate, dict):
                    candidates.append({**row, **candidate, "candidate_json": candidate})
        if len(candidates) > 1_048_575:
            raise RadarStorageLimitError("Zu viele Kandidaten für ein Excel-Tabellenblatt.")
        _table(workbook, "Radarfenster", [
            ("sequence", "Fenster"), ("timestamp", "Empfangszeit UTC"),
            ("source_mode", "Quelle"), ("simulated", "Simuliert"),
            ("stream_epoch", "Stream-Epoche"), ("sample_start", "Sample Start"),
            ("sample_end", "Sample Ende"), ("sample_rate_hz", "IQ-Abtastrate Hz"),
            ("frequency_hz", "RF-Frequenz Hz"), ("iq_stage", "IQ-Stufe"),
            ("recording_discontinuity", "Aufnahmelücke"), ("iq_bytes", "IQ-Dateigröße Bytes"),
            ("result", "Ergebnisdetails")], frames)
        _table(workbook, "Radarkandidaten", [
            ("sequence", "Fenster"), ("timestamp", "Empfangszeit UTC"),
            ("source_mode", "Quelle"), ("simulated", "Simuliert"),
            ("delay_samples", "Verzögerung Samples"), ("delay_seconds", "Verzögerung s"),
            ("excess_path_m", "Bistatischer Mehrweg m"), ("doppler_hz", "Doppler Hz"),
            ("excess_path_rate_m_s", "Mehrwegänderung m/s"),
            ("power_db", "Korrelationspegel dB"),
            ("contrast_db", "Lokaler Kontrast dB"), ("accepted", "Akzeptiert"),
            ("flags", "Kandidatenmerkmale"), ("candidate_json", "Kandidat vollständig JSON")], candidates)
        workbook.properties.title = "KrakenDF Passivradar"
        workbook.properties.description = "Bistatische Verzögerung und Doppler; keine direkte Zielentfernung oder Fluggeschwindigkeit"
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()
