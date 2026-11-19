"""Independent passive-radar acquisition, recording and replay lifecycle.

The parent owns receiver arbitration: it must stop the DF consumer before
starting this service. This class closes only its own IQ connection and the
managed DAQ acquisition it started. Simulation is an explicit IQ fixture, not
a fallback for failed hardware or evidence of aircraft detection.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable

import numpy as np

from .diagnostics import DiagnosticLog
from .hardware import HeimdallClient, IQFrame
from .managed_daq import ManagedDAQ
from .passive_radar import (PassiveRadarConfig, PassiveRadarProcessor,
                            RadarSimulator, analyze_window)
from .radar_storage import RadarStore


def _json(value: Any) -> Any:
    """Normalize only published metadata; raw IQ never enters state snapshots."""
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json(value.tolist())
    if isinstance(value, np.generic):
        return _json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _next(iterator):
    # StopIteration cannot be propagated through an asyncio Future.
    return next(iterator, None)


def _timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        value = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Radar-Replay enthält einen ungültigen Zeitstempel.")
    return result


class PassiveRadarService:
    """One bounded RF pipeline, with separate settings and session storage."""

    def __init__(self, data_dir: Path, daq: ManagedDAQ,
                 diagnostic_log: DiagnosticLog,
                 event_callback: Callable[[str, str], None] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "radar-config.json"
        self.daq, self.diagnostic_log = daq, diagnostic_log
        self.event_callback = event_callback
        self.store = RadarStore(self.data_dir / "radar-sessions")
        self.config = PassiveRadarConfig()
        self._saved_config: PassiveRadarConfig | None = None
        self._task: asyncio.Task | None = None
        self.client: HeimdallClient | None = None
        self._owns_daq = False
        self._lifecycle = asyncio.Lock()
        self._record_lock = asyncio.Lock()
        self._clock, self._sleep = time.monotonic, asyncio.sleep
        self.recording: dict | None = None
        self.replay: dict | None = None
        self._sessions: list[dict] = []
        self._state = self._new_state()
        if self.config_path.exists():
            try:
                if self.config_path.is_symlink():
                    raise ValueError("Passivradar-Konfiguration darf keine Verknüpfung sein.")
                if self.config_path.stat().st_size > 64 * 1024:
                    raise ValueError("Passivradar-Konfiguration ist zu groß.")
                self.config = PassiveRadarConfig.model_validate_json(
                    self.config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self._state.update(status="error", message=f"Radar-Konfiguration: {exc}")
                self._exception("Radar-Konfiguration", exc)
        self._refresh_sessions()

    @staticmethod
    def _new_state() -> dict:
        return {"mode": "idle", "status": "stopped", "message": "Passivradar gestoppt.",
                "simulated": False, "result": None, "received_frames": 0,
                "dropped_frames": 0, "processed_cpis": 0, "processing_ms": 0.0,
                "rf_duration_ms": 0.0, "queue_depth": 0, "queue_capacity": 3,
                "queue_wait_ms": 0.0, "quality": {}, "hardware_validated": False}

    @property
    def mode(self) -> str:
        return self._state["mode"]

    def _event(self, message: str, level: str = "info") -> None:
        if self.event_callback is not None:
            self.event_callback(message, level)
        else:
            self.diagnostic_log.write(message, level)

    def _exception(self, context: str, exc: BaseException) -> None:
        self.diagnostic_log.exception(context, exc)

    def _refresh_sessions(self) -> None:
        try:
            self._sessions = self.store.list_sessions()
        except Exception as exc:
            self._exception("Radar-Sitzungsverzeichnis", exc)
            self._event(f"Radar-Aufnahmen konnten nicht gelesen werden: {exc}", "error")

    def snapshot(self) -> dict:
        # Results are already bounded by the validated CAF dimensions. Keeping
        # only the latest result prevents a running display from collecting IQ.
        return _json(deepcopy({**self._state, "config": self.config.model_dump(),
                               "recording": self.recording, "sessions": self._sessions,
                               "replay": self.replay}))

    async def update_config(self, patch: dict) -> dict:
        async with self._lifecycle:
            if self.mode != "idle" or self.recording is not None or self.replay is not None:
                raise ValueError("Passivradar vor einer Konfigurationsänderung stoppen.")
            updated = PassiveRadarConfig.model_validate({**self.config.model_dump(), **patch})
            temporary = self.config_path.with_name("radar-config.json.tmp")
            if self.config_path.is_symlink() or temporary.is_symlink():
                raise ValueError("Passivradar-Konfiguration darf keine Verknüpfung sein.")
            temporary.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, self.config_path)
            self.config = updated
            return self.config.model_dump()

    async def start(self, mode: str = "live", managed: bool = True,
                    host: str = "127.0.0.1", port: int = 5000) -> dict:
        if mode not in ("live", "simulation"):
            raise ValueError("Radarquelle muss 'live' oder 'simulation' sein.")
        if not isinstance(host, str) or not host.strip() or not 1 <= int(port) <= 65535:
            raise ValueError("Gültigen Heimdall-Host und Port angeben.")
        async with self._lifecycle:
            if self.recording is not None:
                raise ValueError("Radar-Aufnahme vor dem Quellenwechsel stoppen.")
            await self._stop()
            self._state = {**self._new_state(), "mode": mode, "status": "preparing",
                           "message": "Passivradar wird vorbereitet …",
                           "simulated": mode == "simulation", "managed": bool(managed),
                           "host": host, "port": int(port)}
            self._task = asyncio.create_task(self._acquisition_loop(), name="passive-radar")
            return self.snapshot()

    async def stop(self) -> dict:
        async with self._lifecycle:
            await self._stop()
            return self.snapshot()

    async def _stop(self) -> None:
        # Cancel processing before finalizing a recording, so no result can be
        # appended to a session after the stop button has finalized it.
        task, self._task = self._task, None
        try:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            try:
                await self.stop_recording()
            finally:
                await self._release_receiver()
                if self._saved_config is not None:
                    self.config, self._saved_config = self._saved_config, None
                self.replay = None
                self._state.update(mode="idle", status="stopped", simulated=False,
                                   message="Passivradar gestoppt.", queue_depth=0,
                                   result=None, quality={})

    async def close(self) -> None:
        await self.stop()

    async def _release_receiver(self) -> None:
        client, self.client = self.client, None
        try:
            if client is not None:
                await client.close()
        finally:
            if self._owns_daq:
                self._owns_daq = False
                await self.daq.stop()

    async def _compute(self, function, *args):
        # A cancelled to_thread Future leaves native DSP running. Wait for the
        # one in-flight block before sharing the receiver with another mode.
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with suppress(Exception):
                await worker
            raise

    def _valid_frame(self, frame: IQFrame) -> tuple[bool, str]:
        quality = frame.quality
        reasons = list(quality.get("reasons", []))
        for key, reason in (("delay_lock", "delay_not_locked"), ("iq_lock", "iq_not_locked")):
            if key in quality and not quality[key] and reason not in reasons:
                reasons.append(reason)
        if quality.get("noise_source"):
            reasons.append("calibration_noise_active")
        if quality.get("overdrive_flags"):
            reasons.append("adc_overdrive")
        if quality.get("frame_type", 0) != 0:
            reasons.append("not_a_data_frame")
        if not quality.get("valid") or reasons:
            return False, ", ".join(dict.fromkeys(reasons)) or "Kohärenz nicht bereit"
        gains = quality.get("gains_db", [])
        mismatch = (abs(frame.center_frequency_hz - self.config.center_frequency_hz) > 1
                    or abs(frame.sample_rate_hz - self.config.sample_rate_hz) > 1
                    or len(gains) != 5
                    or any(abs(gain - self.config.gain_db) > .11 for gain in gains))
        if mismatch:
            return False, "Heimdall-Istwerte für Frequenz, Rate oder Gain stimmen nicht mit dem Radarprofil überein."
        return True, ""

    async def _acquisition_loop(self) -> None:
        pump = None
        queue = asyncio.Queue(maxsize=3)
        simulated = self.mode == "simulation"
        sample_start = 0
        previous_epoch, expected_sample = None, None
        try:
            processor = PassiveRadarProcessor(self.config)
            if simulated:
                simulator = RadarSimulator(self.config)
                count = min(262144, max(4096, round(self.config.sample_rate_hz * .1)))
                self._event("Radar-Simulation: synthetische kohärente IQ-Daten; keine reale Zielerkennung.")
            else:
                self.client = HeimdallClient(self._state["host"], self._state["port"],
                                             timeout=30 if self._state["managed"] else 10)
                if self._state["managed"]:
                    self._state.update(status="starting", message="Lokaler Kraken-Stack für Radar wird gestartet …")
                    self._owns_daq = True
                    await self.daq.start(self.config.model_dump())
                self._state.update(status="connecting", message="Heimdall-IQ wird verbunden …")
                await self.client.connect()
                await self.client.configure(center_frequency_hz=self.config.center_frequency_hz,
                                            gain_db=self.config.gain_db)

                async def read_frames():
                    while True:
                        frame = await self.client.read_frame()
                        self._state["received_frames"] += 1
                        if queue.full():
                            self._state["dropped_frames"] += 1
                            raise RuntimeError("Radar-Verarbeitung zu langsam; IQ-Warteschlange voll. CPI, Verzögerungsbins oder Host-Leistung anpassen.")
                        queue.put_nowait((frame, time.perf_counter()))
                        self._state["queue_depth"] = queue.qsize()

                pump = asyncio.create_task(read_frames(), name="radar-iq-reader")
            while True:
                cycle_started = time.perf_counter()
                if simulated:
                    frame = await self._compute(simulator.frame, sample_start, count)
                    sample_start += count
                    queued_at = time.perf_counter()
                    self._state["received_frames"] += 1
                else:
                    if pump.done():
                        await pump
                    try:
                        frame, queued_at = await asyncio.wait_for(queue.get(), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                self._state.update(queue_depth=queue.qsize(),
                                   queue_wait_ms=(time.perf_counter() - queued_at) * 1000,
                                   quality=_json(frame.quality),
                                   rf_duration_ms=frame.iq.shape[1] / frame.sample_rate_hz * 1000)
                valid, reason = self._valid_frame(frame)
                if (frame.quality.get("discontinuity") or previous_epoch != frame.stream_epoch
                        or (expected_sample is not None and expected_sample != frame.sample_start)):
                    self._state["result"] = None
                previous_epoch = frame.stream_epoch
                expected_sample = frame.sample_start + frame.iq.shape[1]
                if not valid:
                    # Flush channelizer and CPI tails, including on settings
                    # mismatches; later good frames must not bridge this gap.
                    invalid = IQFrame(frame.iq, frame.sample_rate_hz, frame.center_frequency_hz,
                                      frame.sample_start, frame.stream_epoch, frame.timestamp,
                                      {**frame.quality, "valid": False, "reasons": [reason]})
                    await self._compute(processor.process, invalid)
                    self._state.update(status="mismatch" if "Istwerte" in reason else "calibrating",
                                       message=reason, result=None)
                    continue
                self._state.update(status="running", message=("Synthetische IQ-Simulation · zwei Testreflexionen"
                                                              if simulated else "Kohärente IQ-Daten · Passivradar"))
                began = time.perf_counter()
                results = await self._compute(processor.process, frame)
                self._state["processing_ms"] = (time.perf_counter() - began) * 1000
                if hasattr(processor, "status"):
                    self._state["integration"] = processor.status()
                for result in results:
                    await self._publish_result(result.data, result=result)
                if simulated:
                    duration = count / self.config.sample_rate_hz
                    await asyncio.sleep(max(0, duration - (time.perf_counter() - cycle_started)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state.update(status="error", message=str(exc), result=None)
            self._event(f"Passivradar: {exc}", "error")
            self._exception("Passivradar-Erfassung", exc)
            self.diagnostic_log.write("Radar-Leistung: " + json.dumps({key: self._state[key] for key in
                ("received_frames", "dropped_frames", "processed_cpis", "processing_ms", "rf_duration_ms", "queue_depth")}), "error")
        finally:
            if pump is not None:
                pump.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pump
            try:
                await self.stop_recording()
            finally:
                await self._release_receiver()

    async def _publish_result(self, data: dict, result=None) -> None:
        published = _json(data)
        published["simulated"] = bool(self._state["simulated"])
        if self._state["simulated"]:
            published["fixture"] = "synthetic_coherent_broadcast_two_reflections_not_aircraft_detection"
        self._state["result"] = published
        self._state["processed_cpis"] += 1
        if result is None:
            return
        async with self._record_lock:
            if self.recording is None:
                return
            session_id = self.recording["id"]
            metadata = {"sample_rate_hz": result.sample_rate_hz,
                        "frequency_hz": self.config.center_frequency_hz,
                        "sample_start": result.sample_start, "sample_end": result.sample_end,
                        "stream_epoch": result.stream_epoch, "epoch": result.stream_epoch,
                        "timestamp": result.timestamp,
                        "reference_channel": self.config.reference_channel,
                        "surveillance_channel": self.config.surveillance_channel,
                        "decimation_factor": round(self.config.sample_rate_hz / result.sample_rate_hz),
                        "iq_stage": "channelized_pair_with_prefix"}
            for key in ("cpi_sample_start", "cpi_sample_end"):
                if key in result.data:
                    metadata[key] = result.data[key]
            try:
                await self._compute(self.store.append, session_id, result.iq, metadata, published)
                self.recording = self.store.get(session_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event(f"Radar-Aufnahme gestoppt: {exc}", "error")
                self._exception("Radar-Aufzeichnung", exc)
                try:
                    await self._finish_recording_locked()
                except Exception:
                    # Finalization was logged by the helper. Keep the RF
                    # receiver alive even when storage is no longer writable.
                    pass

    async def start_recording(self, name: str = "") -> dict:
        async with self._record_lock:
            if self.recording is not None:
                raise ValueError("Eine Radar-Aufnahme läuft bereits.")
            if self.mode not in ("live", "simulation") or self._state["status"] != "running":
                raise ValueError("Eine laufende Radarquelle vor der Aufnahme starten.")
            self.recording = self.store.start(self.config.model_dump(), name=name,
                                              simulated=self._state["simulated"])
            self._refresh_sessions()
            self._event("Radar-IQ-Aufnahme gestartet.")
            return deepcopy(self.recording)

    async def _finish_recording_locked(self) -> dict | None:
        recording, self.recording = self.recording, None
        if recording is None:
            return None
        try:
            finished = self.store.stop(recording["id"])
            self._event("Radar-IQ-Aufnahme beendet.")
            return finished
        except Exception as exc:
            self._exception("Radar-Aufnahme abschließen", exc)
            self._event(f"Radar-Aufnahme konnte nicht abgeschlossen werden: {exc}", "error")
            raise
        finally:
            self._refresh_sessions()

    async def stop_recording(self) -> dict | None:
        async with self._record_lock:
            return await self._finish_recording_locked()

    async def start_replay(self, session_id: str, speed: float = 1) -> dict:
        if not math.isfinite(float(speed)) or not .1 <= float(speed) <= 20:
            raise ValueError("Replay-Geschwindigkeit muss zwischen 0,1 und 20 liegen.")
        async with self._lifecycle:
            if self.recording is not None:
                raise ValueError("Radar-Aufnahme vor Replay stoppen.")
            manifest = self.store.get(session_id)
            if not manifest.get("frame_count"):
                raise ValueError("Radar-Aufnahme enthält keine IQ-Fenster.")
            if not manifest.get("stopped_at"):
                raise ValueError("Radar-Aufnahme vor Replay abschließen.")
            replay_config = PassiveRadarConfig.model_validate(manifest["config"])
            await self._stop()
            self._saved_config, self.config = self.config, replay_config
            self.replay = {"session_id": session_id, "speed": float(speed), "processed": 0,
                           "total": manifest["frame_count"], "progress": 0.0}
            self._state = {**self._new_state(), "mode": "replay", "status": "preparing",
                           "message": "Radar-IQ-Replay wird vorbereitet …",
                           "simulated": bool(manifest.get("simulated"))}
            self._task = asyncio.create_task(self._replay_loop(session_id, float(speed)),
                                             name="passive-radar-replay")
            return self.snapshot()

    async def stop_replay(self) -> dict:
        return await self.stop()

    async def _replay_loop(self, session_id: str, speed: float) -> None:
        previous_timestamp = None
        origin_timestamp = None
        origin_clock = self._clock()
        try:
            events = iter(self.store.iter_events(session_id))
            while (event := await self._compute(_next, events)) is not None:
                metadata = event.get("metadata", event)
                timestamp = _timestamp(metadata.get("timestamp"))
                if timestamp is not None:
                    if origin_timestamp is None or (previous_timestamp is not None and timestamp < previous_timestamp):
                        # A DAQ-host clock reset starts a new pacing epoch.
                        origin_timestamp, origin_clock = timestamp, self._clock()
                    deadline = origin_clock + (timestamp - origin_timestamp) / speed
                    await self._sleep(max(0, deadline - self._clock()))
                previous_timestamp = timestamp
                metadata = {**metadata, "timestamp": timestamp,
                            "timestamp_unix": timestamp} if timestamp is not None else metadata
                pair = await self._compute(self.store.load_pair, event)
                began = time.perf_counter()
                data = await self._compute(analyze_window, pair, metadata["sample_rate_hz"],
                                           self.config, metadata)
                self._state.update(status="running", message="Gespeicherte kohärente IQ-Fenster werden neu ausgewertet.",
                                   processing_ms=(time.perf_counter() - began) * 1000,
                                   rf_duration_ms=pair.shape[1] / metadata["sample_rate_hz"] * 1000,
                                   received_frames=self._state["received_frames"] + 1)
                await self._publish_result(data)
                self.replay["processed"] += 1
                self.replay["progress"] = min(1, self.replay["processed"] / self.replay["total"])
            self._state.update(status="finished", message="Radar-Replay beendet.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state.update(status="error", message=str(exc), result=None)
            self._exception("Radar-Replay", exc)
            self._event(f"Radar-Replay: {exc}", "error")
