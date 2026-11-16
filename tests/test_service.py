"""Application orchestration tests using actual DSP and local recording files."""
import asyncio
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from krakendf.config import AppConfig, positions_for
from krakendf.decoders import DecodedPacket
from krakendf.dsp import destination, steering_vector, wrap180
from krakendf.pipeline import Burst
from krakendf.service import DemoService, utcnow


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = DemoService(Path(self.temp.name))

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def wait_for_observations(self, count=1):
        deadline = time.monotonic() + 4
        while len(self.service.observations) < count and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        self.assertGreaterEqual(len(self.service.observations), count, self.service.source)

    def simulate_without_background(self):
        self.service.source.update(mode="simulation", status="running", simulated=True)

    def payload(self, reference=120, callsign="DL7CR"):
        lat, lon, _, _ = self.service.station()
        tx_lat, tx_lon = destination(lat, lon, reference, 500)
        return f"{callsign} {tx_lat:.6f} {tx_lon:.6f} 112m 184205Z 09sat"

    async def test_simulation_runs_actual_five_channel_dsp(self):
        await self.service.start("simulation")
        await self.wait_for_observations(3)
        rows = list(self.service.observations)
        self.assertEqual({row["callsign"] for row in rows}, {"DL7CR", "DF2DEMO", "DO1BFT"})
        self.assertTrue(all(row["accepted"] for row in rows))
        self.assertTrue(all(row["quality"]["identity_source"] == "synthetic_fixture_metadata" for row in rows))
        self.assertTrue(all(not row["quality"]["decoder_verified"] for row in rows))
        self.assertTrue(all(row["confidence"] > .9 for row in rows))
        self.assertTrue(all(row["snr_kind"] == "synthetic_known_noise" for row in rows))
        self.assertTrue(all(15 < row["snr_db"] < 16 for row in rows))
        # The simulation's known injected bias is 4 sin(2a)+1.8 cos(a).
        self.assertTrue(all(abs(row["error_raw_deg"]) < 6.2 for row in rows))
        self.assertEqual(self.service.stats["count"], len(rows))
        self.assertTrue(self.service.snapshot()["source"]["simulated"])

    async def test_record_stop_and_replay_recomputes_from_iq(self):
        self.simulate_without_background()
        session = await self.service.start_recording("Replayprüfung")
        observation, iq = self.service.synthetic_observation(42, "DL7CR", self.service.config.channels[0], 7)
        expected_local = observation["local_deg"]
        deliberately_wrong = copy.deepcopy(observation)
        deliberately_wrong.update(local_deg=199, raw_deg=199, corrected_deg=199)
        await self.service.accept(deliberately_wrong, iq, self.service._sim_spectrum(1))
        stopped = await self.service.stop_recording()
        self.assertEqual(stopped["id"], session["id"])
        self.assertIsNotNone(stopped["stopped_at"])
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["packet_count"], 1)
        events = list(self.service.store.iter_events(session["id"]))
        self.assertEqual(events[0]["observation"]["local_deg"], 199)
        self.assertGreater(stopped["iq_bytes"], 0)
        await self.service.start_replay(session["id"], speed=20)
        await asyncio.wait_for(self.service._task, 4)
        self.assertEqual(self.service.source["status"], "finished", self.service.events)
        replayed = self.service.observations[-1]
        self.assertTrue(replayed["recomputed_from_iq"])
        self.assertAlmostEqual(replayed["local_deg"], expected_local, places=8)
        self.assertNotEqual(replayed["local_deg"], 199)
        self.assertEqual(replayed["sample_start"], observation["sample_start"])
        self.assertEqual(replayed["sample_end"], observation["sample_end"])
        self.assertEqual(replayed["stream_epoch"], observation["stream_epoch"])
        self.assertEqual(replayed["snr_db"], observation["snr_db"])
        self.assertEqual(replayed["snr_kind"], "synthetic_known_noise")
        self.assertEqual(self.service.replay["progress"], 1)
        await self.service.stop_replay()
        self.assertIsNone(self.service.replay)
        self.assertEqual(self.service.source["mode"], "idle")

    async def test_heading_alignment_handles_north_wrap_and_resets_statistics(self):
        self.service.config = AppConfig(station_mode="manual", heading_deg=330)
        for index, local in enumerate((344, 345, 346)):
            row = self.service._build_observation(
                self.payload(10), {"azimuth_deg": local, "confidence": .99, "power_db": -12},
                {"timestamp": utcnow(), "burst_id": f"heading-{index}"}, {"valid": True})
            await self.service.accept(row)
        self.assertEqual(len(self.service.observations), 3)
        result = await self.service.align_heading("DL7CR")
        self.assertLess(abs(wrap180(result["heading_deg"] - 25)), .03)
        self.assertEqual(result["packet_count"], 3)
        self.assertEqual(len(self.service.observations), 0)
        self.assertEqual(self.service.stats["count"], 0)
        restored = DemoService(Path(self.temp.name))
        self.assertAlmostEqual(restored.config.heading_deg, result["heading_deg"])
        await restored.close()

    async def test_configuration_rejects_channel_overlap_without_mutating_state(self):
        before = self.service.config.model_dump()
        channels = copy.deepcopy(before["channels"])
        channels[1]["frequency_hz"] = channels[0]["frequency_hz"]
        with self.assertRaises(ValueError):
            await self.service.update_config({"channels": channels})
        self.assertEqual(self.service.config.model_dump(), before)
        await self.service.update_config({"station_mode": "manual", "heading_deg": 42, "array_id": "square_center_320mm"})
        self.assertEqual(self.service.config.heading_deg, 42)
        self.assertEqual(self.service.config.array_id, "square_center_320mm")

    async def test_missing_gps_rejects_reference_but_manual_override_accepts(self):
        self.service.source.update(mode="live", simulated=False)
        estimate = {"azimuth_deg": 120, "confidence": .99, "power_db": -5}
        row = self.service._build_observation(self.payload(120), estimate, {}, {"valid": True})
        self.assertFalse(row["accepted"])
        self.assertIsNone(row["reference_deg"])
        self.assertFalse(row["quality"]["station_valid"])
        # Do not connect hardware merely to test station selection.
        self.service.source.update(mode="idle")
        await self.service.update_config({"station_mode": "manual", "station_latitude": 49, "station_longitude": 8})
        row = self.service._build_observation(self.payload(120), estimate, {}, {"valid": True})
        self.assertTrue(row["accepted"])
        self.assertAlmostEqual(row["reference_deg"], 120, delta=.02)
        self.assertEqual(row["quality"]["station_source"], "manual")

    async def test_decode_uses_same_packet_window_for_df_and_rf_sample_metadata(self):
        self.service.config = AppConfig(station_mode="manual", heading_deg=28)
        channel = self.service.config.channels[0].model_dump()
        vector = steering_vector(positions_for(self.service.config), channel["frequency_hz"], 92)
        iq = np.asarray(vector[:, None] * np.ones((1, 400)), dtype=np.complex64)
        burst = Burst(iq, 600000, channel["frequency_hz"], 1000, 2600, "epoch-A", channel,
                      time.time(), {"valid": True, "decimation_factor": 4})
        packet = DecodedPacket(self.payload(120), "pocsag", 17, 211, {"valid": True, "bch_corrected_bits": 0})
        with patch("krakendf.service.decode_pocsag", return_value=[packet]):
            await self.service._decode_burst(burst)
        self.assertEqual(len(self.service.observations), 1)
        row = self.service.observations[0]
        self.assertEqual(row["callsign"], "DL7CR")
        self.assertEqual(row["stream_epoch"], "epoch-A")
        self.assertEqual(row["sample_start"], 1000 + 17 * 4)
        self.assertEqual(row["sample_end"], 1000 + 211 * 4)
        self.assertEqual(row["iq_signal_start"], 17)
        self.assertEqual(row["iq_signal_end"], 211)
        self.assertEqual(row["iq_sample_rate_hz"], 600000)
        self.assertTrue(row["quality"]["decoder_verified"])
        self.assertIsNone(row["snr_db"], "A short clip without quiet noise must not invent SNR from the DF score")
        self.assertEqual(row["snr_status"], "no_noise_reference")
        self.assertLess(abs(row["error_raw_deg"]), .03)

    async def test_source_change_disables_calibration_from_old_data_type(self):
        self.simulate_without_background()
        model = {"id": "test-model", "eligible": True, "validated": True, "improved": True,
                 "coefficients": [1, 0, 0, 0, 0, 0, 0],
                 "config": self.service.effective_config(), "simulated": True}
        self.service.models.append(model)
        self.service.apply_model(model["id"])
        self.assertEqual(self.service.active_calibration_id, model["id"])
        await self.service.set_source("idle")
        self.assertIsNone(self.service.active_calibration_id,
                          "Changing source/data type must deactivate an incompatible calibration")

    async def test_replay_station_uses_recorded_position_instead_of_live_gps_anchor(self):
        self.service.config = AppConfig(station_mode="gps", station_latitude=48, station_longitude=9)
        self.service.source.update(mode="replay")
        self.service.gps_anchor = (52, 13)
        latitude, longitude, valid, source = self.service.station()
        self.assertEqual((latitude, longitude), (48, 9))
        self.assertTrue(valid)
        self.assertIn("record", source)

    async def test_replay_preserves_recorded_correction_when_global_model_is_removed(self):
        self.simulate_without_background()
        model = {"id": "recorded-model", "eligible": True, "validated": True, "improved": True,
                 "coefficients": [1.25, 0, 0, 0, 0, 0, 0],
                 "config": self.service.effective_config(), "simulated": True}
        self.service.models.append(model)
        self.service.apply_model(model["id"])
        session = await self.service.start_recording("Modell-Snapshot")
        observation, iq = self.service.synthetic_observation(80, "DL7CR", self.service.config.channels[0], 41)
        await self.service.accept(observation, iq)
        await self.service.stop_recording()
        recorded = self.service.store.observations(session["id"])[0]
        self.assertEqual(recorded["calibration_model"]["coefficients"][0], 1.25)
        self.service.models.clear()
        await self.service.start_replay(session["id"], speed=20)
        await asyncio.wait_for(self.service._task, 4)
        self.assertEqual(self.service.source["status"], "finished", self.service.events)
        replayed = self.service.observations[0]
        self.assertEqual(replayed["calibration_id"], "recorded-model")
        self.assertAlmostEqual(wrap180(replayed["raw_deg"] - replayed["corrected_deg"]), 1.25)

    async def test_bad_source_parameters_leave_running_simulation_untouched(self):
        await self.service.start("simulation")
        await self.wait_for_observations()
        task = self.service._task
        with self.assertRaises(ValueError):
            await self.service.set_source("live", host="invalid host name")
        self.assertIs(self.service._task, task)
        self.assertFalse(task.done())
        self.assertEqual(self.service.source["mode"], "simulation")


if __name__ == "__main__":
    unittest.main()
