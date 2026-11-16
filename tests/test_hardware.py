"""Wire-level and NMEA checks; no physical receiver is exercised."""
import asyncio
import struct
import unittest

import numpy as np

from krakendf.hardware import HardwareError, HeimdallClient, parse_header, parse_nmea


def frame_bytes(index=0, frame_type=0, samples=32, **changes):
    values = {
        "sync": 0x2BF7B95A, "version": 7, "channels": 5,
        "center": 433920000, "rate": 2400000, "bits": 32,
        "dtype": 3, "delay": 1, "iq": 1, "overdrive": 0,
        "timestamp": 1700000000000 + index * 10, "noise": 0,
    }
    values.update(changes)
    header = bytearray(1024)
    for offset, value in [(0, values["sync"]), (4, frame_type), (28, values["channels"]),
                          (64, samples), (80, index), (84, index), (96, values["dtype"]),
                          (100, values["bits"]), (104, values["overdrive"]),
                          (236, values["delay"]), (240, values["iq"]),
                          (248, values["noise"]), (1020, values["version"])]:
        struct.pack_into("<I", header, offset, value)
    for offset, value in [(40, values["center"]), (48, values["rate"]),
                          (56, values["rate"]), (72, values["timestamp"])]:
        struct.pack_into("<Q", header, offset, value)
    for ch in range(5):
        struct.pack_into("<I", header, 108 + 4 * ch, 207)
    # Each antenna is distinct, making a wrong reshape/channel order visible.
    iq = np.stack([np.full(samples, ch + 1j * (ch + 1), dtype="<c8") for ch in range(5)])
    return bytes(header) + iq.tobytes()


def nmea(body):
    check = 0
    for char in body:
        check ^= ord(char)
    return f"${body}*{check:02X}"


class HeaderTests(unittest.TestCase):
    def test_v7_layout_and_gain(self):
        h = parse_header(frame_bytes()[:1024])
        self.assertEqual(h["sample_count"], 32)
        self.assertEqual(h["payload_bytes"], 5 * 32 * 8)
        self.assertEqual(h["gains_db"], [20.7] * 5)
        self.assertEqual(h["sample_rate_hz"], 2400000)
        self.assertEqual(h["timestamp_ms"], 1700000000000)

    def test_reject_incompatible_or_oversized(self):
        for changes in [{"sync": 0}, {"version": 999}, {"channels": 4},
                        {"bits": 8}, {"dtype": 2}, {"rate": 0},
                        {"center": 0}]:
            with self.subTest(changes=changes), self.assertRaises(HardwareError):
                parse_header(frame_bytes(**changes)[:1024])
        h = bytearray(frame_bytes()[:1024])
        struct.pack_into("<I", h, 64, 0xFFFFFFFF)
        with self.assertRaises(HardwareError):
            parse_header(h)
        with self.assertRaises(HardwareError):
            parse_header(b"short")

    def test_dummy_payload_invariant(self):
        h = parse_header(frame_bytes(frame_type=1, samples=0, dtype=0)[:1024])
        self.assertEqual(h["payload_bytes"], 0)
        with self.assertRaises(HardwareError):
            parse_header(frame_bytes(frame_type=1)[:1024])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.servers = []

    async def asyncTearDown(self):
        for writer in self.connections:
            writer.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        await asyncio.sleep(0)

    async def server(self, packets, fragment=True):
        commands = []
        async def handler(reader, writer):
            self.connections.append(writer)
            try:
                commands.append(await reader.readexactly(9))
                for packet in packets:
                    step = 77 if fragment else len(packet)
                    for start in range(0, len(packet), step):
                        writer.write(packet[start:start + step])
                        await writer.drain()
                        await asyncio.sleep(0)
                    commands.append(await reader.readexactly(10))
            except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                writer.close()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1], commands

    async def test_fragmented_payload_and_contiguous_samples(self):
        port, commands = await self.server([frame_bytes(10), frame_bytes(11)])
        client = HeimdallClient(port=port, timeout=1)
        await client.connect()
        first, second = await client.read_frame(), await client.read_frame()
        self.assertTrue(first.quality["valid"])
        self.assertEqual(first.sample_start, 0)
        self.assertEqual(second.sample_start, 32)
        self.assertEqual(first.stream_epoch, second.stream_epoch)
        self.assertEqual(first.iq.dtype, np.dtype("complex64"))
        np.testing.assert_array_equal(first.iq[:, 0], np.arange(5) + 1j * np.arange(1, 6))
        self.assertEqual(commands[0], b"streaming")
        self.assertEqual(commands[1], b"IQDownload")
        await client.close()

    async def test_timestamp_converts_frame_end_to_estimated_first_sample(self):
        port, _ = await self.server([frame_bytes(0, samples=2400)], fragment=False)
        client = HeimdallClient(port=port, timeout=1)
        await client.connect()
        frame = await client.read_frame()
        self.assertAlmostEqual(frame.timestamp, 1700000000.0 - .001, places=6)
        self.assertEqual(frame.quality["timestamp_ms"], 1700000000000)
        self.assertEqual(frame.quality["timestamp_semantics"], "estimated_first_sample_from_frame_end")
        await client.close()

    async def test_calibration_payload_consumed_and_new_epochs(self):
        packets = [frame_bytes(0), frame_bytes(1, frame_type=3, samples=64, noise=1),
                   frame_bytes(2, frame_type=1, samples=0), frame_bytes(3)]
        port, _ = await self.server(packets)
        client = HeimdallClient(port=port, timeout=1)
        await client.connect()
        first, cal, dummy, recovered = [await client.read_frame() for _ in range(4)]
        self.assertFalse(cal.quality["valid"])
        self.assertIn("calibration_frame", cal.quality["reasons"])
        self.assertEqual(cal.iq.shape, (5, 64))
        self.assertEqual(dummy.iq.shape, (5, 0))
        self.assertTrue(recovered.quality["valid"])
        self.assertEqual(recovered.sample_start, 0)
        self.assertNotEqual(first.stream_epoch, recovered.stream_epoch)
        self.assertIn("recovered_after_invalid_frame", recovered.quality["discontinuity_reasons"])
        await client.close()

    async def test_gap_retune_overdrive_and_lock(self):
        packets = [frame_bytes(0), frame_bytes(2), frame_bytes(3, center=434000000),
                   frame_bytes(4, delay=0, iq=0, overdrive=4)]
        port, _ = await self.server(packets)
        client = HeimdallClient(port=port, timeout=1)
        await client.connect()
        frames = [await client.read_frame() for _ in range(4)]
        self.assertIn("cpi_index_gap", frames[1].quality["discontinuity_reasons"])
        self.assertIn("frequency_changed", frames[2].quality["discontinuity_reasons"])
        self.assertFalse(frames[3].quality["valid"])
        self.assertIn("adc_overdrive", frames[3].quality["reasons"])
        self.assertIn("delay_not_locked", frames[3].quality["reasons"])
        self.assertEqual(len(set(f.stream_epoch for f in frames)), 4)
        await client.close()

    async def test_truncated_frame_closes_transport(self):
        port, _ = await self.server([frame_bytes()[:1050]], fragment=False)
        client = HeimdallClient(port=port, timeout=0.1)
        await client.connect()
        with self.assertRaises(HardwareError):
            await client.read_frame()
        self.assertFalse(client.connected)

    async def test_control_wire_packets_and_acknowledgement(self):
        packets = []
        async def handler(reader, writer):
            self.connections.append(writer)
            try:
                for _ in range(2):
                    packets.append(await reader.readexactly(128))
                    # Exercise a fragmented control reply too.
                    writer.write(b"FN")
                    await writer.drain()
                    await asyncio.sleep(0)
                    writer.write(b"SD" + bytes(124))
                    await writer.drain()
            finally:
                writer.close()
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        port = server.sockets[0].getsockname()[1]
        client = HeimdallClient(control_port=port, timeout=1)
        result = await client.configure(center_frequency_hz=434000000, gain_db=20.7)
        self.assertTrue(result["acknowledged"])
        self.assertTrue(result["requires_frame_verification"])
        self.assertEqual(packets[0][:4], b"FREQ")
        self.assertEqual(struct.unpack_from("<Q", packets[0], 4)[0], 434000000)
        self.assertEqual(struct.unpack_from("<5I", packets[1], 4), (207,) * 5)
        for kw in [{"gain_db": -1}, {"gain_db": float("nan")}, {"gain_db": 20.0},
                   {"center_frequency_hz": 10}]:
            with self.assertRaises(ValueError):
                await client.configure(**kw)


class NMEATests(unittest.TestCase):
    def test_known_gga(self):
        result = parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47")
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["latitude"], 48.1173)
        self.assertAlmostEqual(result["longitude"], 11.516666666666667)
        self.assertEqual(result["satellites"], 8)
        self.assertEqual(result["hdop"], 0.9)

    def test_rmc_and_southern_coordinates(self):
        result = parse_nmea(nmea("GNRMC,184205.00,A,3345.1234,S,07040.5678,W,0.2,91.0,121126,,,A"))
        self.assertTrue(result["valid"])
        self.assertLess(result["latitude"], 0)
        self.assertLess(result["longitude"], 0)
        self.assertEqual(result["utc_date"], "121126")
        self.assertEqual(result["course_deg"], 91.0)

    def test_invalid_checksum_missing_fix_and_bad_coordinate(self):
        self.assertIsNone(parse_nmea("$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00"))
        invalid = parse_nmea(nmea("GNRMC,123519,V,,,,,,,121126,,,N"))
        self.assertFalse(invalid["valid"])
        self.assertIsNone(parse_nmea(nmea("GPGGA,123519,4899.000,N,01131.000,E,1,08,0.9,545.4,M,,,")))
        self.assertIsNone(parse_nmea(nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,nan,545.4,M,,,")))
        self.assertIsNone(parse_nmea("unframed input"))


if __name__ == "__main__":
    unittest.main()
