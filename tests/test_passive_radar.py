"""Signal-level tests: the simulated IQ goes through the real radar chain."""
import dataclasses
import json
import unittest

import numpy as np
from pydantic import ValidationError

from krakendf.passive_radar import (
    C, PassiveRadarConfig, PassiveRadarProcessor, RadarSimulator,
    _cancel_clutter, _cross_ambiguity, _layout, analyze_window,
)


def small_config(**overrides):
    values = dict(illuminator="fm", sample_rate_hz=240_000, bandwidth_hz=100_000,
                  cpi_seconds=.1, range_bins=64, max_doppler_hz=100)
    values.update(overrides)
    return PassiveRadarConfig(**values)


class RadarSettingsTests(unittest.TestCase):
    def test_band_defaults_and_common_decimation(self):
        dab, fm = PassiveRadarConfig(), PassiveRadarConfig(illuminator="fm")
        self.assertEqual(dab.bandwidth_hz, 1_536_000)
        self.assertEqual(dab.decimation_factor, 1)
        self.assertEqual(fm.center_frequency_hz, 100_000_000)
        self.assertEqual(fm.processing_sample_rate_hz, 400_000)
        self.assertEqual(fm.decimation_factor, 6)
        with self.assertRaises(ValidationError):
            fm.range_bins = 64

    def test_rejects_ambiguous_or_unbounded_settings(self):
        for changes in (
            {"reference_channel": 1}, {"sample_rate_hz": 500_000},
            {"bandwidth_hz": 2_000_000}, {"cpi_seconds": 5},
            {"range_bins": 513}, {"doppler_bins": 8192},
            {"gain_db": 20.71}, {"center_frequency_hz": float("nan")},
            {"zero_doppler_guard_hz": 200},
            {"range_bins": 512, "cpi_seconds": 1, "max_doppler_hz": 1000},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PassiveRadarConfig(**changes)
        # Requested long delay axis is allowed when its work stays bounded.
        self.assertEqual(PassiveRadarConfig(range_bins=512).range_bins, 512)


class RadarMathTests(unittest.TestCase):
    def test_fast_valid_correlation_matches_direct_and_has_no_wrap(self):
        rng = np.random.default_rng(452)
        blocks, length, delay, slow_fft, rate = 17, 31, 11, 32, 1000
        ref = (rng.normal(size=blocks * length + delay) +
               1j * rng.normal(size=blocks * length + delay)).astype(np.complex64)
        sur = (rng.normal(size=blocks * length) +
               1j * rng.normal(size=blocks * length)).astype(np.complex64)
        actual, axis = _cross_ambiguity(ref, sur, delay, blocks, length, slow_fft, rate)
        expected = np.empty((blocks, delay + 1), np.complex128)
        for block in range(blocks):
            for lag in range(delay + 1):
                start = block * length
                expected[block, lag] = np.sum(
                    sur[start:start + length] * ref[delay + start - lag:delay + start - lag + length].conj())
        expected *= np.hanning(blocks)[:, None]
        expected = np.abs(np.fft.fftshift(np.fft.fft(expected, n=slow_fft, axis=0), axes=0)) ** 2
        np.testing.assert_allclose(actual, expected, atol=.02, rtol=2e-5)
        np.testing.assert_array_equal(axis, np.fft.fftshift(np.fft.fftfreq(slow_fft, length / rate)))

    def test_clutter_fit_matches_finite_window_least_squares(self):
        rng = np.random.default_rng(8)
        count, delay, taps = 523, 20, 8
        ref = (rng.normal(size=count + delay) + 1j * rng.normal(size=count + delay)).astype(np.complex64)
        design = np.stack([ref[delay - k:delay - k + count] for k in range(taps)], axis=1).astype(np.complex128)
        weights = np.array([2.5, .2j, .4, 0, 0, .1, 0, 0])
        sur = (design @ weights + .02 * (rng.normal(size=count) + 1j * rng.normal(size=count))).astype(np.complex64)
        gram = design.conj().T @ design
        ridge = np.trace(gram).real / taps * 1e-5
        coefficients = np.linalg.solve(gram + np.eye(taps) * ridge, design.conj().T @ sur)
        expected = sur - design @ coefficients
        residual, improvement = _cancel_clutter(ref, sur, delay, taps)
        np.testing.assert_allclose(residual, expected, atol=3e-6)
        self.assertGreater(improvement, 35)

    def test_synthetic_positive_and_negative_doppler_delays(self):
        config = small_config()
        processor, simulator = PassiveRadarProcessor(config), RadarSimulator(config)
        result = processor.process(simulator.frame(0, processor.required_samples))[0]
        self.assertTrue(result.data["quality"]["valid"])
        self.assertGreater(result.data["quality"]["cancellation_db"], 10)
        for truth in simulator.truth:
            match = next(candidate for candidate in result.data["candidates"]
                         if candidate["delay_samples"] == truth["delay_samples"])
            self.assertLess(abs(match["doppler_hz"] - truth["doppler_hz"]), result.data["doppler_resolution_hz"])
            self.assertAlmostEqual(match["excess_path_m"], truth["excess_path_m"])
            self.assertAlmostEqual(match["excess_path_rate_m_s"], -C / config.center_frequency_hz * match["doppler_hz"])
        self.assertAlmostEqual(result.data["range_resolution_m"], C / config.bandwidth_hz)
        self.assertAlmostEqual(result.data["range_bin_m"], C / config.processing_sample_rate_hz)
        self.assertNotEqual(result.data["range_resolution_m"], result.data["range_bin_m"])
        self.assertEqual(result.data["doppler_resolution_hz"], 1 / result.data["cpi_seconds"])
        # Display serialization is finite JSON and supports a long delay axis.
        json.dumps(result.data, allow_nan=False)

    def test_zero_reference_has_no_candidates_and_dark_map(self):
        config = small_config()
        n = PassiveRadarProcessor(config).required_samples
        pair = np.zeros((2, n), np.complex64)
        result = analyze_window(pair, config.processing_sample_rate_hz, config)
        self.assertFalse(result["quality"]["valid"])
        self.assertFalse(result["candidates"])
        self.assertTrue(np.all(np.asarray(result["power_db"]) == -100))

    def test_slow_fft_cannot_drop_blocks(self):
        config = small_config(max_doppler_hz=400, doppler_bins=32)
        _, blocks, _, slow_fft = _layout(config.processing_sample_rate_hz, config)
        self.assertGreaterEqual(slow_fft, blocks)
        self.assertGreater(blocks, config.doppler_bins)

    def test_stationary_direct_path_is_guarded_even_without_cancellation(self):
        config = small_config(clutter_enabled=False)
        count, _, _, _ = _layout(config.processing_sample_rate_hz, config)
        rng = np.random.default_rng(77)
        reference = (rng.normal(size=count + config.range_bins - 1) +
                     1j * rng.normal(size=count + config.range_bins - 1)).astype(np.complex64)
        result = analyze_window(np.stack([reference, reference * 3]), config.processing_sample_rate_hz, config)
        for candidate in result["candidates"]:
            self.assertGreaterEqual(abs(candidate["doppler_hz"]), config.zero_doppler_guard_hz)
            self.assertGreaterEqual(candidate["delay_samples"], config.zero_delay_guard_bins)
        self.assertEqual(result["quality"]["contrast_kind"], "robust_map_floor_not_rf_snr")


class RadarStreamingTests(unittest.TestCase):
    def test_partitioned_frame_filtering_and_replay_are_identical(self):
        config = small_config()
        simulator = RadarSimulator(config)
        full = PassiveRadarProcessor(config)
        count = full.required_samples * 2 + 211
        frame = simulator.frame(0, count)
        expected = full.process(frame)
        split = PassiveRadarProcessor(config)
        observed = []
        offsets = [0, 113, 4072, 29_333, count]
        for start, end in zip(offsets, offsets[1:]):
            observed.extend(split.process(dataclasses.replace(frame, iq=frame.iq[:, start:end],
                sample_start=start, timestamp=frame.timestamp + start / config.sample_rate_hz)))
        self.assertEqual(len(expected), len(observed))
        self.assertGreaterEqual(len(observed), 2)
        for one, two in zip(expected, observed):
            np.testing.assert_array_equal(one.iq, two.iq)
            self.assertEqual(one.data["power_db"], two.data["power_db"])
            replay = analyze_window(two.iq, two.sample_rate_hz, config, two.data)
            self.assertEqual(two.data["power_db"], replay["power_db"])
            self.assertEqual(two.data["candidates"], replay["candidates"])
            self.assertEqual(two.sample_end - two.sample_start, two.iq.shape[1] * config.decimation_factor)
        self.assertEqual(observed[1].sample_start,
                         observed[0].sample_end - (config.range_bins - 1) * config.decimation_factor)

    def test_fm_decimation_grid_is_absolute_across_odd_boundaries(self):
        config = PassiveRadarConfig(illuminator="fm", cpi_seconds=.05, range_bins=32)
        sim, processor = RadarSimulator(config), PassiveRadarProcessor(config)
        start = 13
        count = (processor.required_samples + 20) * config.decimation_factor
        frame = sim.frame(start, count)
        full = processor.process(frame)[0]
        split = PassiveRadarProcessor(config)
        outputs = []
        for begin, end in [(0, 137), (137, 33333), (33333, count)]:
            outputs += split.process(dataclasses.replace(frame, iq=frame.iq[:, begin:end],
                sample_start=start + begin, timestamp=frame.timestamp + begin / config.sample_rate_hz))
        np.testing.assert_array_equal(full.iq, outputs[0].iq)
        self.assertEqual(full.sample_start % config.decimation_factor, 0)
        self.assertEqual(full.sample_start, 18)
        self.assertEqual(full.sample_rate_hz, 400_000)

    def test_invalid_frames_and_gaps_never_join_coherent_windows(self):
        config = small_config()
        for changed in ("invalid", "epoch", "gap", "rate", "cal", "cal_kind", "dummy",
                        "lock", "delay_lock", "overflow", "discontinuity"):
            with self.subTest(changed=changed):
                p, sim = PassiveRadarProcessor(config), RadarSimulator(config)
                half = p.required_samples // 2
                p.process(sim.frame(0, half))
                frame = sim.frame(half, half)
                if changed == "invalid": frame.quality["valid"] = False
                if changed == "epoch": frame.stream_epoch = "new"
                if changed == "gap": frame.sample_start += 1
                if changed == "rate": frame.sample_rate_hz += 1
                if changed == "cal": frame.quality["noise_source"] = True
                if changed == "cal_kind": frame.quality["frame_type"] = 3
                if changed == "dummy": frame.quality["frame_type"] = 1
                if changed == "lock": frame.quality["iq_lock"] = False
                if changed == "delay_lock": frame.quality["delay_lock"] = False
                if changed == "overflow": frame.quality["overdrive_flags"] = 1
                if changed == "discontinuity": frame.quality["discontinuity"] = True
                self.assertFalse(p.process(frame))
                self.assertLessEqual(p.status()["buffered_samples"], half)
                self.assertGreaterEqual(p.status()["discontinuities"], 3)

    def test_nonfinite_rejected_and_simulation_source_has_no_call_seams(self):
        config = small_config()
        simulator = RadarSimulator(config)
        whole = simulator.frame(0, 2000)
        first, second = simulator.frame(0, 799), simulator.frame(799, 1201)
        np.testing.assert_array_equal(whole.iq, np.concatenate([first.iq, second.iq], axis=1))
        bad = simulator.frame(0, 1000)
        bad.iq[0, 2] = np.nan
        p = PassiveRadarProcessor(config)
        self.assertFalse(p.process(bad))
        self.assertEqual(p.status()["reason"], "nonfinite_iq")

    def test_dab_and_fm_two_point_four_msps_recover_real_iq_echoes(self):
        # Actual2.4MS/s acceptance test, not a timed assertion dependent on CI.
        for config in (PassiveRadarConfig(), PassiveRadarConfig(illuminator="fm")):
            with self.subTest(illuminator=config.illuminator):
                p, sim = PassiveRadarProcessor(config), RadarSimulator(config)
                count = (p.required_samples + 1) * config.decimation_factor
                result = p.process(sim.frame(0, count))[0]
                self.assertEqual(result.iq.dtype, np.complex64)
                for truth in sim.truth:
                    self.assertTrue(any(c["delay_samples"] == truth["delay_samples"] and
                        abs(c["doppler_hz"] - truth["doppler_hz"]) <= result.data["doppler_resolution_hz"]
                        for c in result.data["candidates"]))
                self.assertLess(p.status()["buffered_samples"], p.required_samples)
                with self.assertRaises(ValueError):
                    analyze_window(result.iq, result.sample_rate_hz * 2, config)
                with self.assertRaises(ValueError):
                    analyze_window(result.iq[:, :-1], result.sample_rate_hz, config)
                with self.assertRaises(ValueError):
                    analyze_window(result.iq, result.sample_rate_hz, config, {"sample_end": 1})


if __name__ == "__main__":
    unittest.main()
