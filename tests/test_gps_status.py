"""GPS service state, position-anchor and telemetry integration.

A gated reader distinguishes fresh position sentences from supplementary
satellite/status messages. Parser and serial framing are tested separately.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from krakendf.config import AppConfig
from krakendf.diagnostics import diagnostic_snapshot
from krakendf.service import DemoService


class GatedGPSReader:
    def __init__(self, port="/dev/ttyGPS-test", baud=9600):
        self.port, self.baud = port, baud
        self.items = asyncio.Queue()
        self.opened = asyncio.Event()
        self.connect_gate = None
        self.processed = 0
        self.connect_error = None
        self.telemetry = {
            "connected": False, "port": port, "baud": baud,
            "valid": False, "nmea_received": False,
            "bytes_received": 0, "lines_received": 0,
            "valid_sentences": 0, "invalid_sentences": 0,
            "data_age_s": None, "nmea_age_s": None,
            "fix_age_s": None, "age_seconds": None,
            "satellites": None, "satellites_used": None,
            "satellites_in_view": None, "satellites_with_signal": None,
            "satellites_detail": [], "satellite_age_s": None,
        }

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.telemetry["connected"] = True
        self.opened.set()

    async def read_fix(self):
        parsed, changes = await self.items.get()
        if isinstance(parsed, BaseException):
            raise parsed
        self.telemetry.update(changes)
        if parsed is not None:
            self.telemetry.update(nmea_received=True, data_age_s=0,
                                  nmea_age_s=0)
            self.telemetry["valid_sentences"] += 1
            self.telemetry["lines_received"] += 1
            self.telemetry["bytes_received"] += 80
            # GSV/GSA do not carry a new position or determine fix validity.
            if parsed.get("sentence_type") in ("RMC", "GGA"):
                self.telemetry.update(parsed)
                self.telemetry["fix_age_s"] = 0
                self.telemetry["age_seconds"] = 0
        self.processed += 1
        return parsed

    def status(self):
        status = dict(self.telemetry)
        if status["fix_age_s"] is not None and status["fix_age_s"] > 5:
            status["valid"] = False
        return status

    async def close(self):
        self.telemetry["connected"] = False


class GPSStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="krakendf-gps-status-")
        self.service = DemoService(Path(self.temp.name))
        self.service.config = AppConfig(gps_port="/dev/ttyGPS-test", station_mode="gps")
        self.reader = GatedGPSReader()
        self.reader_patch = patch("krakendf.service.GPSReader", return_value=self.reader)
        self.reader_patch.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.reader_patch.stop()
        self.temp.cleanup()

    async def wait_for(self, condition):
        async def poll():
            while not condition():
                await asyncio.sleep(.001)
        await asyncio.wait_for(poll(), 2)

    async def start_reader(self):
        await self.service._start_gps()
        await asyncio.wait_for(self.reader.opened.wait(), 1)

    async def feed(self, parsed, **changes):
        target = self.reader.processed + 1
        self.reader.items.put_nowait((parsed, changes))
        await self.wait_for(lambda: self.reader.processed >= target)
        # Give the consumer the opportunity to merge status/anchor data.
        await asyncio.sleep(0)

    def gps(self):
        return self.service.snapshot()["gps"]

    @staticmethod
    def position(second=0, *, sentence="GGA", utc=None):
        return {"sentence_type": sentence, "utc_time": utc or f"1200{second:02d}",
                "received_at": time.time(), "valid": True,
                "latitude": 52.0, "longitude": 13.0,
                "fix_quality": 1, "satellites_used": 8, "satellites": 8,
                "hdop": 1.1}

    async def test_connection_no_data_invalid_nmea_and_no_fix_are_distinct(self):
        await self.start_reader()
        gps = self.gps()
        self.assertTrue(gps["connected"])
        self.assertEqual(gps["status"], "waiting_data")
        self.assertEqual(gps["bytes_received"], 0)
        self.assertIsNone(gps["satellites_in_view"])

        await self.feed(None, bytes_received=25, lines_received=1,
                        invalid_sentences=1, data_age_s=0)
        gps = self.gps()
        self.assertEqual(gps["status"], "invalid_data")
        self.assertTrue(gps["connected"])
        self.assertFalse(gps["valid"])

        await self.feed({"sentence_type": "GGA", "utc_time": "120000", "valid": False},
                        valid=False, satellites=0, satellites_used=0)
        gps = self.gps()
        self.assertEqual(gps["status"], "no_fix")
        self.assertEqual(gps["satellites_used"], 0)
        self.assertIsNone(gps["satellites_in_view"])

        await self.feed({"sentence_type": "GSV"}, satellites_in_view=5,
                        satellites_with_signal=2, satellite_age_s=0)
        gps = self.gps()
        self.assertEqual(gps["status"], "no_fix")
        self.assertEqual(gps["satellites_in_view"], 5)
        self.assertEqual(gps["satellites_with_signal"], 2)
        self.assertEqual(gps["satellites_used"], 0)
        self.assertFalse(gps["station_valid"])

    async def test_connecting_port_is_distinct_from_disconnected(self):
        self.reader.connect_gate = asyncio.Event()
        await self.service._start_gps()
        await asyncio.sleep(0)
        gps = self.gps()
        self.assertEqual(gps["status"], "connecting")
        self.assertFalse(gps["connected"])
        self.reader.connect_gate.set()
        await asyncio.wait_for(self.reader.opened.wait(), 1)
        self.assertEqual(self.gps()["status"], "waiting_data")

    async def test_selecting_gps_after_manual_position_starts_auto_discovery(self):
        self.service.config = AppConfig(gps_port="", station_mode="manual")
        candidate = SimpleNamespace(device="/dev/ttyGPS-test", vid=0x1546,
                                    description="u-blox GPS", manufacturer="u-blox",
                                    product="VK-162")
        serial = ModuleType("serial")
        serial.__path__ = []
        tools = ModuleType("serial.tools")
        tools.__path__ = []
        listing = ModuleType("serial.tools.list_ports")
        listing.comports = lambda: [candidate]
        serial.tools, tools.list_ports = tools, listing
        with patch.dict(sys.modules, {"serial": serial, "serial.tools": tools,
                                     "serial.tools.list_ports": listing}):
            await self.service.update_config({"station_mode": "gps"})
            await asyncio.wait_for(self.reader.opened.wait(), 1)
        gps = self.gps()
        self.assertEqual(self.service.config.gps_port, "/dev/ttyGPS-test")
        self.assertTrue(gps["connected"])
        self.assertTrue(gps["detected"])
        self.assertEqual(gps["status"], "waiting_data")

    async def test_satellite_messages_and_duplicate_fix_epoch_do_not_fill_anchor(self):
        await self.start_reader()
        await self.feed(self.position())
        self.assertEqual(self.gps()["anchor_samples"], 1)

        for kind in ("GSV", "GSA", "GSV"):
            await self.feed({"sentence_type": kind})
        await self.feed(None)  # serial timeout or an ignored line
        await self.feed(self.position(sentence="RMC", utc="120000.00"))
        self.assertEqual(self.gps()["anchor_samples"], 1)
        self.assertFalse(self.gps()["anchor_ready"])

        for second in range(1, 10):
            await self.feed(self.position(second))
        gps = self.gps()
        self.assertEqual(gps["anchor_samples"], 10)
        self.assertEqual(gps["anchor_target"], 10)
        self.assertTrue(gps["anchor_ready"])
        self.assertTrue(gps["station_valid"])
        latitude, longitude, valid, source = self.service.station()
        self.assertAlmostEqual(latitude, 52.0)
        self.assertAlmostEqual(longitude, 13.0)
        self.assertTrue(valid)
        self.assertEqual(source, "gps_locked")

        await self.service._stop_gps()
        gps = self.gps()
        self.assertFalse(gps["connected"])
        self.assertEqual(gps["status"], "disconnected")
        self.assertTrue(gps["anchor_ready"])
        self.assertTrue(gps["station_valid"], "Stationary position anchor is independent of current USB connection")

    async def test_snapshot_refreshes_reader_ages_without_another_serial_sentence(self):
        await self.start_reader()
        await self.feed(self.position())
        self.assertEqual(self.gps()["status"], "fix")
        self.reader.telemetry.update(data_age_s=12, nmea_age_s=12,
                                     fix_age_s=12, age_seconds=12)
        gps = self.gps()
        self.assertEqual(gps["status"], "stale")
        self.assertFalse(gps["valid"])
        self.assertTrue(gps["connected"], "Open serial port does not prove fresh receiver data")

        self.reader.telemetry.update(data_age_s=0, nmea_age_s=12)
        gps = self.gps()
        self.assertEqual(gps["status"], "invalid_data")

    async def test_serial_error_is_not_disguised_as_waiting_for_fix(self):
        self.reader.connect_error = OSError("GPS serial port unavailable")
        await self.service._start_gps()
        await asyncio.wait_for(self.service._gps_task, 1)
        gps = self.gps()
        self.assertEqual(gps["status"], "error")
        self.assertFalse(gps["connected"])
        self.assertFalse(gps["valid"])
        self.assertIn("GPS serial port unavailable", gps["message"])
        self.assertEqual(gps["anchor_samples"], 0)

        self.reader.connect_error = None
        await self.start_reader()
        await self.feed(self.position())
        self.assertTrue(self.gps()["valid"])
        self.reader.items.put_nowait((OSError("GPS unplugged during reception"), {}))
        await asyncio.wait_for(self.service._gps_task, 1)
        gps = self.gps()
        self.assertEqual(gps["status"], "error")
        self.assertFalse(gps["connected"])
        self.assertFalse(gps["valid"], "A previously valid position must not make a disconnected mouse look healthy")
        self.assertIn("GPS unplugged", gps["message"])

    async def test_diagnostic_snapshot_retains_gps_state_without_coordinates(self):
        await self.start_reader()
        await self.feed(self.position())
        await self.feed({"sentence_type": "GSV"}, satellites_in_view=11,
                        satellites_with_signal=7, satellite_age_s=0)
        diagnostic = diagnostic_snapshot(self.service)["gps"]
        self.assertTrue(diagnostic["connected"])
        self.assertEqual(diagnostic["status"], "fix")
        self.assertEqual(diagnostic["satellites_used"], 8)
        self.assertEqual(diagnostic["satellites_in_view"], 11)
        self.assertEqual(diagnostic["satellites_with_signal"], 7)
        self.assertNotIn("latitude", diagnostic)
        self.assertNotIn("longitude", diagnostic)
        self.assertNotIn("utc_date", diagnostic)
        self.assertNotIn("raw_sentence", diagnostic)


if __name__ == "__main__":
    unittest.main()
