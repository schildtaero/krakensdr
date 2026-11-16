"""Support export checks: persistence, bounded logs and fixed local scope."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient

from krakendf.app import create_app
from krakendf.diagnostics import (DiagnosticLog, TAIL_BYTES, TOTAL_LOG_BYTES,
                                  build_diagnostic_archive, diagnostic_snapshot)
from krakendf.service import DemoService


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, events=None):
        data = build_diagnostic_archive(self.root, {"test": True}, events or [])
        return zipfile.ZipFile(io.BytesIO(data))

    def test_event_log_persists_rotates_and_keeps_traceback(self):
        with patch("krakendf.diagnostics.LOG_BYTES", 600), patch("krakendf.diagnostics.LOG_BACKUPS", 2):
            log = DiagnosticLog(self.root)
            for index in range(20):
                log.write(f"event-{index}: " + "x" * 120)
            try:
                raise RuntimeError("burst processor failed; token=top-secret")
            except RuntimeError as exc:
                log.exception("receiver worker", exc)
            log.close()
        files = list((self.root / "logs").glob("application.log*"))
        self.assertLessEqual(len(files), 3)
        text = "\n".join(path.read_text() for path in files)
        self.assertIn("Traceback", text)
        self.assertIn("receiver worker", text)
        self.assertNotIn("top-secret", text)
        later = DiagnosticLog(self.root)
        later.write("after-restart")
        later.close()
        self.assertIn("after-restart", (self.root / "logs" / "application.log").read_text())

    def test_archive_missing_logs_is_readable_and_preserves_recent_events(self):
        with self.archive([{"time": "now", "level": "error", "message": "queue overrun"}]) as archive:
            self.assertEqual(set(archive.namelist()), {"README.txt", "diagnostics.json", "recent-events.json"})
            self.assertIn("queue overrun", archive.read("recent-events.json").decode())
            self.assertIn("Noch keine", archive.read("README.txt").decode())

    def test_log_tails_are_bounded_and_arbitrary_files_are_excluded(self):
        daq = self.root / "logs" / "daq"
        daq.mkdir(parents=True)
        (self.root / "logs" / "application.log").write_text("application failure\n")
        (daq / "run-controller.log").write_bytes(b"old-content\n" + b"n" * (TAIL_BYTES * 2) + b"\nlatest-failure\n")
        (daq / ".env").write_text("secret-test-marker")
        (self.root / "config.json").write_text("not-for-export")
        with self.archive() as archive:
            tail = archive.read("logs/daq/run-controller.log")
            self.assertLessEqual(len(tail), TAIL_BYTES)
            self.assertIn(b"latest-failure", tail)
            self.assertNotIn(b"old-content", tail)
            self.assertNotIn(".env", archive.namelist())
            self.assertNotIn("config.json", archive.namelist())
            self.assertLessEqual(sum(info.file_size for info in archive.infolist() if info.filename.startswith("logs/")), TOTAL_LOG_BYTES)

    def test_archive_omits_symlinks_and_special_files(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        daq = self.root / "logs" / "daq"
        daq.mkdir(parents=True)
        secret = self.root / "private.txt"
        secret.write_text("private-export-canary")
        (daq / "linked-controller.log").symlink_to(secret)
        if hasattr(os, "mkfifo"):
            os.mkfifo(daq / "pipe-controller.log")
        with self.archive() as archive:
            self.assertFalse(any(name.startswith("logs/") for name in archive.namelist()))
        (daq / "linked-controller.log").unlink()
        if (daq / "pipe-controller.log").exists():
            (daq / "pipe-controller.log").unlink()
        daq.rmdir()
        elsewhere = self.root / "private-dir"
        elsewhere.mkdir()
        (elsewhere / "controller.log").write_text("private-export-canary")
        daq.symlink_to(elsewhere, target_is_directory=True)
        with self.archive() as archive:
            self.assertNotIn(b"private-export-canary", b"".join(archive.read(name) for name in archive.namelist()))

    def test_total_size_limit_and_newest_daq_selection(self):
        daq = self.root / "logs" / "daq"
        daq.mkdir(parents=True)
        for index in range(32):
            path = daq / f"run-{index}.log"
            path.write_bytes((f"run-{index}\n".encode() * (TAIL_BYTES // 7 + 1))[:TAIL_BYTES])
            os.utime(path, (1000 + index, 1000 + index))
        with self.archive() as archive:
            logs = [info for info in archive.infolist() if info.filename.startswith("logs/")]
            self.assertLessEqual(len(logs), 24)
            self.assertLessEqual(sum(info.file_size for info in logs), TOTAL_LOG_BYTES)
            self.assertIn("logs/daq/run-31.log", archive.namelist())
            self.assertNotIn("logs/daq/run-0.log", archive.namelist())

    def test_whitelisted_snapshot_omits_positions_notes_symbols_and_payload(self):
        service = DemoService(self.root)
        service.config = service.config.model_copy(update={"station_latitude": 51.234567,
                                                          "station_longitude": 12.345678,
                                                          "tx_notes": "not-for-export"})
        service.source["performance"] = {"processing_ms": 23.4, "nested": {"queue": 2}, "payload": "secret-message"}
        service.emitters["PRIVATECALL"] = {"payload": "secret-message"}
        snapshot = diagnostic_snapshot(service)
        encoded = json.dumps(snapshot)
        for secret in ("51.234567", "12.345678", "not-for-export", "PRIVATECALL", "secret-message"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(snapshot["performance"]["processing_ms"], 23.4)
        self.assertEqual(snapshot["receiver"]["sample_rate_hz"], 2400000)
        if hasattr(service, "diagnostic_log"):
            service.diagnostic_log.close()

    def test_endpoint_downloads_zip_with_receiver_state(self):
        app = create_app(self.root, initial_mode="idle")
        with TestClient(app) as client:
            service = app.state.service
            service.event("test persisted error", "error")
            response = client.get("/api/diagnostics/download?path=/etc/passwd")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "application/zip")
            self.assertIn("KrakenDF-Diagnose.zip", response.headers["content-disposition"])
            self.assertEqual(response.headers["cache-control"], "no-store")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                snapshot = json.loads(archive.read("diagnostics.json"))
                self.assertEqual(snapshot["source"]["mode"], "idle")
                self.assertIn("test persisted error", archive.read("recent-events.json").decode())
                self.assertIn("test persisted error", archive.read("logs/application.log").decode())
                self.assertFalse(any(name.startswith("/") or ".." in name for name in archive.namelist()))

    def test_unexpected_http_failure_is_logged_without_exposing_traceback(self):
        app = create_app(self.root, initial_mode="idle")

        @app.get("/test-broken-request")
        async def broken():
            raise RuntimeError("internal error marker")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/test-broken-request")
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("internal error marker", response.text)
            response = client.get("/api/diagnostics/download")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                log = archive.read("logs/application.log").decode()
                self.assertIn("internal error marker", log)
                self.assertIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
