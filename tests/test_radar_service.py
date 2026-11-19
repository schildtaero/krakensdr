"""Receiver ownership, bounded acquisition and real IQ replay orchestration."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from krakendf.diagnostics import DiagnosticLog
from krakendf.hardware import IQFrame
from krakendf.radar_service import PassiveRadarService


class FakeDAQ:
    def __init__(self, fail=False):
        self.starts = []
        self.stops = 0
        self.fail = fail

    async def start(self, config):
        self.starts.append(config)
        if self.fail:
            raise RuntimeError("no attached Kraken")

    async def stop(self):
        self.stops += 1


class FakeClient:
    instances = []
    fail_connect = False
    initial_frames = []

    def __init__(self, *args, **kwargs):
        self.frames = asyncio.Queue()
        self.closed = False
        self.configured = None
        for frame in self.initial_frames:
            self.frames.put_nowait(frame)
        self.instances.append(self)

    async def connect(self):
        if self.fail_connect:
            raise RuntimeError("hardware connection failed")

    async def configure(self, **settings):
        self.configured = settings

    async def read_frame(self):
        return await self.frames.get()

    async def close(self):
        self.closed = True


class FakeProcessor:
    frames = []

    def __init__(self, config):
        self.config = config

    def process(self, frame):
        self.frames.append(frame)
        if not frame.quality["valid"]:
            return []
        pair = frame.iq[[self.config.reference_channel,
                         self.config.surveillance_channel]].copy()
        return [SimpleNamespace(data={"matrix_db": [[0, 9]], "candidates": [],
                                      "source": "live computation"}, iq=pair,
                                sample_rate_hz=frame.sample_rate_hz,
                                sample_start=frame.sample_start,
                                sample_end=frame.sample_start + pair.shape[1],
                                stream_epoch=frame.stream_epoch,
                                timestamp=frame.timestamp)]


class RadarServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = DiagnosticLog(self.root)
        self.daq = FakeDAQ()
        self.events = []
        self.radar = PassiveRadarService(self.root, self.daq, self.log,
                                         lambda message, level="info": self.events.append((message, level)))
        await self.radar.update_config({"illuminator": "fm", "center_frequency_hz": 100000000,
                                        "sample_rate_hz": 240000, "bandwidth_hz": 100000})
        FakeClient.instances = []
        FakeClient.initial_frames = []
        FakeClient.fail_connect = False
        FakeProcessor.frames = []

    async def asyncTearDown(self):
        await self.radar.close()
        self.log.close()
        self.temporary.cleanup()
        remaining = [task for task in asyncio.all_tasks()
                     if task is not asyncio.current_task() and not task.done()]
        self.assertEqual(remaining, [], "Radar shutdown must not leave background tasks")

    async def wait_for(self, predicate, timeout=4):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.005)

    def frame(self, start=0, **quality):
        values = np.arange(128, dtype=np.float32)
        iq = np.stack([values + (channel + 1) * 1j for channel in range(5)]).astype(np.complex64)
        return IQFrame(iq=iq, sample_rate_hz=self.radar.config.sample_rate_hz,
                       center_frequency_hz=self.radar.config.center_frequency_hz,
                       sample_start=start, stream_epoch="test-epoch", timestamp=1700000000 + start / 240000,
                       quality={"valid": True, "reasons": [], "frame_type": 0,
                                "delay_lock": True, "iq_lock": True, "noise_source": False,
                                "overdrive_flags": 0, "gains_db": [self.radar.config.gain_db] * 5,
                                **quality})

    async def start_live(self, managed=False):
        await self.radar.start("live", managed=managed)
        await self.wait_for(lambda: bool(FakeClient.instances))
        return FakeClient.instances[-1]

    async def test_idle_close_does_not_stop_shared_daq(self):
        await self.radar.close()
        self.assertEqual(self.daq.stops, 0)
        self.assertEqual(self.radar.mode, "idle")

    async def test_managed_failure_is_reported_without_simulation_fallback(self):
        self.daq.fail = True
        with patch("krakendf.radar_service.HeimdallClient", FakeClient):
            await self.radar.start("live", managed=True)
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "error")
        state = self.radar.snapshot()
        self.assertEqual(state["mode"], "live")
        self.assertFalse(state["simulated"])
        self.assertIsNone(state["result"])
        self.assertIn("no attached Kraken", state["message"])
        self.assertEqual(len(self.daq.starts), 1)
        self.assertEqual(self.daq.stops, 1)
        await self.radar.stop()
        self.assertEqual(self.daq.stops, 1)

    async def test_external_client_failure_never_stops_shared_daq(self):
        FakeClient.fail_connect = True
        with patch("krakendf.radar_service.HeimdallClient", FakeClient):
            await self.radar.start("live", managed=False)
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "error")
        self.assertEqual(self.daq.starts, [])
        self.assertEqual(self.daq.stops, 0)
        self.assertTrue(FakeClient.instances[0].closed)

    async def test_header_locks_gains_and_rate_are_checked_and_flush_cpi(self):
        with patch("krakendf.radar_service.HeimdallClient", FakeClient), \
                patch("krakendf.radar_service.PassiveRadarProcessor", FakeProcessor):
            client = await self.start_live()
            client.frames.put_nowait(self.frame())
            await self.wait_for(lambda: self.radar.snapshot()["processed_cpis"] == 1)
            client.frames.put_nowait(self.frame(128, delay_lock=False))
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "calibrating")
            self.assertIsNone(self.radar.snapshot()["result"])
            client.frames.put_nowait(self.frame(256, gains_db=[0] * 5))
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "mismatch")
            self.assertTrue(all(not frame.quality["valid"] for frame in FakeProcessor.frames[1:]))
            self.assertIsNone(self.radar.snapshot()["result"])
            frame = self.frame(384)
            frame.sample_rate_hz += 1000
            client.frames.put_nowait(frame)
            await self.wait_for(lambda: self.radar.snapshot()["received_frames"] == 4)
            client.frames.put_nowait(self.frame(512))
            await self.wait_for(lambda: self.radar.snapshot()["processed_cpis"] == 2)
            state = self.radar.snapshot()
            self.assertEqual(state["status"], "running")
            self.assertFalse(state["result"]["simulated"])
            json.dumps(state, allow_nan=False)
            await self.radar.stop()
            self.assertIsNone(self.radar.snapshot()["result"])
            self.assertTrue(client.closed)
            self.assertEqual(self.daq.stops, 0)

    async def test_overload_stops_explicitly_with_bounded_queue(self):
        FakeClient.initial_frames = [self.frame(index * 128) for index in range(4)]
        with patch("krakendf.radar_service.HeimdallClient", FakeClient), \
                patch("krakendf.radar_service.PassiveRadarProcessor", FakeProcessor):
            await self.start_live()
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "error")
        state = self.radar.snapshot()
        self.assertEqual(state["received_frames"], 4)
        self.assertEqual(state["dropped_frames"], 1)
        self.assertEqual(state["queue_capacity"], 3)
        self.assertLessEqual(state["queue_depth"], 3)
        self.assertIn("zu langsam", state["message"])

    async def test_record_replay_uses_saved_pair_and_recomputes(self):
        with patch("krakendf.radar_service.HeimdallClient", FakeClient), \
                patch("krakendf.radar_service.PassiveRadarProcessor", FakeProcessor):
            client = await self.start_live()
            client.frames.put_nowait(self.frame())
            await self.wait_for(lambda: self.radar.snapshot()["processed_cpis"] == 1)
            recording = await self.radar.start_recording("coherent pair")
            saved = self.frame(128)
            client.frames.put_nowait(saved)
            await self.wait_for(lambda: self.radar.recording["frame_count"] == 1)
            with self.assertRaises(ValueError):
                await self.radar.update_config({"threshold_db": 15})
            with self.assertRaises(ValueError):
                await self.radar.start_replay(recording["id"])
            await self.radar.stop_recording()
            await self.radar.stop()
        original = self.radar.config
        await self.radar.update_config({"threshold_db": original.threshold_db + 1})
        updated = self.radar.config
        seen = []

        def recompute(pair, rate, config, metadata):
            seen.append((pair.copy(), rate, config, metadata))
            return {"matrix_db": [[-4, 21]], "candidates": [], "source": "recomputed from IQ"}

        with patch("krakendf.radar_service.analyze_window", recompute):
            await self.radar.start_replay(recording["id"], speed=20)
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "finished")
        self.assertEqual(len(seen), 1)
        np.testing.assert_array_equal(seen[0][0], saved.iq[[0, 1]])
        self.assertEqual(seen[0][1], 240000)
        self.assertEqual(seen[0][2].threshold_db, original.threshold_db)
        self.assertEqual(seen[0][3]["iq_stage"], "channelized_pair_with_prefix")
        state = self.radar.snapshot()
        self.assertEqual(state["result"]["source"], "recomputed from IQ")
        self.assertEqual(state["replay"]["progress"], 1)
        await self.radar.stop_replay()
        self.assertEqual(self.radar.config, updated)
        self.assertEqual(self.daq.stops, 0)

    async def test_config_is_independent_persistent_and_locked_while_running(self):
        await self.radar.update_config({"threshold_db": 16})
        loaded = PassiveRadarService(self.root, self.daq, self.log)
        self.assertEqual(loaded.config.threshold_db, 16)
        self.assertFalse((self.root / "config.json").exists())
        with patch("krakendf.radar_service.HeimdallClient", FakeClient):
            await self.start_live()
            with self.assertRaises(ValueError):
                await self.radar.update_config({"threshold_db": 17})
        await loaded.close()

    async def test_recording_finalize_failure_still_releases_owned_receiver(self):
        with patch("krakendf.radar_service.HeimdallClient", FakeClient), \
                patch("krakendf.radar_service.PassiveRadarProcessor", FakeProcessor):
            client = await self.start_live(managed=True)
            client.frames.put_nowait(self.frame())
            await self.wait_for(lambda: self.radar.snapshot()["status"] == "running")
            await self.radar.start_recording("failure cleanup")
            with patch.object(self.radar.store, "stop", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    await self.radar.stop()
            self.assertTrue(client.closed)
            self.assertEqual(self.daq.stops, 1)
            self.assertEqual(self.radar.mode, "idle")
            self.assertIsNone(self.radar.recording)

    async def test_explicit_simulation_passes_actual_dsp_and_has_no_hardware_claim(self):
        await self.radar.update_config({"cpi_seconds": .05, "range_bins": 16,
                                        "doppler_bins": 32, "max_doppler_hz": 200})
        await self.radar.start("simulation")
        await self.wait_for(lambda: self.radar.snapshot()["processed_cpis"] > 0, timeout=10)
        state = self.radar.snapshot()
        self.assertEqual(state["mode"], "simulation")
        self.assertEqual(state["status"], "running")
        self.assertTrue(state["simulated"])
        self.assertTrue(state["result"]["simulated"])
        self.assertIn("synthetic", state["result"]["fixture"])
        self.assertFalse(state["hardware_validated"])
        self.assertGreater(state["received_frames"], 0)
        self.assertGreater(state["rf_duration_ms"], 0)
        self.assertEqual(self.daq.starts, [])
        self.assertEqual(self.daq.stops, 0)
        json.dumps(state, allow_nan=False)

    async def test_real_filtered_iq_recording_replay_preserves_caf_and_prefix(self):
        await self.radar.update_config({"sample_rate_hz": 2400000, "bandwidth_hz": 200000,
                                        "cpi_seconds": .05, "range_bins": 16,
                                        "doppler_bins": 32, "max_doppler_hz": 200})
        await self.radar.start("simulation")
        await self.wait_for(lambda: self.radar.snapshot()["processed_cpis"] > 0)
        recording = await self.radar.start_recording("actual DSP fixture")
        await self.wait_for(lambda: self.radar.recording["frame_count"] > 0)
        await self.radar.stop_recording()
        events = list(self.radar.store.iter_events(recording["id"]))
        event = events[0]
        metadata = event["metadata"]
        pair = self.radar.store.load_pair(event)
        self.assertEqual(metadata["decimation_factor"], 6)
        self.assertEqual(metadata["sample_rate_hz"], 400000)
        self.assertEqual(metadata["sample_end"] - metadata["sample_start"], pair.shape[1] * 6)
        self.assertEqual(metadata["cpi_sample_start"] - metadata["sample_start"], 15 * 6)
        self.assertEqual(metadata["reference_channel"], 0)
        saved = self.radar.store.load_result(events[-1])
        await self.radar.stop()
        await self.radar.start_replay(recording["id"], speed=20)
        await self.wait_for(lambda: self.radar.snapshot()["status"] == "finished")
        recomputed = self.radar.snapshot()["result"]
        self.assertEqual(recomputed["power_db"], saved["power_db"])
        self.assertEqual(recomputed["candidates"], saved["candidates"])
        self.assertEqual(recomputed["timestamp_unix"], saved["timestamp_unix"])
        self.assertTrue(recomputed["simulated"])

    async def test_replay_absolute_clock_subtracts_processing_and_preserves_gaps(self):
        now = [100.0]
        sleeps = []
        timestamps = ["2023-11-14T22:13:20+00:00", 1700000000.25, 1700000001.0]
        events = [{"metadata": {"timestamp": value, "sample_rate_hz": 240000}}
                  for value in timestamps]
        self.radar._clock = lambda: now[0]

        async def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        async def compute(function, *args):
            return function(*args)

        def analyze(pair, rate, config, metadata):
            self.assertIsInstance(metadata["timestamp_unix"], float)
            now[0] += .075  # native CAF work consumes real replay budget
            return {"power_db": [[0]], "timestamp_unix": metadata["timestamp_unix"]}

        self.radar._sleep = sleep
        self.radar._compute = compute
        self.radar.replay = {"processed": 0, "total": 3, "progress": 0}
        with patch.object(self.radar.store, "iter_events", return_value=iter(events)), \
                patch.object(self.radar.store, "load_pair", return_value=np.zeros((2, 32), np.complex64)), \
                patch("krakendf.radar_service.analyze_window", analyze):
            await self.radar._replay_loop("unused-test-id", speed=1)
        self.assertEqual(self.radar.snapshot()["status"], "finished")
        np.testing.assert_allclose(sleeps, [0, .175, .675], rtol=0, atol=1e-10)
        self.assertAlmostEqual(now[0], 101.075)


if __name__ == "__main__":
    unittest.main()
