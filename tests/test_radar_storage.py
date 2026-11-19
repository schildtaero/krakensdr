import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from openpyxl import load_workbook

from krakendf.radar_storage import (
    MAX_PAIR_SAMPLES, RadarRecordingCorruptError, RadarStorageLimitError, RadarStore,
)


class RadarStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = RadarStore(self.root, min_free_bytes=0)
        self.config = {"profile": "fm", "frequency_hz": 100_000_000,
                       "reference_channel": 0, "surveillance_channel": 1,
                       "cpi_seconds": 0.25, "cancellation": {"taps": 16}}
        self.session = self.store.start(self.config, "UKW-Versuch", simulated=True)
        rng = np.random.default_rng(13)
        self.iq = (rng.normal(size=(2, 80)) + 1j * rng.normal(size=(2, 80))).astype(np.complex64)

    def tearDown(self):
        self.temp.cleanup()

    def metadata(self, **changes):
        result = {"iq_stage": "channelized_pair_with_prefix", "sample_rate_hz": 400_000,
                  "frequency_hz": 100_000_000, "sample_start": 0, "sample_end": 480,
                  "cpi_sample_start": 60, "cpi_sample_end": 480,
                  "decimation_factor": 6, "stream_epoch": "receiver-run-1",
                  "timestamp": "2026-11-19T12:01:02+00:00",
                  "source_mode": "simulation", "simulated": True,
                  "reference_channel": 0, "surveillance_channel": 1}
        result.update(changes)
        return result

    def result(self):
        return {"power_db": np.zeros((20, 25)), "doppler_hz": np.arange(20),
                "excess_path_m": np.arange(25) * 749.481,
                "quality": {"cancellation_db": 22.0, "accepted": True},
                "candidates": [{"delay_samples": 3, "delay_seconds": 0.0000075,
                                "excess_path_m": 2248.443, "doppler_hz": -40.0,
                                "excess_path_rate_m_s": 119.9169832,
                                "power_db": -28.0, "contrast_db": 14.0,
                                "accepted": False, "flags": ["demo_candidate"]}]}

    def append(self, **changes):
        return self.store.append(self.session["id"], self.iq, self.metadata(**changes), self.result())

    def test_roundtrip_retains_pair_stage_profile_source_and_full_map(self):
        event = self.append()
        self.config["cancellation"]["taps"] = 999
        recovered = RadarStore(self.root, min_free_bytes=0)
        saved = recovered.get(self.session["id"])
        self.assertEqual(saved["config"]["cancellation"]["taps"], 16)
        self.assertEqual(saved["frame_count"], 1)
        self.assertGreater(saved["iq_bytes"], self.iq.nbytes)
        self.assertGreater(saved["stored_bytes"], saved["iq_bytes"])
        self.assertEqual(saved["source_mode"], "simulation")
        np.testing.assert_array_equal(recovered.load_pair(event), self.iq)
        self.assertEqual(event["metadata"]["sample_rate_hz"], 400_000)
        self.assertEqual(event["metadata"]["sample_end"], 480)
        self.assertEqual(event["metadata"]["iq_stage"], "channelized_pair_with_prefix")
        self.assertNotIn("power_db", event["result"])
        self.assertNotIn("doppler_hz", event["result"])
        full = recovered.load_result(event)
        np.testing.assert_array_equal(full["power_db"], np.zeros((20, 25)))
        self.assertFalse(event["result"]["candidates"][0]["accepted"])
        self.assertEqual(event["result"]["candidates"][0]["flags"], ["demo_candidate"])
        self.assertEqual(list(recovered.iter_events(self.session["id"])), [event])

    def test_raw_pair_and_stage_alias_replay_without_conversion(self):
        metadata = self.metadata(sample_end=80, decimation_factor=1, stage="raw_pair",
                                 cpi_sample_start=0, cpi_sample_end=80)
        del metadata["iq_stage"]
        event = self.store.append(self.session["id"], self.iq, metadata)
        self.assertEqual(event["metadata"]["iq_stage"], "raw_pair")
        self.assertIsNone(self.store.load_result(event))
        np.testing.assert_array_equal(self.store.load_pair(event), self.iq)

    def test_stopped_recording_rejects_append_and_stop_is_idempotent(self):
        first = self.store.stop(self.session["id"])
        second = self.store.stop(self.session["id"])
        self.assertEqual(first["stopped_at"], second["stopped_at"])
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.append()
        self.assertEqual(self.store.get(self.session["id"])["frame_count"], 0)

    def test_stale_manifest_and_partial_event_tail_recover(self):
        self.append()
        directory = self.root / self.session["id"]
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest.update(frame_count=0, iq_bytes=0, stored_bytes=0)
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with (directory / "events.jsonl").open("ab") as stream:
            stream.write(b'{"session_id":')
        recovered = RadarStore(self.root, min_free_bytes=0)
        self.assertEqual(recovered.get(self.session["id"])["frame_count"], 1)
        self.assertTrue(recovered.get(self.session["id"])["recovery_warnings"])
        recovered.append(self.session["id"], self.iq, self.metadata(
            sample_start=420, sample_end=900, cpi_sample_start=480, cpi_sample_end=900))
        self.assertEqual(recovered.get(self.session["id"])["frame_count"], 2)
        self.assertTrue((directory / "events.jsonl").read_bytes().endswith(b"\n"))

    def test_committed_event_survives_manifest_write_failure(self):
        with patch("krakendf.radar_storage._atomic_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.append(self.session["id"], self.iq, self.metadata())
        # Even the original object's cache must recover from the committed log.
        self.assertEqual(self.store.get(self.session["id"])["frame_count"], 1)
        event = next(self.store.iter_events(self.session["id"]))
        np.testing.assert_array_equal(self.store.load_pair(event), self.iq)

    def test_committed_corruption_is_reported_and_never_skipped(self):
        (self.root / self.session["id"] / "events.jsonl").write_bytes(b"not-json\n")
        with self.assertRaises(RadarRecordingCorruptError):
            self.store.get(self.session["id"])
        self.assertEqual(self.store.list_sessions()[0]["state"], "corrupt")

    def test_rejects_invalid_iq_and_inconsistent_processing_metadata(self):
        for iq in (np.ones((5, 80), dtype=np.complex64), self.iq.astype(np.complex128),
                   np.full((2, 80), np.inf, dtype=np.complex64), np.zeros((2, 0), dtype=np.complex64)):
            with self.subTest(iq=iq.shape):
                with self.assertRaises(ValueError):
                    self.store.append(self.session["id"], iq, self.metadata())
        for changes in ({"iq_stage": None}, {"sample_end": 479}, {"sample_rate_hz": np.nan},
                        {"sample_start": -1}, {"decimation_factor": 0}, {"stream_epoch": ""},
                        {"simulated": False}, {"source_mode": "live"},
                        {"reference_channel": 1}, {"surveillance_channel": 5}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.append(**changes)
        self.assertEqual(self.store.get(self.session["id"])["frame_count"], 0)

    def test_rejects_traversal_symlinks_and_forged_committed_events(self):
        with self.assertRaises(ValueError):
            self.store.get("../outside")
        event = self.append()
        for forged in (dict(event, iq_path="../../outside.npy"),
                       dict(event, session_id="../outside"),
                       dict(event, sequence=99),
                       dict(event, metadata=self.metadata(sample_end=960))):
            with self.assertRaises(ValueError):
                self.store.load_pair(forged)
        path = self.root / self.session["id"] / event["iq_path"]
        outside = self.root / "outside.npy"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaises(RadarRecordingCorruptError):
            self.store.load_pair(event)
        directory = self.root / self.session["id"]
        (directory / "results").rename(directory / "original_results")
        (directory / "results").symlink_to(directory / "original_results", target_is_directory=True)
        with self.assertRaises(RadarRecordingCorruptError):
            self.append()

    def test_session_symlink_and_metadata_special_files_are_refused(self):
        session_id = "f" * 32
        (self.root / session_id).symlink_to(self.root / self.session["id"], target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.get(session_id)
        manifest = self.root / self.session["id"] / "manifest.json"
        saved = self.root / "manifest_saved.json"
        manifest.rename(saved)
        manifest.symlink_to(saved)
        with self.assertRaises(RadarRecordingCorruptError):
            self.store.get(self.session["id"])

    def test_storage_budget_and_free_space_limits_precede_writes(self):
        limited = RadarStore(self.root, max_session_bytes=self.iq.nbytes, min_free_bytes=0)
        with self.assertRaises(RadarStorageLimitError):
            limited.append(self.session["id"], self.iq, self.metadata())
        disk_usage = type("DiskUsage", (), {"free": 0})()
        with patch("krakendf.radar_storage.shutil.disk_usage", return_value=disk_usage):
            with self.assertRaises(RadarStorageLimitError):
                self.append()
        self.assertFalse(list((self.root / self.session["id"] / "iq").iterdir()))
        self.assertEqual(self.store.get(self.session["id"])["frame_count"], 0)

    def test_overlapping_delay_prefix_is_contiguous_but_epoch_and_cpi_gaps_are_marked(self):
        self.append()
        contiguous = self.append(sample_start=420, sample_end=900, cpi_sample_start=480, cpi_sample_end=900)
        self.assertFalse(contiguous["metadata"]["recording_discontinuity"])
        gap = self.append(sample_start=1000, sample_end=1480, cpi_sample_start=1060, cpi_sample_end=1480)
        self.assertTrue(gap["metadata"]["recording_discontinuity"])
        restart = self.append(sample_start=0, sample_end=480, stream_epoch="receiver-run-2")
        self.assertTrue(restart["metadata"]["recording_discontinuity"])

    def test_replay_rejects_corrupt_iq_header_and_result(self):
        event = self.append()
        path = self.root / self.session["id"] / event["iq_path"]
        with path.open("wb") as stream:
            np.lib.format.write_array_header_1_0(stream, {
                "descr": "<c8", "fortran_order": False, "shape": (2, MAX_PAIR_SAMPLES * 100)})
            stream.truncate(event["iq_bytes"])
        with self.assertRaises(RadarRecordingCorruptError):
            self.store.load_pair(event)
        result_path = self.root / self.session["id"] / event["result_path"]
        result_path.write_text("broken-json")
        with self.assertRaises(RadarRecordingCorruptError):
            self.store.load_result(event)

    def test_bounded_arrays_reject_huge_views_before_materializing(self):
        huge_iq = np.broadcast_to(np.complex64(0), (2, MAX_PAIR_SAMPLES + 1))
        with self.assertRaises(ValueError):
            self.store.append(self.session["id"], huge_iq, self.metadata())
        huge_result = {"power_db": np.broadcast_to(np.float32(0), (1000, 1000))}
        with self.assertRaises(ValueError):
            self.store.append(self.session["id"], self.iq, self.metadata(), huge_result)
        self.assertFalse(list((self.root / self.session["id"] / "iq").iterdir()))

    def test_largest_display_matrix_is_saved_outside_event_log(self):
        result = {"power_db": np.zeros((1000, 512), dtype=np.float32), "candidates": []}
        event = self.store.append(self.session["id"], self.iq, self.metadata(), result)
        self.assertNotIn("power_db", event["result"])
        self.assertEqual(len(self.store.load_result(event)["power_db"]), 1000)
        self.assertLess((self.root / self.session["id"] / "events.jsonl").stat().st_size, 2048)

    def test_excel_retains_numeric_candidates_profile_flags_and_literal_text(self):
        self.append()
        workbook = load_workbook(io.BytesIO(self.store.export_xlsx(self.session["id"])))
        self.assertEqual(workbook.sheetnames, ["Radarsitzung", "Radarfenster", "Radarkandidaten"])
        frame = list(workbook["Radarfenster"].values)
        frame_row = dict(zip(frame[0], frame[1]))
        self.assertEqual(frame_row["Quelle"], "simulation")
        self.assertEqual(frame_row["IQ-Abtastrate Hz"], 400_000)
        rows = list(workbook["Radarkandidaten"].values)
        candidate = dict(zip(rows[0], rows[1]))
        self.assertEqual(candidate["Doppler Hz"], -40)
        self.assertEqual(candidate["Bistatischer Mehrweg m"], 2248.443)
        self.assertEqual(candidate["Lokaler Kontrast dB"], 14)
        self.assertFalse(candidate["Akzeptiert"])
        self.assertEqual(candidate["Quelle"], "simulation")
        self.assertIn('"accepted":false', candidate["Kandidat vollständig JSON"])
        summary = dict(list(workbook["Radarsitzung"].values)[1:])
        self.assertIn('"profile":"fm"', summary["config"])
        malicious = '=HYPERLINK("https://invalid.example","click")'
        other = self.store.start(self.config, malicious)
        exported = load_workbook(io.BytesIO(self.store.export_xlsx(other["id"])))
        for row in exported["Radarsitzung"]:
            for cell in row:
                self.assertNotEqual(cell.data_type, "f")

    def test_return_values_do_not_mutate_committed_metadata(self):
        event = self.append()
        event["metadata"]["stream_epoch"] = "modified"
        manifest = self.store.get(self.session["id"])
        manifest["config"]["profile"] = "modified"
        saved = next(self.store.iter_events(self.session["id"]))
        self.assertEqual(saved["metadata"]["stream_epoch"], "receiver-run-1")
        self.assertEqual(self.store.get(self.session["id"])["config"]["profile"], "fm")


if __name__ == "__main__":
    unittest.main()
