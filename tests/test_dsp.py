"""Meaningful numerical tests; no SDR hardware is implied by these fixtures."""
import json
import math
from pathlib import Path
import unittest

import numpy as np

from krakendf.dsp import (apply_calibration, bearing_deg, destination, estimate_doa,
                         fit_calibration, statistics, steering_vector, wrap180)


ROOT = Path(__file__).resolve().parents[1]


class GeodesicTests(unittest.TestCase):
    def test_wrap_boundary(self):
        self.assertEqual(wrap180(359 - 1), -2)
        self.assertEqual(wrap180(1 - 359), 2)
        self.assertEqual(wrap180(180), -180)
        np.testing.assert_equal(wrap180([0, 360, -181]), [0, 0, 179])

    def test_wgs84_inverse_known_reference(self):
        # Published Vincenty inverse reference: Flinders Peak -> Buninyong.
        angle = bearing_deg(-37.95103341666667, 144.42486788888888,
                            -37.65282113888889, 143.92649552777777)
        self.assertAlmostEqual(angle, 306.86816, places=4)

    def test_direct_inverse_small_and_dateline(self):
        for latitude, longitude in ((52.0, 13.0), (0.0, 179.999), (-70.0, -160.0)):
            for angle in (0.0, 90.0, 183.5, 359.0):
                endpoint = destination(latitude, longitude, angle, 500.0)
                self.assertLess(abs(wrap180(bearing_deg(latitude, longitude, *endpoint) - angle)), 1e-6)

    def test_invalid_references(self):
        with self.assertRaises(ValueError):
            bearing_deg(52, 13, 52, 13)
        with self.assertRaises(ValueError):
            bearing_deg(100, 13, 52, 13)
        with self.assertRaises(ValueError):
            bearing_deg(0, 0, 0, 180)


class DirectionFindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.presets = json.loads((ROOT / 'config/antenna-presets.json').read_text())['presets']

    def test_all_geometries_and_angle_boundary(self):
        rng = np.random.default_rng(1212)
        for preset in self.presets:
            positions = np.array([[a['x_m'], a['y_m'], a['z_m']] for a in preset['antennas']])
            for angle in (0.0, 90.0, 183.0, 359.0):
                with self.subTest(preset=preset['id'], angle=angle):
                    # Common modulated waveform, with independent antenna noise.
                    waveform = np.exp(1j * rng.uniform(-np.pi, np.pi, 4096))
                    iq = steering_vector(positions, 433e6, angle)[:, None] * waveform
                    iq += 0.12 * (rng.normal(size=iq.shape) + 1j * rng.normal(size=iq.shape))
                    result = estimate_doa(iq.astype(np.complex64), positions, 433e6)
                    self.assertLess(abs(wrap180(result['azimuth_deg'] - angle)), 0.4)
                    self.assertGreater(result['confidence'], 0.9)
                    self.assertEqual(len(result['angles_deg']), 360)

    def test_common_phase_and_gain_do_not_change_doa(self):
        positions = np.array([[a['x_m'], a['y_m']] for a in self.presets[1]['antennas']])
        iq = steering_vector(positions, 433e6, 77.3)[:, None] * np.ones((1, 512), complex)
        first = estimate_doa(iq, positions, 433e6, method='bartlett')['azimuth_deg']
        second = estimate_doa(iq * (0.0001 * np.exp(1.7j)), positions, 433e6, method='bartlett')['azimuth_deg']
        self.assertLess(abs(wrap180(first - 77.3)), 0.05)
        self.assertAlmostEqual(first, second, places=7)

    def test_iq_validation(self):
        positions = np.array([[a['x_m'], a['y_m']] for a in self.presets[0]['antennas']])
        for iq in (np.zeros((5, 100)), np.ones((4, 100)), np.full((5, 100), np.nan)):
            with self.assertRaises(ValueError):
                estimate_doa(iq, positions, 433e6)


class StatisticsAndCalibrationTests(unittest.TestCase):
    def test_statistics_wrap_reference_bins_and_missing_sectors(self):
        result = statistics([
            {'reference_deg': 359, 'error_raw_deg': 358, 'error_corrected_deg': 1},
            {'reference_deg': 1, 'error_raw_deg': -358, 'error_corrected_deg': -1},
            {'reference_deg': 180, 'error_raw_deg': 90, 'accepted': False},
        ])
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['excluded_count'], 1)
        self.assertAlmostEqual(result['rms_raw_deg'], 2)
        self.assertAlmostEqual(result['rms_corrected_deg'], 1)
        self.assertEqual(result['bins'][0]['count'], 1)
        self.assertEqual(result['bins'][23]['count'], 1)
        self.assertIsNone(result['bins'][10]['rms_raw_deg'])
        self.assertAlmostEqual(result['bias_raw_deg'], 0)

    def test_sector_balancing_and_profile_visibility(self):
        data = [{'reference_deg': 1, 'error_raw_deg': 1, 'profile_id': 'a'}] * 9
        data += [{'reference_deg': 31, 'error_raw_deg': 9, 'profile_id': 'b'}]
        result = statistics(data)
        self.assertAlmostEqual(result['rms_raw_deg'], 3)
        self.assertAlmostEqual(result['equal_sector_rms_raw_deg'], math.sqrt(41))
        self.assertTrue(result['mixed_profiles'])
        self.assertEqual(result['by_profile']['a']['count'], 9)

    def calibration_fixture(self, run_id, samples_per_sector, seed, inverse=False):
        rng = np.random.default_rng(seed)
        records = []
        for sector in range(24):
            for sample in range(samples_per_sector):
                # Centered positions guarantee every required direction sector
                # is sampled by independently generated training/validation.
                reference = sector * 15 + 2 + (sample + 0.5) * 11 / samples_per_sector
                bias = 2 + 4 * math.sin(math.radians(reference)) + 1.5 * math.cos(math.radians(2 * reference))
                if inverse:
                    bias = -bias
                raw = (reference + bias + rng.normal(0, .2)) % 360
                records.append({'raw_deg': raw, 'reference_deg': reference, 'run_id': run_id,
                                'burst_id': f'{run_id}-{sector}-{sample}', 'profile_id': 'mounted-array-1'})
        return records

    def test_independent_calibration_improves_known_bias(self):
        training = self.calibration_fixture('training', 5, 42)
        validation = self.calibration_fixture('validation', 2, 77)
        model = fit_calibration(training, validation)
        self.assertTrue(model['eligible'])
        self.assertTrue(model['validated'])
        self.assertTrue(model['improved'])
        self.assertLess(model['validation_after_deg'], .4)
        self.assertGreater(model['validation_before_deg'], 3)
        self.assertLess(abs(wrap180(apply_calibration(validation[0]['raw_deg'], model)
                                   - validation[0]['reference_deg'])), .6)

    def test_sparse_same_run_or_mixed_profile_do_not_enable(self):
        training = self.calibration_fixture('training', 3, 42)
        same_run = self.calibration_fixture('training', 1, 77)
        self.assertFalse(fit_calibration(training[:10], same_run)['eligible'])
        same_model = fit_calibration(training, same_run)
        self.assertTrue(same_model['eligible'])
        self.assertFalse(same_model['validated'])
        self.assertEqual(apply_calibration(10, same_model), 10)
        mixed = self.calibration_fixture('validation', 1, 12)
        mixed[0]['profile_id'] = 'different-mounting'
        self.assertFalse(fit_calibration(training, mixed)['eligible'])

    def test_failing_independent_validation_is_not_applied(self):
        model = fit_calibration(self.calibration_fixture('train', 3, 42),
                                self.calibration_fixture('check', 2, 77, inverse=True))
        self.assertTrue(model['validated'])
        self.assertFalse(model['improved'])
        self.assertEqual(apply_calibration(30, model), 30)

    def test_calibration_handles_zero_crossing_without_360_degree_error(self):
        training = self.calibration_fixture('train', 3, 42)
        validation = self.calibration_fixture('check', 2, 77)
        model = fit_calibration(training, validation)
        for raw in (0, .001, 359.999, 360):
            self.assertTrue(0 <= apply_calibration(raw, model) < 360)
        self.assertLess(abs(wrap180(apply_calibration(0, model) - apply_calibration(359.999, model))), .01)


if __name__ == '__main__':
    unittest.main()
