"""SNR is a noise-subtracted power ratio, separate from the DF eigen score."""
import unittest

import numpy as np

from krakendf.snr import estimate_packet_snr


class PacketSNRTests(unittest.TestCase):
    def capture(self, snr_db):
        rng = np.random.default_rng(901)
        n = 120000
        x = (rng.standard_normal((5, n)) + 1j * rng.standard_normal((5, n))) / np.sqrt(2)
        phases = np.exp(1j * np.arange(5) * 1.5)
        tone = np.exp(2j * np.pi * .103 * np.arange(60000))
        x[:, 30000:90000] += 10 ** (snr_db / 20) * phases[:, None] * tone
        return x.astype(np.complex64)

    def test_recovers_known_power_ratios_including_negative_db(self):
        for expected in (20, 5, 0, -5, -10):
            with self.subTest(expected=expected):
                estimate = estimate_packet_snr(self.capture(expected), 30000, 90000,
                                               sample_rate_hz=100000)
                self.assertEqual(estimate["snr_status"], "estimated")
                self.assertAlmostEqual(estimate["snr_db"], expected, delta=.5)
                self.assertEqual(estimate["snr_noise_source"], "pretrigger+posttrigger")
                self.assertAlmostEqual(estimate["snr_noise_power"], 1.0, delta=.02)

    def test_noise_window_excludes_preamble_outside_decoder_boundaries(self):
        x = self.capture(10)
        result = estimate_packet_snr(x, 40000, 80000, sample_rate_hz=100000,
                                    capture_quality={"signal_start_offset": 30000,
                                                     "signal_end_offset": 90000})
        self.assertAlmostEqual(result["snr_db"], 10, delta=.3)
        self.assertLess(result["snr_noise_samples"], 60000)

    def test_legacy_detector_power_does_not_certify_quiet_reference(self):
        x = self.capture(0)[:, 30000:90000]
        result = estimate_packet_snr(x, 0, x.shape[1], sample_rate_hz=100000,
                                    capture_quality={"noise_power": 1})
        self.assertIsNone(result["snr_db"])
        self.assertEqual(result["snr_status"], "no_noise_reference")

    def test_explicit_quiet_capture_baseline_is_supported(self):
        x = self.capture(-5)[:, 30000:90000]
        result = estimate_packet_snr(x, 0, x.shape[1], sample_rate_hz=100000,
                                    capture_quality={"noise_power": 1,
                                                     "noise_reference_valid": True})
        self.assertEqual(result["snr_noise_source"], "capture_baseline")
        self.assertAlmostEqual(result["snr_db"], -5, delta=.3)

    def test_changed_noise_reference_is_not_turned_into_snr(self):
        x = self.capture(5)
        x[:, 90000:] *= 4
        result = estimate_packet_snr(x, 30000, 90000, sample_rate_hz=100000)
        self.assertIsNone(result["snr_db"])
        self.assertEqual(result["snr_status"], "unstable_noise_reference")

    def test_burst_contamination_of_idle_region_is_rejected(self):
        x = self.capture(5)
        x[:, 10000:20000] += 10
        result = estimate_packet_snr(x, 30000, 90000, sample_rate_hz=100000)
        self.assertIsNone(result["snr_db"])
        self.assertEqual(result["snr_status"], "unstable_noise_reference")

    def test_nonpositive_noise_subtracted_power_has_no_fabricated_value(self):
        x = np.ones((5, 10000), dtype=np.complex64)
        result = estimate_packet_snr(x, 2000, 8000, sample_rate_hz=100000)
        self.assertIsNone(result["snr_db"])
        self.assertEqual(result["snr_status"], "below_noise_resolution")

    def test_zero_noise_invalid_iq_and_bounds(self):
        x = np.zeros((5, 10000), dtype=np.complex64)
        self.assertEqual(estimate_packet_snr(x, 2000, 8000, sample_rate_hz=100000)["snr_status"],
                         "invalid_noise_reference")
        for bad in (np.zeros((4, 10000), dtype=np.complex64), np.full_like(x, np.nan)):
            self.assertEqual(estimate_packet_snr(bad, 2000, 8000, sample_rate_hz=100000)["snr_status"],
                             "invalid_iq")
        for left, right, rate in ((-1, 8000, 100000), (2000, 10001, 100000), (100, 101, 100000),
                                  (2000, 8000, float("nan"))):
            self.assertEqual(estimate_packet_snr(x, left, right, sample_rate_hz=rate)["snr_status"],
                             "invalid_window")

    def test_overflow_never_leaks_infinite_snr(self):
        x = np.ones((5, 10000), dtype=np.complex64)
        x[:, 2000:8000] = np.complex64(1e30)
        result = estimate_packet_snr(x, 2000, 8000, sample_rate_hz=100000)
        self.assertIsNone(result["snr_db"])
        self.assertIsNone(result["snr_total_power"])
        self.assertEqual(result["snr_status"], "invalid_signal_power")


if __name__ == "__main__":
    unittest.main()
