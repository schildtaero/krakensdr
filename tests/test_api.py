"""Local API integration tests: no Kraken, GPS device or remote tile access.

Simulation exercises the actual five-channel DSP estimator. Packet identities
in that mode deliberately come from fixture metadata, not a claimed RF decode.
"""
import io
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from starlette.websockets import WebSocketDisconnect

from krakendf.app import create_app
from krakendf.dsp import wrap180


class APIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.app = create_app(self.data, initial_mode="idle")
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.service = self.app.state.service

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text[:2000])
        return response.json()

    def wait_state(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.ok(self.client.get("/api/state"))
            if predicate(state):
                return state
            if state["source"]["status"] == "error":
                self.fail(f"Source error during integration test: {state['source']['message']}")
            time.sleep(.03)
        self.fail(f"Timed out waiting for state; final state: {state}")

    def manual_position(self):
        return self.ok(self.client.patch("/api/config", json={
            "station_mode": "manual", "station_latitude": 52.1,
            "station_longitude": 13.2, "heading_deg": 0,
        }))

    def fixture_session(self, name, references, *, kind="measurement", start_index=100,
                        simulated=True, config_patch=None, with_iq=False):
        config = self.service.effective_config()
        if config_patch:
            config = {**config, **config_patch}
        session = self.service.store.start(config, name, kind, simulated)
        channel = self.service.config.channels[0]
        for offset, reference in enumerate(references):
            observation, iq = self.service.synthetic_observation(reference, "DL7CR", channel,
                                                                start_index + offset, session["id"])
            self.assertTrue(observation["accepted"])
            self.service.store.append(session["id"], observation, iq if with_iq else None)
        return self.service.store.stop(session["id"])

    def test_health_assets_metadata_and_unknown_paths(self):
        health = self.ok(self.client.get("/api/health"))
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["source_mode"], "idle")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Kraken", page.text)
        for asset in ("app.js", "styles.css", "map.js", "map.css",
                      "vendor/leaflet/leaflet.js", "vendor/leaflet/leaflet.css",
                      "vendor/milsymbol/milsymbol.js"):
            result = self.client.get(f"/static/{asset}")
            self.assertEqual(result.status_code, 200, asset)
            self.assertGreater(len(result.content), 100)
        self.assertEqual(self.client.get("/does-not-exist").status_code, 404)
        self.assertEqual(self.client.get("/static/does-not-exist.js").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/" + "0" * 32).status_code, 404)
        self.assertEqual(len(self.ok(self.client.get("/api/arrays"))["presets"]), 3)
        self.assertTrue(self.ok(self.client.get("/api/hardware/gains"))["gains_db"])
        self.assertIsInstance(self.ok(self.client.get("/api/decoders")), dict)
        self.assertEqual(self.ok(self.client.get("/api/gps/ports"))["model"], "ARCELI VK-162")

    def test_invalid_configuration_is_rejected_without_partial_mutation(self):
        original = self.ok(self.client.get("/api/config"))
        for invalid in ({"fft_size": 1000}, {"gain_db": 100}, {"station_latitude": 95},
                        {"station_longitude": 181}, {"heading_deg": 360}, {"array_id": "missing"},
                        {"unknown_field": True}, {"center_frequency_hz": 10}):
            response = self.client.patch("/api/config", json=invalid)
            self.assertIn(response.status_code, (400, 422), response.text)
            self.assertEqual(self.ok(self.client.get("/api/config")), original)
        channels = original["channels"]
        channels[1]["frequency_hz"] = channels[0]["frequency_hz"]
        response = self.client.patch("/api/config", json={"channels": channels})
        self.assertIn(response.status_code, (400, 422))
        self.assertEqual(self.ok(self.client.get("/api/config"))["channels"][1]["frequency_hz"], 434200000)
        self.assertEqual(self.client.post("/api/source", json={"mode": "invented"}).status_code, 400)

    def test_manual_position_and_heading_alignment_use_known_reference(self):
        state = self.manual_position()
        self.assertTrue(state["station"]["valid"])
        self.assertEqual(state["station"]["source"], "manual")
        self.assertEqual(state["config"]["station_latitude"], 52.1)
        channel = self.service.config.channels[0]
        observation, iq = self.service.synthetic_observation(73, "DL7CR", channel, 27)
        self.client.portal.call(self.service.accept, observation, iq)
        result = self.ok(self.client.post("/api/heading", json={"callsign": "dl7cr"}))
        expected = (observation["reference_deg"] - observation["local_deg"]) % 360
        self.assertLess(abs(wrap180(result["heading_deg"] - expected)), 1e-6)
        self.assertEqual(result["packet_count"], 1)
        state = self.ok(self.client.get("/api/state"))
        self.assertEqual(state["stats"]["count"], 0)
        self.assertEqual(self.client.post("/api/heading", json={"callsign": "UNKNOWN"}).status_code, 400)

    def test_invalid_source_host_preserves_running_source_and_observations(self):
        self.manual_position()
        self.ok(self.client.post("/api/source", json={"mode": "simulation"}))
        before = self.wait_state(lambda value: len(value["emitters"]) == 3)
        task = self.service._task
        result = self.client.post("/api/source", json={"mode": "simulation", "host": "invalid host"})
        self.assertEqual(result.status_code, 400)
        after = self.ok(self.client.get("/api/state"))
        self.assertEqual(after["source"]["mode"], before["source"]["mode"])
        self.assertEqual(after["source"]["host"], before["source"]["host"])
        self.assertGreaterEqual(after["stats"]["count"], before["stats"]["count"])
        self.assertIs(self.service._task, task)
        self.assertFalse(task.cancelled())

    def test_symbols_are_persistent_and_invalid_sidcs_do_not_overwrite(self):
        symbol = {"sidc": "SHGPUCI--------", "label": "Referenz 1"}
        response = self.ok(self.client.patch("/api/symbols/dl7cr", json=symbol))
        self.assertEqual(response["symbols"]["DL7CR"], symbol)
        for sidc in ("BAD", "XXXXXXXXXXXXXXX", "123456789012345", "S!GPU----------"):
            response = self.client.patch("/api/symbols/DL7CR", json={"sidc": sidc})
            self.assertIn(response.status_code, (400, 422), sidc)
        self.assertEqual(self.ok(self.client.get("/api/symbols"))["symbols"]["DL7CR"], symbol)
        self.assertIn("DL7CR", self.ok(self.client.get("/api/state"))["symbols"])
        with TestClient(create_app(self.data, initial_mode="idle")) as reloaded:
            self.assertEqual(reloaded.get("/api/symbols").json()["symbols"]["DL7CR"], symbol)

    def test_map_mount_defaults_offline_and_rejects_bad_upload(self):
        with patch("krakendf.maps.MapCache._fetch_remote", new_callable=AsyncMock) as remote:
            info = self.ok(self.client.get("/api/map/status"))
            self.assertTrue(info["offline"])
            missing = self.client.get("/api/map/tiles/10/549/333.png")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.headers["x-offline-cache"], "miss")
            remote.assert_not_awaited()
        self.assertEqual(self.client.get("/api/map/tiles/2/10/0.png").status_code, 400)
        self.assertEqual(self.client.put("/api/map/mbtiles", content=b"not sqlite",
                                        headers={"Content-Type": "application/octet-stream"}).status_code, 422)
        self.assertEqual(self.client.post("/api/map/settings", json={"offline": "false"}).status_code, 422)

    def test_simulation_record_replay_and_excel_use_actual_dsp_tracks(self):
        self.manual_position()
        self.ok(self.client.post("/api/source", json={"mode": "simulation"}))
        state = self.wait_state(lambda value: len(value["emitters"]) == 3)
        self.assertTrue(state["source"]["simulated"])
        self.assertGreaterEqual(state["stats"]["count"], 3)
        for emitter in state["emitters"]:
            self.assertTrue(emitter["accepted"])
            self.assertFalse(emitter["quality"]["decoder_verified"])
            self.assertEqual(emitter["quality"]["identity_source"], "synthetic_fixture_metadata")
            self.assertLess(abs(emitter["error_raw_deg"]), 9)
            self.assertTrue(math.isfinite(emitter["latitude"]))
        started = self.ok(self.client.post("/api/record/start", json={"name": "Integration", "kind": "measurement"}))
        self.assertTrue(started["simulated"])
        self.assertEqual(self.client.patch("/api/config", json={"heading_deg": 20}).status_code, 400)
        self.assertEqual(self.client.post("/api/source", json={"mode": "idle"}).status_code, 400)
        self.wait_state(lambda value: value["recording"] and value["recording"]["packet_count"] >= 3)
        stopped = self.ok(self.client.post("/api/record/stop"))
        self.assertGreaterEqual(stopped["packet_count"], 3)
        self.assertGreater(stopped["iq_bytes"], 0)
        self.assertEqual(stopped["state"], "stopped")
        session_id = stopped["id"]
        observations = self.service.store.observations(session_id)
        self.assertEqual(len(observations), stopped["packet_count"])
        stored = self.ok(self.client.get(f"/api/sessions/{session_id}"))
        self.assertEqual(stored["stats"]["count"], len(observations))
        self.ok(self.client.post("/api/replay/start", json={"session_id": session_id, "speed": 20}))
        replay = self.wait_state(lambda value: value["source"]["status"] == "finished")
        self.assertEqual(replay["source"]["mode"], "replay")
        self.assertTrue(replay["source"]["simulated"])
        self.assertEqual(replay["replay"]["progress"], 1)
        self.assertEqual(replay["stats"]["count"], len(observations))
        originals = {row["callsign"]: row for row in observations}
        for emitter in replay["emitters"]:
            self.assertTrue(emitter["recomputed_from_iq"])
            self.assertLess(abs(wrap180(emitter["raw_deg"] - originals[emitter["callsign"]]["raw_deg"])), 1e-6)
        export = self.client.get("/api/export.xlsx", params={"session_id": session_id})
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export.headers["content-type"])
        workbook = load_workbook(io.BytesIO(export.content))
        self.assertEqual(workbook["Messungen"].max_row, len(observations) + 1)
        self.assertEqual(workbook.sheetnames, ["Sitzung", "Messungen", "Gesamt", "Azimut", "Kalibrierung"])
        self.ok(self.client.post("/api/replay/stop"))
        self.assertEqual(self.ok(self.client.get("/api/state"))["source"]["mode"], "idle")
        self.ok(self.client.post("/api/measurements/reset"))
        self.assertEqual(self.ok(self.client.get("/api/state"))["stats"]["count"], 0)

    def test_calibration_requires_independent_matching_sessions_and_real_coverage(self):
        self.manual_position()
        training = self.fixture_session("Training", [7.5 + sector * 15 + offset for sector in range(24)
                                                     for offset in (-3, 0, 3)],
                                        kind="calibration", start_index=1000)
        validation = self.fixture_session("Prüfung", [8.5 + sector * 15 for sector in range(24)],
                                          kind="validation", start_index=5000)
        same = self.client.post("/api/calibration/fit", json={"training_session_id": training["id"], "validation_session_id": training["id"]})
        self.assertEqual(same.status_code, 400)
        model = self.ok(self.client.post("/api/calibration/fit", json={
            "training_session_id": training["id"], "validation_session_id": validation["id"]}))
        self.assertTrue(model["eligible"], model["reason"])
        self.assertTrue(model["validated"], model["reason"])
        self.assertTrue(model["independent_runs"], model["reason"])
        self.assertTrue(model["coverage_complete"], model["reason"])
        self.assertTrue(model["improved"], model["reason"])
        self.assertLess(model["validation_after_deg"], model["validation_before_deg"])
        self.assertEqual(model["training_count"], 72)
        self.assertEqual(model["validation_count"], 24)
        self.assertEqual(self.client.post("/api/calibration/apply", json={"calibration_id": model["id"]}).status_code, 400)
        self.ok(self.client.post("/api/source", json={"mode": "simulation"}))
        applied = self.ok(self.client.post("/api/calibration/apply", json={"calibration_id": model["id"]}))
        self.assertEqual(applied["calibration"]["active_id"], model["id"])
        self.assertIsNone(self.ok(self.client.post("/api/calibration/apply", json={"calibration_id": None}))["calibration"]["active_id"])
        listed = self.ok(self.client.get("/api/calibrations"))["models"]
        self.assertIn(model["id"], [item["id"] for item in listed])

    def test_sparse_and_mismatched_calibrations_cannot_be_applied(self):
        self.manual_position()
        training = self.fixture_session("Wenig Training", [15, 30, 45], kind="calibration", start_index=101)
        validation = self.fixture_session("Wenig Prüfung", [18, 33, 48], kind="validation", start_index=201)
        model = self.ok(self.client.post("/api/calibration/fit", json={
            "training_session_id": training["id"], "validation_session_id": validation["id"]}))
        self.assertFalse(model["eligible"])
        self.assertFalse(model["validated"])
        self.assertEqual(self.client.post("/api/calibration/apply", json={"calibration_id": model["id"]}).status_code, 400)
        mismatch = self.fixture_session("Anderes Heading", [18], kind="validation", start_index=301,
                                        config_patch={"heading_deg": 30})
        self.assertEqual(self.client.post("/api/calibration/fit", json={
            "training_session_id": training["id"], "validation_session_id": mismatch["id"]}).status_code, 400)

    def test_idle_record_replay_constraints_and_websocket_snapshot(self):
        self.assertEqual(self.client.post("/api/record/start", json={"name": "No source"}).status_code, 400)
        self.assertEqual(self.client.post("/api/record/stop").status_code, 400)
        self.assertEqual(self.client.post("/api/replay/start", json={"session_id": "0" * 32, "speed": 0}).status_code, 422)
        empty = self.service.store.start(self.service.effective_config(), "Leer", "measurement", True)
        self.assertEqual(self.client.post("/api/replay/start", json={"session_id": empty["id"]}).status_code, 400)
        self.service.store.stop(empty["id"])
        self.assertEqual(self.client.post("/api/replay/start", json={"session_id": empty["id"]}).status_code, 400)
        with self.client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as socket:
            message = socket.receive_json()
            self.assertEqual(message["type"], "state")
            self.assertEqual(message["data"]["source"]["mode"], "idle")
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with self.client.websocket_connect("/ws", headers={"origin": "https://unrelated.example"}):
                pass
        self.assertEqual(rejected.exception.code, 1008)


if __name__ == "__main__":
    unittest.main()
