"""Application orchestration for live, synthetic-IQ and recorded observations."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid

import numpy as np

from .config import AppConfig, load_presets, positions_for
from .decoders import decode_lora, decode_pocsag, decoder_capabilities, parse_bft_payload
from .dsp import (apply_calibration, bearing_deg, destination, estimate_doa,
                  fit_calibration, statistics, steering_vector, wrap180)
from .hardware import GPSReader, HeimdallClient, MANUAL_GAINS_DB
from .pipeline import LivePipeline, spectrum
from .storage import SessionStore
from .managed_daq import ManagedDAQ


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return clean(value.tolist())
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def save_json(path: Path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(clean(data), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class DemoService:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = data_dir / "config.json"
        self.config = AppConfig.model_validate_json(self.config_path.read_text()) if self.config_path.exists() else AppConfig()
        self.store = SessionStore(data_dir / "sessions")
        self.models = self._load_json("calibrations.json", [])
        self.symbols = self._load_json("symbols.json", {})
        self.active_calibration_id = None
        daq_mode = os.environ.get("KRAKENDF_DAQ_MODE", "managed")
        self.source = {"mode": "idle", "status": "stopped", "message": "Bereit", "simulated": False,
                       "host": os.environ.get("KRAKENDF_DAQ_HOST", "127.0.0.1"),
                       "port": int(os.environ.get("KRAKENDF_DAQ_PORT", "5000")),
                       "managed": daq_mode == "managed", "received_frames": 0, "dropped_frames": 0}
        runtime_dir = Path(os.environ.get("KRAKENDF_RUNTIME", Path(__file__).resolve().parent.parent / ".runtime"))
        self.daq = ManagedDAQ(runtime_dir, data_dir / "logs" / "daq")
        self.gps = {"model": "ARCELI VK-162", "status": "not_configured", "valid": False,
                    "latitude": None, "longitude": None, "satellites": None, "age_s": None}
        self.gps_anchor = None
        self.gps_fixes = deque(maxlen=10)
        self.events = deque(maxlen=80)
        self.observations = deque(maxlen=20000)
        self.emitters = {}
        self.spectrum = {"start_hz": self.config.center_frequency_hz - self.config.sample_rate_hz / 2,
                         "step_hz": self.config.sample_rate_hz / 512, "power_db": []}
        self.stats = statistics([])
        self.recording = None
        self.replay = None
        self._restore_config = None
        self.run_id = uuid.uuid4().hex
        self._task = None
        self._gps_task = None
        self.client = None
        self.gps_reader = None
        self.revision = 0
        self.control = asyncio.Lock()
        self._last_decode_error = 0
        self._sim_rng = np.random.default_rng(433)
        self._sim_index = 0
        self._sim_start = time.monotonic()
        self._sessions_cache = []
        self._sessions_checked = 0

    def _load_json(self, name, default):
        path = self.data_dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def event(self, message: str, level: str = "info"):
        self.events.appendleft({"time": utcnow(), "level": level, "message": message})

    async def start(self, initial_mode="live"):
        self.event("KrakenDF bereit. Standardquelle ist der lokale KrakenSDR; Simulation ist ausdrücklich wählbar.")
        await self.set_source(initial_mode)
        await self._start_gps()

    async def close(self):
        if self.recording:
            await self.stop_recording()
        await self._stop_source()
        await self._stop_gps()

    async def _stop_source(self):
        task, self._task = self._task, None
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.client:
            await self.client.close()
            self.client = None
        await self.daq.stop()

    async def _stop_gps(self):
        if self._gps_task:
            self._gps_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._gps_task
            self._gps_task = None
        if self.gps_reader:
            await self.gps_reader.close()
            self.gps_reader = None

    async def _start_gps(self):
        await self._stop_gps()
        if not self.config.gps_port and self.config.station_mode == "gps":
            try:
                from serial.tools import list_ports
                import re
                candidates = [p for p in list_ports.comports() if p.vid == 0x1546 or
                              re.search(r"u[- ]?blox|vk[- ]?162|gps|gnss", " ".join(str(v or "") for v in (p.description, p.manufacturer, p.product)), re.I)]
                if len(candidates) == 1:
                    self.config = self.config.model_copy(update={"gps_port": candidates[0].device})
                    save_json(self.config_path, self.config.model_dump())
                    self.event(f"GPS-Maus automatisch erkannt: {candidates[0].device}")
                else:
                    self.gps.update(status="not_found" if not candidates else "ambiguous", valid=False,
                                    message="VK-162 nicht eindeutig erkannt; GPS-Port im Setup wählen.")
            except ImportError:
                self.gps.update(status="missing_dependency", valid=False, message="USB-GPS benötigt pyserial; setup.sh ausführen.")
        if self.config.gps_port:
            self._gps_task = asyncio.create_task(self._gps_loop())

    async def _gps_loop(self):
        reader = GPSReader(self.config.gps_port, self.config.gps_baud)
        self.gps_reader = reader
        try:
            await reader.connect()
            while True:
                await reader.read_fix()
                status = reader.status()
                self.gps.update(status)
                self.gps["status"] = "fix" if status.get("valid") else "no_fix"
                self.gps["age_s"] = status.get("age_seconds")
                if status.get("valid") and status.get("latitude") is not None and not self.recording:
                    self.gps_fixes.append((status["latitude"], status["longitude"]))
                    # A fixed receiver uses a short averaged anchor, never a
                    # moving course-over-ground as its antenna heading.
                    if len(self.gps_fixes) == 10 and self.gps_anchor is None:
                        lat = float(np.mean([p[0] for p in self.gps_fixes]))
                        lon_rad = np.deg2rad([p[1] for p in self.gps_fixes])
                        lon = float(np.rad2deg(np.angle(np.mean(np.exp(1j * lon_rad)))))
                        self.gps_anchor = (lat, lon)
                        if self.config.station_mode == "gps" and self.source["mode"] != "replay":
                            self.config = self.config.model_copy(update={"station_latitude": lat, "station_longitude": lon})
                            save_json(self.config_path, self.config.model_dump())
                            self.active_calibration_id = None
                            self.reset_measurements()
                        self.event("GPS-Stationsposition aus zehn Fixes gemittelt und fixiert.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gps.update(status="error", valid=False, message=str(exc))
            self.event(f"GPS: {exc}", "warning")
        finally:
            await reader.close()

    def station(self):
        if self.source["mode"] == "replay":
            return self.config.station_latitude, self.config.station_longitude, True, "recorded"
        if self.config.station_mode == "manual":
            return self.config.station_latitude, self.config.station_longitude, True, "manual"
        if self.gps_anchor is not None:
            return *self.gps_anchor, True, "gps_locked"
        if self.source["mode"] == "simulation":
            return self.config.station_latitude, self.config.station_longitude, True, "simulation"
        return self.config.station_latitude, self.config.station_longitude, False, "gps_pending"

    def effective_config(self):
        cfg = self.config.model_dump()
        lat, lon, valid, mode = self.station()
        cfg.update(station_latitude=lat, station_longitude=lon)
        return cfg

    async def set_source(self, mode, host=None, port=None, managed=None):
        if mode not in ("simulation", "live", "idle"):
            raise ValueError("Quelle muss simulation, live oder idle sein.")
        if self.recording:
            raise ValueError("Vor dem Quellenwechsel die laufende Aufnahme stoppen.")
        if host is not None and (not isinstance(host, str) or not host.strip() or len(host) > 253 or any(c.isspace() for c in host)):
            raise ValueError("Ungültiger Heimdall-Hostname.")
        if port is not None and not 1 <= int(port) <= 65535:
            raise ValueError("Ungültiger TCP-Port.")
        use_managed = self.source["managed"] if managed is None else bool(managed)
        candidate_host = host if host is not None else self.source["host"]
        if managed is None and host is not None and host not in ("127.0.0.1", "localhost", "::1"):
            use_managed = False
        if mode == "live" and use_managed and (candidate_host not in ("127.0.0.1", "localhost", "::1") or (port is not None and int(port) != 5000)):
            raise ValueError("Lokaler USB-Betrieb verwendet 127.0.0.1:5000. Für einen anderen Host externen Empfänger wählen.")
        await self._stop_source()
        if self._restore_config:
            self.config = self._restore_config
            self._restore_config = None
        self.replay = None
        self.active_calibration_id = None
        self.run_id = uuid.uuid4().hex
        self.revision += 1
        self.reset_measurements()
        self.source.update(mode=mode, simulated=(mode == "simulation"), status="connecting" if mode == "live" else "running" if mode == "simulation" else "stopped",
                           message="Synthetische IQ-Daten · beschleunigter Demoablauf" if mode == "simulation" else "Verbindung zu Heimdall" if mode == "live" else "Quelle angehalten")
        self.source["managed"] = use_managed
        if use_managed:
            self.source.update(host="127.0.0.1", port=5000)
        if host is not None:
            if not isinstance(host, str) or not host.strip() or len(host) > 253 or any(c.isspace() for c in host):
                raise ValueError("Ungültiger Heimdall-Hostname.")
            self.source["host"] = host
        if port is not None:
            if not 1 <= int(port) <= 65535:
                raise ValueError("Ungültiger TCP-Port.")
            self.source["port"] = int(port)
        if mode == "simulation":
            self._sim_start = time.monotonic()
            self._task = asyncio.create_task(self._simulation_loop())
        elif mode == "live":
            self._task = asyncio.create_task(self._live_loop())
        self.event(f"Quelle: {mode}")

    async def update_config(self, patch):
        if self.recording or self.replay:
            raise ValueError("Konfiguration erst nach Stoppen von Aufnahme oder Replay ändern.")
        candidate = AppConfig.model_validate({**self.config.model_dump(), **patch})
        old_mode = self.source["mode"]
        if old_mode == "live" and not self.source["managed"] and candidate.sample_rate_hz != self.config.sample_rate_hz:
            raise ValueError("Die Live-Abtastrate wird in Heimdall konfiguriert; danach hier übernehmen und neu verbinden.")
        if old_mode == "live" and candidate.gain_db not in MANUAL_GAINS_DB:
            raise ValueError("Heimdall benötigt einen unterstützten diskreten Gain-Wert.")
        if old_mode == "live" and not self.source["managed"] and self.client and self.client.connected:
            await self.client.configure(center_frequency_hz=candidate.center_frequency_hz, gain_db=candidate.gain_db)
        gps_changed = (candidate.gps_port, candidate.gps_baud) != (self.config.gps_port, self.config.gps_baud)
        self.config = candidate
        save_json(self.config_path, candidate.model_dump())
        self.active_calibration_id = None
        self.reset_measurements()
        self.revision += 1
        if gps_changed:
            self.gps_anchor = None
            self.gps_fixes.clear()
            await self._start_gps()
        if old_mode in ("live", "simulation"):
            await self.set_source(old_mode)
        self.event("Messprofil aktualisiert; neue Statistik und deaktivierte Winkelkorrektur.")

    def reset_measurements(self):
        self.observations.clear()
        self.emitters.clear()
        self.stats = statistics([])
        self.run_id = uuid.uuid4().hex

    def active_model(self):
        return next((m for m in self.models if m["id"] == self.active_calibration_id), None)

    def _build_observation(self, payload, estimate, meta, quality):
        parsed = parse_bft_payload(payload)
        if parsed is None:
            return None
        local = float(estimate["azimuth_deg"])
        raw = (local + self.config.heading_deg) % 360
        corrected = float(apply_calibration(raw, self.active_model())) if self.active_model() else raw
        lat, lon, station_valid, station_mode = self.station()
        reference = None
        if parsed.get("position_valid") and station_valid:
            with suppress(ValueError):
                reference = bearing_deg(lat, lon, parsed["latitude"], parsed["longitude"])
        quality = {**quality, "position_valid": parsed.get("position_valid", False),
                   "station_valid": station_valid, "station_source": station_mode,
                   "confidence_kind": estimate.get("confidence_kind")}
        accepted = bool(quality.get("valid", True) and reference is not None and estimate["confidence"] >= .2)
        return {**meta, **parsed, "payload": payload, "local_deg": local,
                "raw_deg": raw, "corrected_deg": corrected, "reference_deg": reference,
                "error_raw_deg": float(wrap180(raw-reference)) if reference is not None else None,
                "error_corrected_deg": float(wrap180(corrected-reference)) if reference is not None else None,
                "station_latitude": lat, "station_longitude": lon,
                "power_db": estimate["power_db"], "confidence": estimate["confidence"],
                "accepted": accepted, "quality": quality, "heading_deg": self.config.heading_deg,
                "array_id": self.config.array_id, "profile_id": self.config.profile_id,
                "calibration_id": self.active_calibration_id, "calibration_model": self.active_model(),
                "run_id": self.recording["id"] if self.recording else self.run_id,
                "timestamp": meta.get("timestamp", utcnow())}

    async def accept(self, observation, iq=None, spec=None, record=True):
        if observation is None:
            return
        observation = clean(observation)
        self.observations.append(observation)
        self.emitters[observation["callsign"]] = {**observation, "last_seen": observation["timestamp"], "_seen_at": time.time()}
        self.stats = statistics(list(self.observations))
        if spec and spec.get("power_db"):
            self.spectrum = spec
        if record and self.recording:
            try:
                await asyncio.to_thread(self.store.append, self.recording["id"], observation, iq, spec)
                self.recording = self.store.get(self.recording["id"])
                self._sessions_checked = 0
            except Exception as exc:
                self.event(f"Aufnahmefehler: {exc}", "error")
                failed, self.recording = self.recording, None
                with suppress(Exception):
                    self.store.stop(failed["id"])

    def synthetic_observation(self, reference, callsign, channel, index, run_id=None):
        """Known truth drives an actual five-channel MUSIC estimate.

        Packet identity in this mode comes from fixture metadata, not an RF
        decoder. This is explicit in every observation and recorded manifest.
        """
        rng = np.random.default_rng(433 + index)
        angle = math.radians(reference)
        bias = 4 * math.sin(2 * angle) + 1.8 * math.cos(angle)
        # The simulated physical array points north. Its physical IQ response
        # must remain independent of the user's configured heading offset.
        local = (reference + bias) % 360
        fs = max(50000, channel.bandwidth_hz * 2)
        count = 4096
        waveform = np.exp(2j * np.pi * np.cumsum(rng.choice([-1, 1], count)) * .07)
        vector = steering_vector(positions_for(self.config), channel.frequency_hz, local)
        noise = .12 * (rng.normal(size=(5, count)) + 1j * rng.normal(size=(5, count)))
        iq = np.asarray(vector[:, None] * waveform[None, :] + noise, dtype=np.complex64)
        estimate = estimate_doa(iq, positions_for(self.config), channel.frequency_hz)
        lat, lon, _, _ = self.station()
        txlat, txlon = destination(lat, lon, reference, 350 + index % 70)
        payload = f"{callsign} {txlat:.6f} {txlon:.6f} 112m {datetime.now(timezone.utc):%H%M%S}Z 09sat"
        meta = {"modulation": channel.modulation, "frequency_hz": channel.frequency_hz,
                "sample_rate_hz": fs, "sample_start": index * count, "sample_end": (index+1)*count,
                "stream_epoch": "simulated-fixture-v1", "burst_id": f"sim-{index}",
                "source_mode": "simulation", "simulated": True, "timestamp": utcnow(),
                "iq_frequency_hz": channel.frequency_hz, "iq_sample_rate_hz": fs}
        observation = self._build_observation(payload, estimate, meta,
                                              {"valid": True, "identity_source": "synthetic_fixture_metadata",
                                               "simulated": True, "decoder_verified": False})
        if run_id:
            observation["run_id"] = run_id
        return observation, iq

    def _sim_spectrum(self, phase):
        rng = np.random.default_rng(int(phase * 4) + 100)
        n = 768
        start = self.config.center_frequency_hz - self.config.sample_rate_hz / 2
        freq = start + np.arange(n) * self.config.sample_rate_hz / n
        power = -88 + rng.normal(0, 1.2, n)
        for ch in self.config.channels:
            if ch.enabled:
                width = ch.lora_bandwidth_hz if ch.modulation == "lora" else 10000
                shape = np.exp(-((freq - ch.frequency_hz) / (width * .55))**8)
                power += (35 + 4 * math.sin(phase)) * shape
        return {"start_hz": start, "step_hz": self.config.sample_rate_hz/n,
                "power_db": power.round(2).tolist(), "simulated": True}

    async def _simulation_loop(self):
        try:
            while True:
                revision = self.revision
                elapsed = time.monotonic() - self._sim_start
                channels = [c for c in self.config.channels if c.enabled]
                if channels:
                    for j, callsign in enumerate(("DL7CR", "DF2DEMO", "DO1BFT")):
                        reference = (elapsed * 10 + j * 120 + 15) % 360
                        self._sim_index += 1
                        observation, iq = await asyncio.to_thread(self.synthetic_observation, reference, callsign,
                                                                 channels[j % len(channels)], self._sim_index)
                        if revision != self.revision:
                            break
                        await self.accept(observation, iq, self._sim_spectrum(elapsed))
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.source.update(status="error", message=str(exc))
            self.event(f"Simulation: {exc}", "error")

    async def _live_loop(self):
        client = HeimdallClient(self.source["host"], self.source["port"], timeout=30 if self.source["managed"] else 10)
        self.client = client
        pump = None
        queue = asyncio.Queue(maxsize=3)

        async def read_frames():
            while True:
                frame = await client.read_frame()
                if queue.full():
                    self.source["dropped_frames"] += 1
                    self.event("DAQ-Verarbeitung überlastet: Datenlücke, betroffene Bursts werden verworfen.", "warning")
                    raise RuntimeError("Verarbeitung zu langsam; weniger Kanäle oder schnelleren Host verwenden.")
                await queue.put(frame)

        try:
            if self.source["managed"]:
                self.source.update(status="starting", message="Lokaler Kraken-Stack wird gestartet …")
                await self.daq.start(self.config.model_dump())
            await client.connect()
            await client.configure(center_frequency_hz=self.config.center_frequency_hz, gain_db=self.config.gain_db)
            pipeline = LivePipeline(self.config)
            pump = asyncio.create_task(read_frames())
            while True:
                if pump.done():
                    await pump
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                self.source["received_frames"] += 1
                if not frame.quality.get("valid"):
                    pipeline.process(frame)
                    self.source.update(status="calibrating", message=", ".join(frame.quality.get("reasons", ["Kohärenz nicht bereit"])))
                    continue
                mismatch = abs(frame.center_frequency_hz-self.config.center_frequency_hz) > 1 or abs(frame.sample_rate_hz-self.config.sample_rate_hz) > 1
                gains = frame.quality.get("gains_db", [])
                if gains and any(abs(g-self.config.gain_db) > .11 for g in gains[:5]):
                    mismatch = True
                if mismatch:
                    self.source.update(status="mismatch", message="Heimdall-Istwerte für Frequenz, Rate oder Gain stimmen noch nicht mit dem Messprofil überein.")
                    continue
                self.source.update(status="running", message="Kohärente Fünfkanaldaten · Heimdall")
                spec, bursts = await asyncio.to_thread(pipeline.process, frame)
                if spec:
                    self.spectrum = spec
                for burst in bursts:
                    await self._decode_burst(burst)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.source.update(status="error", message=str(exc))
            self.event(f"Kraken: {exc}", "error")
        finally:
            if pump:
                pump.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pump
            await client.close()
            if self.source["managed"]:
                await self.daq.stop()

    async def _decode_burst(self, burst):
        energy = np.mean(abs(burst.iq)**2, axis=1)
        antenna = int(np.argmax(energy))
        try:
            if burst.channel["modulation"] == "pocsag":
                packets = await asyncio.to_thread(decode_pocsag, burst.iq[antenna], burst.sample_rate_hz)
            else:
                packets = await asyncio.to_thread(decode_lora, burst.iq[antenna], burst.sample_rate_hz,
                                                  burst.channel.get("lora_sf", 9), burst.channel.get("lora_bandwidth_hz", 125000))
            if not packets:
                if time.time() - self._last_decode_error > 3:
                    self.event(f"{burst.channel['id']}: Burst erkannt, keine geprüfte Nutzlast.", "warning")
                    self._last_decode_error = time.time()
                return
            for number, packet in enumerate(packets):
                parsed = parse_bft_payload(packet.payload)
                if parsed is None:
                    self.event(f"{burst.channel['id']}: Nutzlast ohne gültiges BFT-Format.", "warning")
                    continue
                left = max(0, int(packet.sample_start))
                right = min(burst.iq.shape[1], int(packet.sample_end))
                if right-left < 16:
                    continue
                iq = burst.iq[:, left:right]
                estimate = await asyncio.to_thread(estimate_doa, iq, positions_for(self.config), burst.frequency_hz)
                factor = burst.quality.get("decimation_factor", 1)
                stamp = datetime.fromtimestamp(burst.timestamp + left / burst.sample_rate_hz, timezone.utc).isoformat()
                meta = {"modulation": burst.channel["modulation"], "frequency_hz": burst.frequency_hz,
                        "sample_rate_hz": self.config.sample_rate_hz,
                        "sample_start": burst.sample_start + left*factor, "sample_end": burst.sample_start+right*factor,
                        "iq_sample_start": burst.sample_start, "iq_sample_rate_hz": burst.sample_rate_hz,
                        "iq_frequency_hz": burst.frequency_hz, "iq_signal_start": left, "iq_signal_end": right,
                        "burst_id": f"{burst.channel['id']}-{burst.sample_start}-{number}",
                        "stream_epoch": burst.stream_epoch, "source_mode": "live", "simulated": False,
                        "timestamp": stamp, "decoder_antenna": antenna}
                obs = self._build_observation(packet.payload, estimate, meta, {**burst.quality, **packet.quality, "decoder_verified": True})
                await self.accept(obs, burst.iq, self.spectrum)
        except Exception as exc:
            self.event(f"Decoder {burst.channel['id']}: {exc}", "error")

    async def start_recording(self, name="", kind="measurement"):
        if self.recording:
            raise ValueError("Es läuft bereits eine Aufnahme.")
        if self.source["mode"] not in ("live", "simulation") or self.replay:
            raise ValueError("Aufnahmen benötigen Live- oder Simulationsbetrieb.")
        if self.source["status"] not in ("running", "calibrating"):
            raise ValueError("Empfang ist noch nicht bereit. Erst den angezeigten Start-/Hardwarefehler beheben.")
        self.recording = await asyncio.to_thread(self.store.start, self.effective_config(), name or f"Messung {utcnow()[:19]}", kind, self.source["mode"] == "simulation")
        self._sessions_checked = 0
        self.event(f"Aufnahme gestartet: {self.recording['name']}")
        return self.recording

    async def stop_recording(self):
        if not self.recording:
            raise ValueError("Keine laufende Aufnahme.")
        result = await asyncio.to_thread(self.store.stop, self.recording["id"])
        self.recording = None
        self._sessions_checked = 0
        self.event("Aufnahme abgeschlossen.")
        return result

    async def align_heading(self, callsign):
        if self.recording or self.replay:
            raise ValueError("Heading-Abgleich erst nach Stoppen der Aufnahme oder des Replays.")
        candidates = [o for o in self.observations if o["callsign"] == callsign and o.get("accepted") and o.get("reference_deg") is not None][-5:]
        if not candidates:
            raise ValueError("Keine gültige GPS-Referenz und Peilung für dieses Rufzeichen vorhanden.")
        if time.time()-self.emitters[callsign]["_seen_at"] > 45:
            raise ValueError("Referenzemitter ist veraltet; neue Meldung abwarten.")
        offsets = np.deg2rad([wrap180(o["reference_deg"]-o["local_deg"]) for o in candidates])
        heading = float(np.rad2deg(np.angle(np.mean(np.exp(1j*offsets)))) % 360)
        await self.update_config({"heading_deg": heading})
        self.event(f"Heading auf {callsign} ausgerichtet: {heading:.1f}° ({len(candidates)} Pakete).")
        return {"heading_deg": heading, "packet_count": len(candidates)}

    async def fit(self, training_id, validation_id):
        if training_id == validation_id:
            raise ValueError("Training und Prüfung müssen getrennte Aufnahmen sein.")
        train = self.store.get(training_id)
        validation = self.store.get(validation_id)
        if train.get("stopped_at") is None or validation.get("stopped_at") is None:
            raise ValueError("Beide Aufnahmen vor der Kalibrierung abschließen.")
        if train["config"] != validation["config"] or train["simulated"] != validation["simulated"]:
            raise ValueError("Trainings- und Prüflauf benötigen dasselbe Messprofil und dieselbe Datenart.")
        training_obs = self.store.observations(training_id)
        validation_obs = self.store.observations(validation_id)
        model = await asyncio.to_thread(fit_calibration, training_obs, validation_obs)
        model.update(id=uuid.uuid4().hex, created_at=utcnow(), training_session_id=training_id,
                     validation_session_id=validation_id, config=train["config"], simulated=train["simulated"],
                     profile_id=AppConfig.model_validate(train["config"]).profile_id)
        self.models.append(clean(model))
        save_json(self.data_dir / "calibrations.json", self.models)
        self.event(f"Kalibrierung: {model.get('reason', 'Prüfung abgeschlossen')}")
        return model

    def apply_model(self, model_id):
        if self.recording or self.replay:
            raise ValueError("Korrekturwechsel erst nach Stoppen der Aufnahme oder des Replays.")
        if model_id:
            model = next((m for m in self.models if m["id"] == model_id), None)
            if model is None:
                raise ValueError("Unbekannte Kalibrierung.")
            if not model.get("validated") or not model.get("improved"):
                raise ValueError("Diese Korrektur hat keine unabhängig bestätigte Verbesserung.")
            if model["config"] != self.effective_config() or model["simulated"] != (self.source["mode"] == "simulation"):
                raise ValueError("Kalibrierung passt nicht zum aktiven Messprofil oder zur Datenart.")
        self.active_calibration_id = model_id
        self.reset_measurements()
        self.event("Winkelkorrektur aktiviert." if model_id else "Winkelkorrektur deaktiviert.")

    async def start_replay(self, session_id, speed=1):
        if self.recording:
            raise ValueError("Vor Replay die laufende Aufnahme stoppen.")
        if not .1 <= speed <= 20:
            raise ValueError("Replaygeschwindigkeit muss zwischen 0,1 und 20 liegen.")
        session = self.store.get(session_id)
        if not session.get("stopped_at"):
            raise ValueError("Nur abgeschlossene Aufnahmen können wiedergegeben werden.")
        events = list(self.store.iter_events(session_id))
        if not events:
            raise ValueError("Diese Aufnahme enthält keine Pakete.")
        await self._stop_source()
        if self._restore_config is None:
            self._restore_config = self.config
        self.config = AppConfig.model_validate(session["config"])
        self.active_calibration_id = None
        self.reset_measurements()
        self.source.update(mode="replay", status="running", simulated=bool(session["simulated"]), message=f"Replay: {session['name']}")
        self.replay = {"session_id": session_id, "speed": speed, "progress": 0, "recomputed_iq": True}
        self._task = asyncio.create_task(self._replay_loop(events, speed))

    async def _replay_loop(self, events, speed):
        try:
            previous = None
            for i, item in enumerate(events):
                observation = dict(item["observation"])
                stamp = datetime.fromisoformat(observation["timestamp"]).timestamp()
                if previous is not None:
                    # Preserve original timing; no fake speed derived from RF sample count.
                    await asyncio.sleep(max(0, stamp-previous)/speed)
                previous = stamp
                iq = await asyncio.to_thread(self.store.load_iq, item) if item.get("iq_path") else None
                if iq is not None:
                    left = max(0, int(observation.get("iq_signal_start", 0)))
                    right = min(iq.shape[1], int(observation.get("iq_signal_end", iq.shape[1])))
                    estimate = await asyncio.to_thread(estimate_doa, iq[:, left:right], positions_for(self.config), observation["frequency_hz"])
                    observation["local_deg"] = estimate["azimuth_deg"]
                    observation["raw_deg"] = (estimate["azimuth_deg"] + self.config.heading_deg) % 360
                    # Preserve the exact recorded correction version, if present.
                    model = observation.get("calibration_model") or next((m for m in self.models if m["id"] == observation.get("calibration_id")), None)
                    if observation.get("calibration_id") and model is None:
                        raise ValueError("Die aufgezeichnete Korrekturversion fehlt; korrigierte Werte können nicht neu berechnet werden.")
                    observation["corrected_deg"] = float(apply_calibration(observation["raw_deg"], model)) if model else observation["raw_deg"]
                    if observation.get("reference_deg") is not None:
                        observation["error_raw_deg"] = float(wrap180(observation["raw_deg"]-observation["reference_deg"]))
                        observation["error_corrected_deg"] = float(wrap180(observation["corrected_deg"]-observation["reference_deg"]))
                    observation["recomputed_from_iq"] = True
                await self.accept(observation, spec=item.get("spectrum"), record=False)
                self.replay["progress"] = (i+1)/len(events)
            self.source.update(status="finished", message="Replay abgeschlossen")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.source.update(status="error", message=f"Replay: {exc}")
            self.event(f"Replay: {exc}", "error")

    async def stop_replay(self):
        await self.set_source("idle")

    def snapshot(self):
        now = time.time()
        if now-self._sessions_checked > 1:
            self._sessions_cache = self.store.list_sessions()
            self._sessions_checked = now
        counts = [0]*24
        coverage_rows = self.store.observations(self.recording["id"]) if self.recording and self.recording["kind"] == "calibration" else self.observations
        for o in coverage_rows:
            if o.get("accepted") and o.get("reference_deg") is not None:
                counts[int(o["reference_deg"] % 360 // 15)] += 1
        emitters = []
        for row in self.emitters.values():
            item = {k: v for k, v in row.items() if k != "_seen_at"}
            item["age_s"] = max(0, now-row["_seen_at"])
            emitters.append(item)
        lat, lon, valid, station_mode = self.station()
        gps = {**self.gps, "model": "ARCELI VK-162", "station_valid": valid, "station_source": station_mode}
        if gps.get("received_at"):
            gps["age_s"] = max(0, now-gps["received_at"])
            if gps["age_s"] > 5 and gps["status"] == "fix":
                gps.update(status="stale", valid=False)
        if self.source["mode"] == "simulation" and not self.config.gps_port:
            gps.update(status="simulation", latitude=lat, longitude=lon, simulated=True)
        return clean({"timestamp": utcnow(), "source": self.source, "gps": gps,
                      "station": {"latitude": lat, "longitude": lon, "valid": valid, "source": station_mode, "heading_deg": self.config.heading_deg},
                      "config": self.effective_config(), "profile_id": self.config.profile_id,
                      "arrays": load_presets(), "emitters": emitters, "spectrum": self.spectrum, "stats": self.stats,
                      "recording": self.recording, "sessions": self._sessions_cache,
                      "calibration": {"active_id": self.active_calibration_id, "coverage": counts, "models": self.models},
                      "replay": self.replay, "events": list(self.events), "symbols": self.symbols,
                      "decoder_capabilities": decoder_capabilities(), "manual_gains_db": MANUAL_GAINS_DB,
                      "daq": self.daq.status()})
