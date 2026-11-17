"""Serial data, position validity and satellite telemetry are separate states."""
import unittest
from unittest.mock import patch

from krakendf.hardware import GPSReader, parse_nmea


def nmea(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}\r\n"


class FakeSerial:
    def __init__(self):
        self.lines = []
        self.is_open = True

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        self.is_open = False


def gsv(talker, parts, part, total, rows, signal=None):
    fields = [f"{talker}GSV", str(parts), str(part), str(total)]
    for prn, elevation, azimuth, cn0 in rows:
        fields.extend([str(prn), str(elevation), str(azimuth), str(cn0)])
    if signal is not None:
        fields.append(str(signal))
    return ",".join(fields)


def gsa(fix_type=3, prns=(1, 2, 3), talker="GP"):
    return ",".join([f"{talker}GSA", "A", str(fix_type)]
                    + [str(value) for value in prns] + [""] * (12 - len(prns))
                    + ["1.3", "0.8", "1.0"])


class GPSTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader = GPSReader("/dev/test-gps", 9600)
        self.serial = FakeSerial()
        self.reader._serial = self.serial

    async def asyncTearDown(self):
        await self.reader.close()

    async def feed(self, body, now=100.0):
        self.serial.lines.append(nmea(body).encode())
        with patch("krakendf.hardware.time.time", return_value=now):
            return await self.reader.read_fix()

    def status(self, now=100.0):
        with patch("krakendf.hardware.time.time", return_value=now):
            return self.reader.status()

    async def test_open_port_no_data_is_not_satellite_zero_or_fix(self):
        with patch("serial.Serial", return_value=self.serial) as serial_open, \
                patch("krakendf.hardware.time.time", return_value=90):
            await self.reader.connect()
        serial_open.assert_called_once_with("/dev/test-gps", 9600, timeout=1)
        result = self.status()
        self.assertTrue(result["connected"])
        self.assertEqual(result["connected_at"], 90)
        self.assertEqual(result["bytes_received"], 0)
        self.assertIsNone(result["data_age_s"])
        self.assertIsNone(result["satellites_in_view"])
        self.assertIsNone(result["satellites_used"])
        self.assertFalse(result["valid"])

    async def test_no_fix_can_have_satellites_and_signal(self):
        await self.feed("GPGGA,123519,,,,,0,00,99.9,,,,,,")
        await self.feed(gsv("GP", 2, 1, 5,
                            [(1, 20, 100, 32), (2, 40, 200, ""), (3, 60, 300, 0), (4, 80, 20, 41)]))
        partial = self.status()
        self.assertIsNone(partial["satellites_in_view"])
        await self.feed(gsv("GP", 2, 2, 5, [(5, 45, 220, 23)]))
        result = self.status()
        self.assertFalse(result["valid"])
        self.assertEqual(result["fix_quality"], 0)
        self.assertEqual(result["satellites_used"], 0)
        self.assertEqual(result["satellites_in_view"], 5)
        self.assertEqual(result["satellites_with_signal"], 3)
        self.assertEqual(len(result["satellites_detail"]), 5)

    async def test_gsv_new_cycle_replaces_pending_without_mixing_rows(self):
        rows = [(number, 20, 120, 20) for number in range(1, 5)]
        await self.feed(gsv("GP", 2, 1, 5, rows))
        await self.feed(gsv("GP", 2, 1, 5, [(n, 30, 180, 30) for n in range(11, 15)]))
        await self.feed(gsv("GP", 2, 2, 5, [(15, 30, 180, 30)]))
        self.assertEqual({row["prn"] for row in self.status()["satellites_detail"]},
                         {str(n) for n in range(11, 16)})
        # An incomplete subsequent cycle does not replace this complete one.
        await self.feed(gsv("GP", 2, 1, 5, rows), now=101)
        self.assertEqual(self.status(101)["satellites_detail"][0]["prn"], "11")

    async def test_out_of_order_or_inconsistent_cycle_is_not_published(self):
        await self.feed(gsv("GP", 2, 2, 5, [(5, 30, 100, 35)]))
        self.assertIsNone(self.status()["satellites_in_view"])
        await self.feed(gsv("GP", 2, 1, 5, [(1, 30, 100, 35)]))
        await self.feed(gsv("GP", 2, 2, 6, [(5, 30, 100, 35)]))
        self.assertIsNone(self.status()["satellites_in_view"])

    async def test_signal_ids_deduplicate_but_constellations_remain_distinct(self):
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)], signal=1))
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 39)], signal=5))
        await self.feed(gsv("GA", 1, 1, 1, [(1, 30, 100, 21)], signal=1))
        result = self.status()
        self.assertEqual(result["satellites_in_view"], 2)
        self.assertEqual(result["satellites_with_signal"], 2)
        self.assertEqual(len(result["satellites_detail"]), 2)
        self.assertEqual(next(row for row in result["satellites_detail"] if row["talker"] == "GP")["cn0_dbhz"], 39)

    async def test_combined_gn_reporting_not_added_to_overlapping_gp_gl(self):
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]))
        await self.feed(gsv("GL", 1, 1, 1, [(65, 30, 100, 24)]))
        await self.feed(gsv("GN", 1, 1, 3, [(1, 30, 100, 24), (65, 30, 100, 24), (301, 30, 100, "")]))
        result = self.status()
        self.assertEqual(result["satellites_in_view"], 3)
        self.assertEqual(result["satellites_with_signal"], 2)
        self.assertEqual({row["talker"] for row in result["satellites_detail"]}, {"GN"})

    async def test_explicit_zero_is_different_from_unreported(self):
        self.assertIsNone(self.status()["satellites_in_view"])
        await self.feed("GPGSV,1,1,00")
        self.assertEqual(self.status()["satellites_in_view"], 0)
        self.assertEqual(self.status()["satellites_with_signal"], 0)

    async def test_telemetry_does_not_refresh_or_create_position_fix(self):
        await self.feed(gsa())
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]))
        self.assertFalse(self.status()["valid"])
        self.assertIsNone(self.status()["last_fix_at"])
        await self.feed("GPRMC,123519,A,4807.038,N,01131.000,E,0.2,91.0,171126,,,A")
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]), now=106)
        result = self.status(106)
        self.assertFalse(result["valid"])
        self.assertEqual(result["received_at"], 100)
        self.assertEqual(result["utc_time"], "123519")
        self.assertEqual(result["fix_age_s"], 6)
        self.assertEqual(result["nmea_age_s"], 0)

    async def test_explicit_gsa_and_rmc_fix_loss_do_not_erase_history(self):
        valid = "GPRMC,123519,A,4807.038,N,01131.000,E,0.2,91.0,171126,,,A"
        await self.feed(valid)
        await self.feed(gsa(fix_type=1, prns=()), now=101)
        result = self.status(101)
        self.assertFalse(result["valid"])
        self.assertEqual(result["last_fix_at"], 100)
        self.assertEqual(result["received_at"], 100)
        await self.feed(gsa(fix_type=3), now=102)
        self.assertFalse(self.status(102)["valid"])
        await self.feed(valid, now=103)
        self.assertTrue(self.status(103)["valid"])
        await self.feed("GPRMC,123520,V,,,,,,,171126,,,N", now=104)
        self.assertFalse(self.status(104)["valid"])
        self.assertEqual(self.status(104)["last_fix_at"], 103)

    async def test_used_satellites_and_gsv_expire_independently(self):
        await self.feed(gsa(prns=(1, 2, 3)))
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]))
        self.assertEqual(self.status()["satellites_used"], 3)
        self.assertTrue(self.status()["satellites_detail"][0]["used"])
        await self.feed("GPRMC,123519,A,4807.038,N,01131.000,E,0.2,91.0,171126,,,A", now=111)
        result = self.status(111)
        self.assertTrue(result["valid"])
        self.assertIsNone(result["satellites_used"])
        self.assertIsNone(result["satellites_in_view"])
        self.assertIsNone(result["satellites_with_signal"])
        self.assertEqual(result["satellites_detail"], [])
        self.assertEqual(result["satellite_age_s"], 11)

    async def test_constellation_specific_no_fix_does_not_destroy_combined_fix(self):
        await self.feed("GNRMC,123519,A,4807.038,N,01131.000,E,0.2,91.0,171126,,,A")
        await self.feed(gsa(fix_type=1, prns=(), talker="GP"), now=101)
        await self.feed(gsa(fix_type=3, prns=(65, 66, 67), talker="GL"), now=101)
        result = self.status(101)
        self.assertTrue(result["valid"])
        self.assertEqual(result["fix_type"], 3)
        await self.feed(gsa(fix_type=1, prns=(), talker="GN") + ",1", now=102)
        self.assertTrue(self.status(102)["valid"])
        self.assertEqual(self.status(102)["satellites_used"], 3)
        await self.feed(gsa(fix_type=1, prns=(), talker="GN"), now=103)
        self.assertFalse(self.status(103)["valid"])

    async def test_gsa_system_id_does_not_mark_wrong_constellation_used(self):
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]))
        await self.feed(gsv("GA", 1, 1, 1, [(1, 30, 100, 24)]))
        await self.feed(gsa(prns=(1,), talker="GN") + ",3")
        result = self.status()
        by_talker = {row["talker"]: row for row in result["satellites_detail"]}
        self.assertIsNone(by_talker["GP"]["used"])
        self.assertTrue(by_talker["GA"]["used"])
        self.assertEqual(result["satellites_used"], 1)
        await self.feed(gsa(prns=(1,), talker="GP"))
        await self.feed(gsa(prns=(1,), talker="GN") + ",1")
        self.assertEqual(self.status()["satellites_used"], 2)

    async def test_empty_reads_bad_checksum_and_other_valid_nmea_are_distinct(self):
        with patch("krakendf.hardware.time.time", return_value=100):
            await self.reader.read_fix()
        self.assertEqual(self.status()["bytes_received"], 0)
        self.serial.lines.append(b"$GPGGA,bad*00\r\n")
        with patch("krakendf.hardware.time.time", return_value=101):
            await self.reader.read_fix()
        result = self.status(101)
        self.assertGreater(result["bytes_received"], 0)
        self.assertEqual(result["invalid_sentences"], 1)
        self.assertEqual(result["nmea_received"], 0)
        await self.feed("GPVTG,,T,,M,0.0,N,0.0,K,N", now=102)
        before = self.status(102)
        self.assertEqual(before["nmea_received"], 1)
        self.assertIn("VTG", before["sentence_types"])
        with patch("krakendf.hardware.time.time", return_value=103):
            await self.reader.read_fix()
        after = self.status(103)
        self.assertEqual(after["bytes_received"], before["bytes_received"])
        self.assertEqual(after["last_data_at"], 102)
        self.assertFalse(after["valid"])

    async def test_disconnect_invalidates_live_state_without_erasing_counters(self):
        await self.feed("GPRMC,123519,A,4807.038,N,01131.000,E,0.2,91.0,171126,,,A")
        await self.feed(gsv("GP", 1, 1, 1, [(1, 30, 100, 24)]))
        await self.reader.close()
        result = self.status()
        self.assertFalse(result["connected"])
        self.assertFalse(result["valid"])
        self.assertGreater(result["bytes_received"], 0)
        self.assertIsNone(result["satellites_in_view"])

    async def test_malformed_satellite_numbers_checksum_and_fix_fields_rejected(self):
        bodies = [gsv("GP", 1, 1, 1, [(1, 91, 100, 24)]),
                  gsv("GP", 1, 1, 1, [(1, 30, 361, 24)]),
                  gsv("GP", 1, 1, 1, [(1, 30, 100, "nan")]),
                  "GPGSV,1,2,1,1,30,100,24", "GPGSV,1,1,1,1,30,100",
                  gsa(fix_type=4), "GPGGA,123519,,,,,0,-1,99.9,,,,,,"]
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(parse_nmea(nmea(body)))


if __name__ == "__main__":
    unittest.main()
