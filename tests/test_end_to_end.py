"""End-to-end protocol-IQ regression through the actual live processing path.

These tests synthesize *on-air protocol waveforms*, not service identity
metadata. Both RF channels occupy one 2.4-MS/s five-antenna stream; identity,
position and CRC/BCH quality must come from the real decoder implementations.
This remains a software integration test, not Kraken hardware validation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.signal import resample_poly

from krakendf.config import AppConfig, positions_for
from krakendf.decoders import synthesize_pocsag
from krakendf.dsp import destination, steering_vector, wrap180
from krakendf.hardware import IQFrame
from krakendf.lora_phy import synthesize_lora
from krakendf.pipeline import LivePipeline
from krakendf.service import DemoService


RATE = 2_400_000
CPI = 1_048_576  # Actual default Heimdall block length, deliberately non-divisible by 48.
CENTER = 434_000_000
EPOCH = "synthetic-protocol-integration-v1"
TIMESTAMP = 1_710_000_000.0


def make_payload(callsign: str, config: AppConfig, reference: float) -> str:
    latitude, longitude = destination(
        config.station_latitude, config.station_longitude, reference, 450
    )
    return f"{callsign} {latitude:.6f} {longitude:.6f} 112m 120000Z 09sat"


def rf_protocol_fixture(config: AppConfig, *, corrupted_lora=False, include_pocsag=True):
    """Small baseband arrays, upsampled before mixing to the common RF band."""
    channels = {channel.id: channel for channel in config.channels}
    positions = positions_for(config)
    specifications = []
    if include_pocsag:
        channel = channels["pocsag"]
        reference = 60.0
        local = (reference - config.heading_deg) % 360
        payload = make_payload("E2EPC", config, reference)
        iq = synthesize_pocsag(payload, 48000, padding_seconds=0, noise_std=0)
        specifications.append({
            "channel": "pocsag", "payload": payload, "callsign": "E2EPC",
            "reference": reference, "local": local,
            "start": int(.55 * RATE), "frequency": channel.frequency_hz,
            "iq": resample_poly(iq, 50, 1).astype(np.complex64),
            "vector": steering_vector(positions, channel.frequency_hz, local).astype(np.complex64),
        })
    channel = channels["lora"]
    reference = 251.0
    local = (reference - config.heading_deg) % 360
    payload = make_payload("E2ELORA", config, reference)
    iq = synthesize_lora(payload.encode("ascii"), sample_rate_hz=300000,
                         corrupt_crc=corrupted_lora)
    specifications.append({
        "channel": "lora", "payload": payload, "callsign": "E2ELORA",
        "reference": reference, "local": local,
        "start": int(.72 * RATE), "frequency": channel.frequency_hz,
        "iq": resample_poly(iq, 8, 1).astype(np.complex64),
        "vector": steering_vector(positions, channel.frequency_hz, local).astype(np.complex64),
    })
    return specifications


def make_frame(start: int, count: int, specifications, *, epoch=EPOCH) -> IQFrame:
    """Mix concurrent RF channels while preserving their independent AoA."""
    rng = np.random.default_rng(401 + start)
    iq = .025 * (rng.standard_normal((5, count), dtype=np.float32)
                 + 1j * rng.standard_normal((5, count), dtype=np.float32))
    for item in specifications:
        source_start = max(start, item["start"])
        source_end = min(start + count, item["start"] + len(item["iq"]))
        if source_end <= source_start:
            continue
        source = item["iq"][source_start-item["start"]:source_end-item["start"]]
        n = np.arange(source_start, source_end, dtype=np.float64)
        carrier = np.exp(2j * np.pi * (item["frequency"]-CENTER) * n / RATE)
        waveform = (source * carrier).astype(np.complex64)
        iq[:, source_start-start:source_end-start] += item["vector"][:, None] * waveform
    return IQFrame(
        iq=iq.astype(np.complex64, copy=False), sample_rate_hz=RATE,
        center_frequency_hz=CENTER, sample_start=start,
        stream_epoch=epoch, timestamp=TIMESTAMP + start/RATE,
        quality={"valid": True, "fixture": "synthetic_on_air_protocol"},
    )


async def feed_protocol_frames(service: DemoService, specifications, *, stop_seconds=3.0):
    pipeline = LivePipeline(service.config)
    captures = []
    stop = int(stop_seconds * RATE)
    for start in range(0, stop, CPI):
        frame = await asyncio.to_thread(make_frame, start, min(CPI, stop-start), specifications)
        spec, bursts = await asyncio.to_thread(pipeline.process, frame)
        service.spectrum = spec
        for burst in bursts:
            captures.append(burst)
            await service._decode_burst(burst)
    return captures


class LiveProtocolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="krakendf-e2e-")
        self.service = DemoService(Path(self.directory.name))
        self.service.config = AppConfig(station_mode="manual", heading_deg=23.0)
        # Select the live *processing path* without opening any socket. The IQ
        # source in this test is explicitly the protocol fixture above.
        self.service.source.update(mode="live", status="running", simulated=False)

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_two_rf_channels_cross_cpi_decode_df_record_and_replay(self):
        specifications = await asyncio.to_thread(rf_protocol_fixture, self.service.config)
        self.assertLess(specifications[1]["start"], specifications[0]["start"] + len(specifications[0]["iq"]))
        self.assertTrue(any(item["start"] // CPI != (item["start"] + len(item["iq"])) // CPI
                            for item in specifications))
        session = await self.service.start_recording("Protocol-IQ integration")
        captures = await feed_protocol_frames(self.service, specifications)
        observations = list(self.service.observations)
        self.assertEqual({row["callsign"] for row in observations}, {"E2EPC", "E2ELORA"},
                         msg=f"Decoded observations/events: {observations!r}; {list(self.service.events)!r}")
        self.assertEqual(len(observations), 2)
        by_call = {row["callsign"]: row for row in observations}
        for fixture in specifications:
            row = by_call[fixture["callsign"]]
            self.assertEqual(row["payload"], fixture["payload"])
            self.assertTrue(row["quality"]["decoder_verified"])
            self.assertTrue(row["accepted"])
            self.assertEqual(row["stream_epoch"], EPOCH)
            self.assertEqual(row["modulation"], fixture["channel"])
            self.assertEqual(row["frequency_hz"], fixture["frequency"])
            self.assertLess(abs(wrap180(row["local_deg"]-fixture["local"])), 1.1)
            self.assertLess(abs(wrap180(row["raw_deg"]-fixture["reference"])), 1.1)
            self.assertLess(abs(row["error_raw_deg"]), 1.1)
            self.assertLess(abs(row["sample_start"]-fixture["start"]), .005*RATE)
            self.assertGreater(row["sample_end"], row["sample_start"])
            self.assertEqual(row["sample_rate_hz"], RATE)
            capture = next(b for b in captures if b.channel["id"] == fixture["channel"])
            factor = capture.quality["decimation_factor"]
            self.assertEqual(row["sample_start"], capture.sample_start + row["iq_signal_start"]*factor)
            self.assertEqual(row["sample_end"], capture.sample_start + row["iq_signal_end"]*factor)
            if fixture["channel"] == "lora":
                self.assertTrue(row["quality"]["crc_ok"])
            else:
                self.assertTrue(row["quality"]["bch_ok"])
                self.assertTrue(row["quality"]["parity_ok"])

        completed = await self.service.stop_recording()
        self.assertEqual(completed["packet_count"], 2)
        self.assertGreater(completed["iq_bytes"], 0)
        events = list(self.service.store.iter_events(session["id"]))
        self.assertEqual(len(events), 2)
        for event in events:
            iq = self.service.store.load_iq(event)
            self.assertEqual(iq.shape[0], 5)
            self.assertEqual(iq.dtype, np.complex64)
            self.assertEqual(event["observation"]["stream_epoch"], EPOCH)

        # Replay recomputes DoA from the recorded coherent five-channel arrays.
        await self.service.start_replay(session["id"], speed=20)
        await asyncio.wait_for(self.service._task, timeout=10)
        self.assertEqual(self.service.source["status"], "finished")
        replayed = {row["callsign"]: row for row in self.service.observations}
        self.assertEqual(set(replayed), set(by_call))
        for callsign, before in by_call.items():
            after = replayed[callsign]
            self.assertTrue(after["recomputed_from_iq"])
            self.assertEqual(after["sample_start"], before["sample_start"])
            self.assertEqual(after["burst_id"], before["burst_id"])
            self.assertAlmostEqual(after["raw_deg"], before["raw_deg"], places=7)

    async def test_bad_lora_crc_cannot_create_emitter_or_position_measurement(self):
        self.service.config = self.service.config.model_copy(update={
            "channels": [channel.model_copy(update={"enabled": channel.id == "lora"})
                         for channel in self.service.config.channels]
        })
        specifications = await asyncio.to_thread(
            rf_protocol_fixture, self.service.config, corrupted_lora=True, include_pocsag=False
        )
        captures = await feed_protocol_frames(self.service, specifications, stop_seconds=2.2)
        self.assertGreaterEqual(len(captures), 1, "CRC rejection must be tested after an actual burst was captured")
        self.assertEqual(list(self.service.observations), [])
        self.assertEqual(self.service.emitters, {})
        self.assertTrue(any("keine geprüfte Nutzlast" in item["message"] for item in self.service.events))


if __name__ == "__main__":
    unittest.main()
