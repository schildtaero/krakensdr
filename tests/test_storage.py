import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from openpyxl import load_workbook

from krakendf.storage import SessionCorruptError, SessionStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root)
        self.config = {"heading_deg": 24, "array_id": "uca_560mm", "channels": [{"id": "test"}]}
        self.session = self.store.start(self.config, "Testlauf", "calibration", True)

    def tearDown(self):
        self.temp.cleanup()

    def observation(self, **changes):
        result = {"callsign": "DL7CR", "timestamp": "2026-11-12T01:02:03+00:00",
                  "raw_deg": 359, "corrected_deg": 1, "reference_deg": 2,
                  "error_raw_deg": -3, "error_corrected_deg": -1,
                  "accepted": True, "sample_start": 1024, "sample_end": 1536,
                  "stream_epoch": "epoch-1", "burst_id": "burst-1", "run_id": self.session["id"],
                  "quality": {"iq_locked": True, "delay_locked": True}}
        result.update(changes)
        return result

    def test_roundtrip_retains_all_channels_samples_and_metadata(self):
        rng = np.random.default_rng(42)
        iq = (rng.normal(size=(5, 512)) + 1j * rng.normal(size=(5, 512))).astype(np.complex64)
        spectrum = {"start_hz": 433000000, "step_hz": 1000, "power_db": [-12, -8]}
        self.store.append(self.session["id"], self.observation(), iq, spectrum)
        self.config["channels"][0]["id"] = "changed"
        event = list(self.store.iter_events(self.session["id"]))[0]
        np.testing.assert_array_equal(self.store.load_iq(event), iq)
        self.assertEqual(event["observation"]["sample_start"], 1024)
        self.assertEqual(event["spectrum"], spectrum)
        metadata = SessionStore(self.root).get(self.session["id"])
        self.assertEqual(metadata["packet_count"], 1)
        self.assertGreater(metadata["iq_bytes"], iq.nbytes)
        self.assertEqual(metadata["config"]["channels"][0]["id"], "test")

    def test_stopped_session_rejects_append_and_stop_is_idempotent(self):
        first = self.store.stop(self.session["id"])
        second = self.store.stop(self.session["id"])
        self.assertEqual(first["stopped_at"], second["stopped_at"])
        with self.assertRaises(ValueError):
            self.store.append(self.session["id"], self.observation())

    def test_stale_manifest_after_committed_event_recovers_without_data_loss(self):
        self.store.append(self.session["id"], self.observation())
        path = self.root / self.session["id"] / "manifest.json"
        metadata = json.loads(path.read_text())
        metadata["packet_count"] = 0
        path.write_text(json.dumps(metadata))
        recovered = SessionStore(self.root).get(self.session["id"])
        self.assertEqual(recovered["packet_count"], 1)

    def test_partial_tail_is_reported_and_next_append_repairs_tail(self):
        self.store.append(self.session["id"], self.observation())
        path = self.root / self.session["id"] / "events.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"sequence":1,"observation":')
        recovered = SessionStore(self.root)
        self.assertEqual(recovered.get(self.session["id"])["packet_count"], 1)
        self.assertTrue(recovered.get(self.session["id"])["recovery_warnings"])
        recovered.append(self.session["id"], self.observation(burst_id="burst-2"))
        self.assertEqual(len(recovered.observations(self.session["id"])), 2)
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_manifest_write_failure_does_not_discard_committed_event(self):
        with patch("krakendf.storage._atomic_json", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.store.append(self.session["id"], self.observation())
        self.assertEqual(SessionStore(self.root).get(self.session["id"])["packet_count"], 1)

    def test_middle_corruption_is_not_silently_skipped(self):
        path = self.root / self.session["id"] / "events.jsonl"
        path.write_bytes(b"not-json\n")
        with self.assertRaises(SessionCorruptError):
            self.store.observations(self.session["id"])
        self.assertEqual(self.store.list_sessions()[0]["state"], "corrupt")

    def test_rejects_path_traversal_symlink_and_invalid_iq(self):
        with self.assertRaises(ValueError):
            self.store.get("../elsewhere")
        for iq in (np.ones((1, 3), dtype=np.complex64), np.ones((5, 3), dtype=np.complex128),
                   np.full((5, 3), np.nan, dtype=np.complex64)):
            with self.assertRaises(ValueError):
                self.store.append(self.session["id"], self.observation(), iq)
        self.store.append(self.session["id"], self.observation(), np.ones((5, 3), dtype=np.complex64))
        event = next(self.store.iter_events(self.session["id"]))
        bad = dict(event, iq_path="../../outside.npy")
        with self.assertRaises(ValueError):
            self.store.load_iq(bad)
        symlink = self.root / self.session["id"] / "iq" / "link.npy"
        symlink.symlink_to(self.root / self.session["id"] / event["iq_path"])
        with self.assertRaises(ValueError):
            self.store.load_iq(dict(event, iq_path="iq/link.npy"))

    def test_excel_has_all_sheets_numeric_values_and_literal_untrusted_text(self):
        malicious = '=HYPERLINK("https://invalid.example","click")'
        self.store.append(self.session["id"], self.observation(callsign=malicious, payload="+SUM(1,2)"))
        output = self.store.export_xlsx(self.session["id"], stats={"count": 1, "rms_raw_deg": 3.0,
            "rms_corrected_deg": 1.0, "bins": [{"start_deg": 0, "end_deg": 15, "count": 1,
                                               "rms_raw_deg": 3.0, "rms_corrected_deg": 1.0}]})
        workbook = load_workbook(io.BytesIO(output), data_only=False)
        self.assertEqual(workbook.sheetnames, ["Sitzung", "Messungen", "Gesamt", "Azimut", "Kalibrierung"])
        self.assertEqual(workbook["Messungen"]["B2"].value, malicious)
        self.assertEqual(workbook["Messungen"]["B2"].data_type, "s")
        self.assertEqual(workbook["Messungen"]["E2"].value, 359)
        self.assertEqual(workbook["Azimut"]["D2"].value, 3.0)
        for sheet in workbook:
            for row in sheet:
                for cell in row:
                    self.assertNotEqual(cell.data_type, "f")

    def test_current_empty_export_and_invalid_kind(self):
        output = self.store.export_xlsx(observations=[], config=self.config,
                                        stats={"count": 0, "bins": []})
        workbook = load_workbook(io.BytesIO(output))
        self.assertEqual(workbook["Messungen"].max_row, 1)
        with self.assertRaises(ValueError):
            self.store.start({}, "bad", "unsupported", False)

    def test_excel_preserves_received_control_bytes_as_visible_escapes(self):
        output = self.store.export_xlsx(observations=[self.observation(payload="RF\x00\x1ftext")],
                                       stats={"count": 1, "bins": []})
        workbook = load_workbook(io.BytesIO(output))
        headers = [cell.value for cell in workbook["Messungen"][1]]
        column = headers.index("Originaltelegramm") + 1
        self.assertEqual(workbook["Messungen"].cell(2, column).value, "RF\\x00\\x1ftext")


if __name__ == "__main__":
    unittest.main()
