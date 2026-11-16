"""Receiver regression tests; synthetic roundtrips are not HF validation."""

from pathlib import Path
import unittest

import numpy as np

from krakendf.lora_phy import (
    UnsupportedLoRaError, _crc16, _whitening, decode_lora_iq,
    detect_lora_preamble, lora_capability, synthesize_lora,
)


class LoRaReceiverTests(unittest.TestCase):
    def test_protocol_constants_have_independent_known_values(self):
        self.assertEqual(_crc16(b"123456789"), 0x31C3)
        self.assertEqual(_whitening(8).tolist(), [255, 254, 252, 248, 240, 225, 194, 133])

    def test_bft_position_payload_and_original_sample_clock(self):
        payload = b"DL7CR 49.712340 11.045670 312m 184205Z 07sat"
        # 300 kHz is a realistic rational-rate channelizer output for BW125k.
        signal = synthesize_lora(payload, sample_rate_hz=300000)
        prefix = np.zeros(1291, dtype=np.complex64)
        suffix = np.zeros(2345, dtype=np.complex64)
        packets = decode_lora_iq(np.concatenate([prefix, signal, suffix]), 300000)
        self.assertEqual(len(packets), 1)
        packet = packets[0]
        self.assertEqual(packet["payload"], payload)
        self.assertTrue(packet["quality"]["crc_ok"])
        self.assertTrue(packet["quality"]["header_ok"])
        self.assertLessEqual(abs(packet["sample_start"] - len(prefix)), 4)
        self.assertLessEqual(abs(packet["sample_end"] - len(prefix) - len(signal)), 4)

    def test_carrier_offsets_preserve_crc_and_association(self):
        payload = b"DL7CR TESTmessageLoRaTST"
        signal = synthesize_lora(payload, sample_rate_hz=300000)
        for cfo in (-1500, -200, 200, 1500):
            with self.subTest(cfo=cfo):
                shifted = signal * np.exp(2j * np.pi * cfo * np.arange(len(signal)) / 300000)
                packets = decode_lora_iq(np.concatenate([np.zeros(337), shifted, np.zeros(2048)]), 300000)
                self.assertEqual(len(packets), 1)
                self.assertEqual(packets[0]["payload"], payload)
                self.assertLess(abs(packets[0]["quality"]["cfo_hz"] - cfo), 5)

    def test_corrupted_payload_crc_is_never_emitted(self):
        signal = synthesize_lora(b"DL7CR TESTmessageLoRaTST", corrupt_crc=True)
        self.assertEqual(decode_lora_iq(signal, 250000), [])

    def test_complete_data_required(self):
        signal = synthesize_lora(b"DL7CR TESTmessageLoRaTST")
        # Preamble-only or truncated messages cannot supply a valid CRC.
        self.assertEqual(decode_lora_iq(signal[:10 * 1024], 250000), [])
        self.assertEqual(decode_lora_iq(signal[:-3 * 1024], 250000), [])
        self.assertIsNotNone(detect_lora_preamble(signal[:8 * 1024], 250000))

    def test_multiple_complete_packets_keep_separate_sample_intervals(self):
        first = synthesize_lora(b"DL7CR FIRST")
        second = synthesize_lora(b"DL7CR SECOND")
        gap = np.zeros(19013, dtype=np.complex64)
        packets = decode_lora_iq(np.concatenate([first, gap, second, gap[:2048]]), 250000)
        self.assertEqual([p["payload"] for p in packets], [b"DL7CR FIRST", b"DL7CR SECOND"])
        self.assertLess(packets[0]["sample_end"], packets[1]["sample_start"])

    def test_below_noise_preamble_trigger_and_crc_decode(self):
        # A reproducible algorithm test, not a specified receiver sensitivity.
        signal = synthesize_lora(b"DL7CR TESTmessageLoRaTST", sample_rate_hz=300000)
        iq = np.concatenate([np.zeros(1291), signal, np.zeros(3011)])
        rng = np.random.default_rng(109)
        noise = (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq))) * np.sqrt(10 / 2)
        iq = iq + noise  # -10 dB average signal-to-noise power in the burst.
        self.assertIsNotNone(detect_lora_preamble(iq, 300000))
        packets = decode_lora_iq(iq, 300000)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["payload"], b"DL7CR TESTmessageLoRaTST")

    def test_noise_and_cw_do_not_become_messages(self):
        rng = np.random.default_rng(308)
        noise = rng.standard_normal(60000) + 1j * rng.standard_normal(60000)
        self.assertEqual(decode_lora_iq(noise, 250000), [])
        self.assertIsNone(detect_lora_preamble(noise, 250000))
        carrier = np.exp(2j * np.pi * 5000 * np.arange(60000) / 250000)
        self.assertEqual(decode_lora_iq(carrier, 250000), [])

    def test_all_supported_fec_and_low_data_rate_modes(self):
        for sf, cr in ((7, 1), (8, 2), (9, 3), (10, 4), (11, 1), (12, 1)):
            with self.subTest(sf=sf, cr=cr):
                signal = synthesize_lora(b"CHECK", sf=sf, coding_rate=cr)
                packets = decode_lora_iq(signal, 250000, sf=sf)
                self.assertEqual([p["payload"] for p in packets], [b"CHECK"])

    def test_invalid_formats_and_capability_are_explicit(self):
        with self.assertRaises(UnsupportedLoRaError):
            decode_lora_iq(np.zeros(100), 250000)
        with self.assertRaises(UnsupportedLoRaError):
            decode_lora_iq(np.zeros((5, 100), dtype=np.complex64), 250000)
        with self.assertRaises(UnsupportedLoRaError):
            decode_lora_iq(np.zeros(100, dtype=np.complex64), 100000)
        self.assertEqual(decode_lora_iq(np.array([complex(float("nan"), 0)]), 250000), [])
        self.assertEqual(lora_capability()["status"], "experimental")
        self.assertFalse(lora_capability()["hardware_validated"])

    def test_external_radio_capture_if_available(self):
        # Independent on-air fixture held by the upstream reference checkout;
        # deliberately not bundled or claimed to be 433-MHz Kraken data.
        fixture = Path(__file__).resolve().parents[2] / "references/SimpleSDR/data/lora_capture.npy"
        if not fixture.exists():
            self.skipTest("Optional external Pluto868 reference capture absent")
        capture = np.load(fixture, mmap_mode="r")
        self.assertEqual(capture.shape, (40_000_000,))
        sample_rate = 2_500_000
        offset = int(11.95 * sample_rate)
        region = np.asarray(capture[offset:int(12.5 * sample_rate)])
        packets = decode_lora_iq(region, sample_rate, sf=9, bandwidth_hz=125000)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0]["payload"], b"DL7CR TESTmessageLoRaTST")
        self.assertTrue(packets[0]["quality"]["crc_ok"])
        # Exact interval of the decoded frame, not decode completion time.
        absolute_start = (offset + packets[0]["sample_start"]) / sample_rate
        self.assertTrue(12.09 < absolute_start < 12.12)


if __name__ == "__main__":
    unittest.main()
