"""Real coherent-IQ radar through HTTP, recording, replay and receiver arbitration."""
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import numpy as np
from openpyxl import load_workbook

from krakendf.app import create_app


class RadarAPIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name), initial_mode="idle")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.service = self.app.state.service

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text[:1000])
        return response.json()

    def wait_radar(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.ok(self.client.get("/api/radar/state"))
            self.assertNotEqual(state["status"], "error", state["message"])
            if predicate(state):
                return state
            time.sleep(.03)
        self.fail(f"Radar timeout: {state['status']}, {state['message']}")

    def test_assets_and_independent_persistent_config(self):
        for asset in ("radar.js", "radar.css"):
            self.assertEqual(self.client.get("/static/" + asset).status_code, 200)
        self.assertIn('id="radar-view"', self.client.get("/").text)
        before = self.ok(self.client.get("/api/config"))
        radar = self.ok(self.client.patch("/api/radar/config", json={
            "illuminator": "fm", "center_frequency_hz": 100_000_000,
            "bandwidth_hz": 200_000,
        }))
        self.assertEqual(radar["illuminator"], "fm")
        self.assertEqual(self.ok(self.client.get("/api/config")), before)
        for invalid in ({"surveillance_channel": 0}, {"sample_rate_hz": 500_000},
                        {"max_doppler_hz": 10_000}, {"invented": True}):
            self.assertEqual(self.client.patch("/api/radar/config", json=invalid).status_code, 400)
            self.assertEqual(self.ok(self.client.get("/api/radar/config")), radar)
        self.assertEqual(self.ok(self.client.get("/api/radar/sessions")), {"sessions": []})
        with self.client.websocket_connect("/ws") as socket:
            self.assertEqual(socket.receive_json()["data"]["radar"]["config"], radar)

    def test_real_iq_simulation_record_recompute_and_excel(self):
        before = self.ok(self.client.get("/api/config"))
        self.ok(self.client.post("/api/source", json={"mode": "simulation"}))
        self.ok(self.client.post("/api/radar/start", json={"mode": "simulation"}))
        state = self.wait_radar(lambda s: s["processed_cpis"] > 0)
        self.assertEqual(self.service.source["mode"], "idle")
        self.assertTrue(state["simulated"])
        self.assertEqual(self.ok(self.client.get("/api/config")), before)
        result = state["result"]
        self.assertTrue(result["simulated"])
        self.assertTrue(result["quality"]["valid"])
        for delay, doppler in ((42, 80), (85, -120)):
            candidate = min(result["candidates"], key=lambda c: abs(c["delay_samples"]-delay))
            self.assertEqual(candidate["delay_samples"], delay)
            self.assertAlmostEqual(candidate["doppler_hz"], doppler, delta=4.1)
        self.assertEqual(self.client.patch("/api/config", json={"heading_deg": 10}).status_code, 400)
        self.assertEqual(self.client.patch("/api/radar/config", json={"gain_db": 0}).status_code, 400)
        self.assertEqual(self.client.post("/api/record/start", json={}).status_code, 400)
        recording = self.ok(self.client.post("/api/radar/record/start", json={"name": "DAB CPI"}))
        self.wait_radar(lambda s: s["recording"] and s["recording"]["frame_count"] >= 1)
        self.assertEqual(self.client.post("/api/source", json={"mode": "simulation"}).status_code, 400)
        saved = self.ok(self.client.post("/api/radar/record/stop", json={}))
        self.assertGreaterEqual(saved["frame_count"], 1)
        sid = recording["id"]
        events = list(self.service.radar.store.iter_events(sid))
        self.assertEqual(events[0]["metadata"]["iq_stage"], "channelized_pair_with_prefix")
        original = self.service.radar.store.load_result(events[0])
        self.ok(self.client.post("/api/radar/stop", json={}))
        # A replay uses the saved acquisition settings and real IQ, restoring
        # the operator's new settings when replay stops.
        self.ok(self.client.patch("/api/radar/config", json={"threshold_db": 13}))
        self.ok(self.client.post("/api/radar/replay/start", json={"session_id": sid, "speed": 20}))
        replay = self.wait_radar(lambda s: s["status"] == "finished")
        self.assertEqual(replay["replay"]["progress"], 1)
        self.assertEqual(replay["config"]["threshold_db"], 12)
        self.assertTrue(replay["result"]["simulated"])
        if len(events) == 1:
            np.testing.assert_array_equal(replay["result"]["power_db"], original["power_db"])
        self.assertEqual(self.ok(self.client.get("/api/config")), before)
        self.ok(self.client.post("/api/radar/replay/stop", json={}))
        stopped = self.ok(self.client.get("/api/radar/state"))
        self.assertEqual(stopped["config"]["threshold_db"], 13)
        self.assertIsNone(stopped["result"])
        excel = self.client.get("/api/radar/export.xlsx", params={"session_id": sid})
        self.assertEqual(excel.status_code, 200)
        book = load_workbook(io.BytesIO(excel.content), data_only=True)
        self.assertGreaterEqual(len(book.sheetnames), 3)
        cells = [cell for sheet in book for row in sheet.iter_rows(values_only=True) for cell in row]
        self.assertIn("simulation", cells)
        self.assertTrue(any(isinstance(value, (int, float)) and value > 1000 for value in cells))
        self.assertEqual(self.ok(self.client.get("/api/sessions")), {"sessions": []})

    def test_invalid_start_and_replay_preserve_running_df(self):
        self.ok(self.client.post("/api/source", json={"mode": "simulation"}))
        for invalid in ({"mode": "invented"}, {"mode": "live", "host": "bad host"},
                        {"mode": "live", "host": "remote", "managed": True}):
            self.assertEqual(self.client.post("/api/radar/start", json=invalid).status_code, 400)
            self.assertEqual(self.service.source["mode"], "simulation")
        self.assertEqual(self.client.post("/api/radar/replay/start", json={
            "session_id": "0"*32}).status_code, 404)
        self.assertEqual(self.service.source["mode"], "simulation")
        session = self.service.radar.store.start(self.service.radar.config.model_dump())
        self.service.radar.store.stop(session["id"])
        self.assertEqual(self.client.post("/api/radar/replay/start", json={
            "session_id": session["id"]}).status_code, 400)
        self.assertEqual(self.service.source["mode"], "simulation")

    def test_radar_inherits_external_receiver_and_never_falls_back(self):
        self.ok(self.client.post("/api/source", json={"mode": "idle", "managed": False,
                                                    "host": "daq.example", "port": 5100}))
        with patch.object(self.service.radar, "start", new_callable=AsyncMock) as start:
            start.return_value = {"mode": "live"}
            self.ok(self.client.post("/api/radar/start", json={"mode": "live"}))
            start.assert_awaited_once_with("live", managed=False, host="daq.example", port=5100)
        with patch("krakendf.radar_service.HeimdallClient.connect", new_callable=AsyncMock,
                   side_effect=RuntimeError("No receiver")):
            self.ok(self.client.post("/api/radar/start", json={"mode": "live"}))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                state = self.ok(self.client.get("/api/radar/state"))
                if state["status"] == "error":
                    break
                time.sleep(.02)
            self.assertEqual(state["mode"], "live")
            self.assertEqual(state["status"], "error")
            self.assertFalse(state["simulated"])
            self.assertIsNone(state["result"])
        self.ok(self.client.post("/api/radar/stop", json={}))
        self.ok(self.client.patch("/api/radar/config", json={"gain_db": 0}))


if __name__ == "__main__":
    unittest.main()
