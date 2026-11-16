"""Bounded live-worker orchestration, using gates rather than speed thresholds.

These checks exercise scheduling and cancellation without RF devices. They do
not assert that an arbitrary CI machine can sustain a particular sample rate.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

from krakendf.config import AppConfig
from krakendf.decoders import DecodedPacket
from krakendf.pipeline import Burst
from krakendf.service import DemoService


class QueuedIQClient:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.read_started = asyncio.Event()
        self.connected = False

    async def connect(self):
        self.connected = True

    async def configure(self, **configuration):
        return {"acknowledged": True}

    async def read_frame(self):
        self.read_started.set()
        return await self.frames.get()

    async def close(self):
        self.connected = False


class LiveWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = QueuedIQClient()
        self.pipeline = SimpleNamespace(process=Mock(return_value=({}, [])),
                                        prepare=Mock(return_value={"backend": "test", "fallback_reason": None}))
        manager = SimpleNamespace(
            start=AsyncMock(), stop=AsyncMock(),
            status=Mock(return_value={"managed": True, "installed": True,
                                      "running": False, "state": "stopped"}))
        self.patches = [
            patch("krakendf.service.ManagedDAQ", return_value=manager),
            patch("krakendf.service.HeimdallClient", return_value=self.client),
            patch("krakendf.service.LivePipeline", return_value=self.pipeline),
        ]
        for item in self.patches:
            item.start()
        self.service = DemoService(Path(self.temp.name))
        self.service.config = AppConfig(station_mode="manual")
        self.thread_releases = []

    async def asyncTearDown(self):
        for release in self.thread_releases:
            release.set()
        await self.service.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def ready(self):
        # External mode skips native/USB startup but uses the real live loop.
        await self.service.set_source("live", managed=False)
        await asyncio.wait_for(self.client.read_started.wait(), 3)

    async def until(self, predicate):
        async def observe():
            while not predicate():
                await asyncio.sleep(.001)
        await asyncio.wait_for(observe(), 3)

    def frame(self, index):
        return SimpleNamespace(
            quality={"valid": True},
            iq=np.ones((5, 128), dtype=np.complex64),
            center_frequency_hz=self.service.config.center_frequency_hz,
            sample_rate_hz=self.service.config.sample_rate_hz,
            sample_start=index * 128,
            stream_epoch="controlled-stream", timestamp=0.0)

    def burst(self):
        channel = self.service.config.channels[0].model_dump()
        return Burst(np.ones((5, 96), dtype=np.complex64), 50000,
                     channel["frequency_hz"], 0, 96, "controlled-stream",
                     channel, 0.0, {"valid": True, "decimation_factor": 1})

    async def test_blocked_decoder_does_not_stop_rf_pipeline_and_reports_work(self):
        decoder_entered, release_decoder = asyncio.Event(), asyncio.Event()
        second_frame_processed = asyncio.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def process(frame):
            calls.append(frame.sample_start)
            if len(calls) == 1:
                return {"power_db": [-12.0]}, [self.burst()]
            loop.call_soon_threadsafe(second_frame_processed.set)
            return {"power_db": [-13.0]}, []

        async def decode(burst, context=None):
            decoder_entered.set()
            await release_decoder.wait()

        self.pipeline.process.side_effect = process
        with patch.object(self.service, "_decode_burst", side_effect=decode):
            await self.ready()
            await self.client.frames.put(self.frame(0))
            await asyncio.wait_for(decoder_entered.wait(), 3)
            await self.client.frames.put(self.frame(1))
            await asyncio.wait_for(second_frame_processed.wait(), 3)
            await self.until(lambda: self.service.spectrum.get("power_db") == [-13.0])
            self.assertFalse(release_decoder.is_set())
            self.assertEqual(calls, [0, 128])
            self.assertEqual(self.service.source["status"], "running")
            performance = self.service.snapshot()["source"]["performance"]
            self.assertEqual(self.service.source["received_frames"], 2)
            self.assertAlmostEqual(performance["frame_duration_ms"], 128 / 2400000 * 1000)
            self.assertGreater(performance["last_pipeline_ms"], 0)
            self.assertGreater(performance["processing_load"], 0)
            self.assertEqual(performance["overruns"], 0)
            self.assertEqual(performance["decoded_bursts"], 0)
            self.assertLessEqual(performance["queue_depth"], performance["queue_capacity"])
            release_decoder.set()
            await self.until(lambda: self.service.source["performance"]["decoded_bursts"] == 1)
            self.assertGreater(self.service.source["performance"]["last_decode_ms"], 0)

    async def test_source_change_cancels_decoder_and_rejects_late_thread_result(self):
        loop = asyncio.get_running_loop()
        decoder_entered, decoder_returned = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        self.thread_releases.append(release)
        packet = DecodedPacket("DL7CR 52.001000 13.001000 112m 184205Z 09sat",
                               "pocsag", 0, 96, {"valid": True})

        def slow_decode(*args):
            loop.call_soon_threadsafe(decoder_entered.set)
            release.wait()
            loop.call_soon_threadsafe(decoder_returned.set)
            return [packet]

        self.pipeline.process.return_value = ({}, [self.burst()])
        with patch("krakendf.service.decode_pocsag", side_effect=slow_decode), \
                patch.object(self.service, "accept", new_callable=AsyncMock) as accept:
            await self.ready()
            await self.client.frames.put(self.frame(0))
            await asyncio.wait_for(decoder_entered.wait(), 3)
            await self.service.set_source("idle")
            self.assertEqual(self.service.source["mode"], "idle")
            self.assertIsNone(self.service._task)
            self.assertFalse(self.client.connected)
            # Cancelling asyncio.to_thread cannot abort its synchronous work.
            # Its eventual packet must nevertheless never enter the new source.
            release.set()
            await asyncio.wait_for(decoder_returned.wait(), 3)
            await asyncio.sleep(0)
            accept.assert_not_awaited()
            self.assertEqual(list(self.service.observations), [])
            self.assertEqual(self.service.emitters, {})

    async def test_rf_queue_overflow_is_a_visible_live_error(self):
        loop = asyncio.get_running_loop()
        pipeline_entered = asyncio.Event()
        release = threading.Event()
        self.thread_releases.append(release)

        def blocked_pipeline(frame):
            loop.call_soon_threadsafe(pipeline_entered.set)
            release.wait()
            return {}, []

        self.pipeline.process.side_effect = blocked_pipeline
        await self.ready()
        await self.client.frames.put(self.frame(0))
        await asyncio.wait_for(pipeline_entered.wait(), 3)
        capacity = self.service.source["performance"]["queue_capacity"]
        for index in range(1, capacity + 2):
            self.client.frames.put_nowait(self.frame(index))
        await self.until(lambda: self.service.source["performance"]["overruns"] == 1)
        release.set()
        await asyncio.wait_for(self.service._task, 3)
        self.assertEqual(self.service.source["mode"], "live")
        self.assertEqual(self.service.source["status"], "error")
        self.assertIn("Verarbeitung zu langsam", self.service.source["message"])
        self.assertEqual(self.service.source["dropped_frames"], 1)
        self.assertTrue(any("Datenlücke" in item["message"] for item in self.service.events))
        self.assertEqual(list(self.service.observations), [])
        self.assertFalse(self.client.connected)

    async def test_burst_queue_overflow_is_a_visible_decoder_error(self):
        await self.ready()
        capacity = self.service.source["performance"]["burst_queue_capacity"]
        self.pipeline.process.return_value = ({}, [self.burst() for _ in range(capacity + 1)])
        await self.client.frames.put(self.frame(0))
        await asyncio.wait_for(self.service._task, 3)
        self.assertEqual(self.service.source["status"], "error")
        self.assertIn("Paketwarteschlange voll", self.service.source["message"])
        self.assertEqual(self.service.source["performance"]["overruns"], 1)
        self.assertEqual(self.service.source["performance"]["burst_queue_depth"], capacity)
        self.assertEqual(self.service.source["dropped_frames"], 0)
        self.assertTrue(any("Paketwarteschlange" in item["message"] and item["level"] == "error"
                            for item in self.service.events))
        self.assertEqual(list(self.service.observations), [])
        self.assertFalse(self.client.connected)

    async def test_delayed_packet_cannot_be_recorded_in_a_later_session(self):
        loop = asyncio.get_running_loop()
        decoder_entered = asyncio.Event()
        release = threading.Event()
        self.thread_releases.append(release)
        packet = DecodedPacket("DL7CR 52.001000 13.001000 112m 184205Z 09sat",
                               "pocsag", 0, 96, {"valid": True})
        calls = []

        def process(frame):
            calls.append(frame.sample_start)
            return {}, [self.burst()] if len(calls) == 2 else []

        def slow_decode(*args):
            loop.call_soon_threadsafe(decoder_entered.set)
            release.wait()
            return [packet]

        self.pipeline.process.side_effect = process
        with patch("krakendf.service.decode_pocsag", side_effect=slow_decode):
            await self.ready()
            await self.client.frames.put(self.frame(0))
            await self.until(lambda: len(calls) == 1)
            first = await self.service.start_recording("First RF window")
            await self.client.frames.put(self.frame(1))
            await asyncio.wait_for(decoder_entered.wait(), 3)
            await self.service.stop_recording()
            second = await self.service.start_recording("Later RF window")
            release.set()
            await self.until(lambda: self.service.source["performance"]["decoded_bursts"] == 1)
            await self.service.stop_recording()
        self.assertEqual(self.service.store.get(first["id"])["packet_count"], 0)
        self.assertEqual(self.service.store.get(second["id"])["packet_count"], 0,
                         "A queued old burst must not enter a later recording")
        self.assertEqual(list(self.service.store.iter_events(second["id"])), [])
        # Its successful decode still enters live tracking, attributed to its
        # original run rather than the current recording's run. Requiring an
        # observation also prevents decoder failures from making this vacuous.
        self.assertEqual(len(self.service.observations), 1)
        self.assertEqual(self.service.observations[0]["run_id"], first["id"])

    async def test_delayed_packet_keeps_its_spectrum_without_rewinding_live_display(self):
        loop = asyncio.get_running_loop()
        decoder_entered = asyncio.Event()
        release = threading.Event()
        self.thread_releases.append(release)
        packet = DecodedPacket("DL7CR 52.001000 13.001000 112m 184205Z 09sat",
                               "pocsag", 0, 96, {"valid": True})
        spectrum_a = {"start_hz": 433000000, "step_hz": 1000,
                      "power_db": [-20.0, -5.0, -20.0]}
        spectrum_b = {"start_hz": 433000000, "step_hz": 1000,
                      "power_db": [-19.0, -18.0, -3.0]}
        calls = []

        def process(frame):
            calls.append(frame.sample_start)
            if len(calls) == 2:
                return spectrum_a, [self.burst()]
            return spectrum_b, []

        def slow_decode(*args):
            loop.call_soon_threadsafe(decoder_entered.set)
            release.wait()
            return [packet]

        self.pipeline.process.side_effect = process
        with patch("krakendf.service.decode_pocsag", side_effect=slow_decode):
            await self.ready()
            await self.client.frames.put(self.frame(0))
            await self.until(lambda: len(calls) == 1)
            session = await self.service.start_recording("Packet spectrum association")
            await self.client.frames.put(self.frame(1))
            await asyncio.wait_for(decoder_entered.wait(), 3)
            await self.client.frames.put(self.frame(2))
            await self.until(lambda: len(calls) == 3 and self.service.spectrum == spectrum_b)
            release.set()
            await self.until(lambda: self.service.source["performance"]["decoded_bursts"] == 1)
            await self.service.stop_recording()
        events = list(self.service.store.iter_events(session["id"]))
        self.assertEqual(len(events), 1, "The delayed packet must decode successfully")
        self.assertEqual(events[0]["spectrum"], spectrum_a,
                         "Saved packet spectrum must come from its own RF frame")
        self.assertEqual(self.service.spectrum, spectrum_b,
                         "A delayed packet must not rewind the newer live spectrum")

    async def test_close_manifest_failure_still_releases_rf_gps_and_log(self):
        self.service.recording = {"id": "cannot-finalize-manifest"}
        try:
            with patch.object(self.service, "stop_recording", new_callable=AsyncMock,
                              side_effect=OSError("manifest write failed")) as stop_recording, \
                    patch.object(self.service, "_stop_source", new_callable=AsyncMock) as stop_source, \
                    patch.object(self.service, "_stop_gps", new_callable=AsyncMock) as stop_gps, \
                    patch.object(self.service.diagnostic_log, "close",
                                 wraps=self.service.diagnostic_log.close) as close_log:
                with self.assertRaisesRegex(OSError, "manifest write failed"):
                    await self.service.close()
                stop_recording.assert_awaited_once()
                stop_source.assert_awaited_once()
                stop_gps.assert_awaited_once()
                close_log.assert_called_once()
        finally:
            self.service.recording = None


if __name__ == "__main__":
    unittest.main()
