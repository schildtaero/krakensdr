"""Common phase, frame-boundary and gap behavior of the real IQ pipeline."""
import unittest
from unittest.mock import patch

import numpy as np
from scipy import signal

from krakendf.config import AppConfig, ChannelConfig, positions_for
from krakendf.dsp import estimate_doa, steering_vector, wrap180
from krakendf.hardware import IQFrame
from krakendf.pipeline import BurstCapture, Channelizer, LivePipeline
import krakendf.pipeline as pipeline_module


def channel(modulation='pocsag', frequency=433800000, bandwidth=25000):
    return {'id': modulation, 'frequency_hz': frequency, 'bandwidth_hz': bandwidth,
            'modulation': modulation, 'lora_sf': 9, 'lora_bandwidth_hz': 125000}


class ChannelizationTests(unittest.TestCase):
    def test_accelerated_and_fallback_match_causal_reference_at_full_rate(self):
        """Optimization must preserve the actual filter and absolute sample grid."""
        rng = np.random.default_rng(824)
        rate, center, size, origin = 2_400_000, 434_000_000, 65521, 1003
        blocks = [17, 1024, 481, 8192, 317, 22222, 23268]
        for bandwidth, offset in [(10000, -200000), (25000, -200000), (150000, 200000)]:
            samples = np.arange(origin, origin + size)
            tone = np.exp(2j * np.pi * (offset + 2100) / rate * samples)
            spatial = np.exp(1j * np.arange(5) * .48)
            iq = (spatial[:, None] * tone[None, :]
                  + .01 * (rng.standard_normal((5, size)) + 1j * rng.standard_normal((5, size))))
            iq = iq.astype(np.complex64)
            config = channel(frequency=center + offset, bandwidth=bandwidth)
            factor = int(rate // (2 * bandwidth))
            reference = signal.sosfilt(signal.butter(8, bandwidth * .48, fs=rate, output='sos'),
                                       iq * np.exp(-2j * np.pi * offset / rate * samples), axis=1)
            reference = reference[:, (-origin) % factor::factor]
            for accelerated in [False, True]:
                with self.subTest(bandwidth=bandwidth, accelerated=accelerated):
                    instance = Channelizer(config, rate, center, use_acceleration=accelerated)
                    output, cursor = [], 0
                    for count in blocks:
                        if cursor >= size:
                            break
                        stop = min(size, cursor + count)
                        part, mapped = instance.process(iq[:, cursor:stop], origin + cursor)
                        if part.shape[1]:
                            self.assertEqual(mapped % factor, 0)
                        output.append(part)
                        cursor = stop
                    if cursor < size:
                        output.append(instance.process(iq[:, cursor:], origin + cursor)[0])
                    actual = np.concatenate(output, axis=1)
                    np.testing.assert_allclose(actual, reference, rtol=2e-6, atol=2e-6)

    def test_missing_optional_accelerator_keeps_scipy_reception_available(self):
        with patch.object(pipeline_module, '_ACCELERATOR_CHECKED', True), \
                patch.object(pipeline_module, '_ACCELERATOR', None), \
                patch.object(pipeline_module, '_ACCELERATOR_ERROR', 'Numba unavailable'):
            status = pipeline_module.prepare_acceleration()
            instance = Channelizer(channel(), 250000, 433800000)
            actual, mapped = instance.process(np.ones((5, 1000), np.complex64), 0)
        self.assertEqual(status['backend'], 'scipy')
        self.assertEqual(status['fallback_reason'], 'Numba unavailable')
        self.assertEqual(mapped, 0)
        self.assertEqual(actual.shape, (5, 200))
        self.assertTrue(np.isfinite(actual).all())

    def test_arbitrary_frame_sizes_preserve_same_iq_and_doa_for_both_offsets(self):
        rng = np.random.default_rng(1953)
        positions = positions_for(AppConfig())
        source_rate = 1_000_000
        center = 434_000_000
        size = 19877
        for offset, direction in ((90_000, 73.0), (-90_000, 303.0)):
            with self.subTest(offset=offset):
                rf = center + offset
                spatial = steering_vector(positions, rf, direction)
                tone = np.exp(2j * np.pi * (offset + 1100) * np.arange(size) / source_rate)
                iq = spatial[:, None] * tone
                iq += .01 * (rng.standard_normal(iq.shape) + 1j * rng.standard_normal(iq.shape))
                whole = Channelizer(channel(frequency=rf, bandwidth=50_000), source_rate, center)
                expected, expected_start = whole.process(iq, 0)
                streamed = Channelizer(channel(frequency=rf, bandwidth=50_000), source_rate, center)
                output = []
                position = 0
                for block_size in (17, 41, 1023, 100, 3007, 22, 8100, 7567):
                    end = min(position + block_size, size)
                    if end <= position:
                        continue
                    piece, mapped = streamed.process(iq[:, position:end], position)
                    if piece.shape[1]:
                        self.assertEqual(mapped % streamed.factor, 0)
                    output.append(piece)
                    position = end
                if position < size:
                    output.append(streamed.process(iq[:, position:], position)[0])
                actual = np.concatenate(output, axis=1)
                self.assertEqual(expected_start, 0)
                np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-6)
                doa = estimate_doa(actual[:, 20:], positions, rf)
                self.assertLess(abs(wrap180(doa['azimuth_deg'] - direction)), .3)


class BurstCaptureTests(unittest.TestCase):
    def test_packet_crosses_many_cpis_and_finishes_after_quiet(self):
        c = Channelizer(channel(bandwidth=12_000), 50_000, 433800000)
        self.assertEqual(c.factor, 2)
        fs = c.output_rate
        rng = np.random.default_rng(12)
        size, start, end = 18000, 6000, 12000
        x = .01 * (rng.standard_normal((5, size)) + 1j * rng.standard_normal((5, size)))
        x[:, start:end] += 1.0
        capture = BurstCapture(c)
        packets = []
        cursor = 0
        blocks = (311, 777, 401, 1900, 233, 503, 109)
        index = 0
        while cursor < size:
            finish = min(size, cursor + blocks[index % len(blocks)])
            packets.extend(capture.feed(x[:, cursor:finish].astype(np.complex64), cursor*c.factor,
                                        'epoch-a', 100 + cursor/fs))
            cursor, index = finish, index + 1
        self.assertEqual(len(packets), 1)
        packet = packets[0]
        self.assertEqual(packet.sample_end-packet.sample_start, packet.iq.shape[1]*c.factor)
        self.assertLessEqual(packet.sample_start, start*c.factor)
        self.assertGreaterEqual(packet.sample_end, end*c.factor)
        self.assertLess(packet.sample_end, (end+int(fs*.065))*c.factor)
        active = packet.iq[:, packet.quality['signal_start_offset']:packet.quality['signal_end_offset']]
        self.assertGreater(active.shape[1], (end-start)*.97)
        self.assertGreater(float(np.mean(abs(active))), .97)
        self.assertFalse(capture.pending)
        self.assertFalse(packet.quality['max_duration_reached'])

    def test_gap_or_epoch_change_discards_incomplete_packet(self):
        for changed_epoch in (False, True):
            with self.subTest(changed_epoch=changed_epoch):
                c = Channelizer(channel(bandwidth=12_000), 50_000, 433800000)
                capture = BurstCapture(c)
                quiet = np.full((5, 2500), .01+0j, np.complex64)
                loud = np.ones((5, 2500), np.complex64)
                capture.feed(quiet, 0, 'old', 0)
                self.assertEqual(capture.feed(loud, 5000, 'old', .1), [])
                self.assertTrue(capture.pending)
                new_start = 10000 if changed_epoch else 20000
                packets = capture.feed(quiet, new_start, 'new' if changed_epoch else 'old', .2)
                self.assertEqual(packets, [])
                self.assertFalse(capture.pending)

    def test_noise_floor_can_drop_immediately(self):
        c = Channelizer(channel(bandwidth=12_000), 50_000, 433800000)
        capture = BurstCapture(c)
        capture.feed(np.ones((5, 2500), np.complex64), 0, 'same', 0)
        self.assertGreater(capture.noise, .9)
        capture.feed(np.full((5, 2500), .01, np.complex64), 5000, 'same', .1)
        self.assertLess(capture.noise, .001)

    def test_lora_chirp_trigger_below_energy_threshold_with_tiny_frames(self):
        c = Channelizer(channel('lora', bandwidth=150_000), 250_000, 433800000)
        capture = BurstCapture(c)
        rng = np.random.default_rng(177)
        n = 1024  # SF9/BW125k at 250 ksample/s.
        start, size = 8192, 8192+8*n+round(c.output_rate*1.0)
        t = np.arange(n) / c.output_rate
        duration = 512/125000
        symbol = np.exp(2j*np.pi*(125000/(2*duration)*t*t-125000/2*t))
        noise = rng.standard_normal((5, size)) + 1j*rng.standard_normal((5, size))
        spatial = np.exp(1j*np.arange(5)*.71)
        noise[:, start:start+8*n] += .4*spatial[:, None]*np.tile(symbol, 8)[None, :]
        # Packet energy remains close to the floor, far below 8dB squelch.
        baseline = np.mean(abs(noise[:, :start])**2)
        occupied = np.mean(abs(noise[:, start:start+8*n])**2)
        self.assertLess(occupied/baseline, 1.2)
        packets = []
        for cursor in range(0, size, 317):  # Each chunk is < one chirp.
            part = noise[:, cursor:cursor+317].astype(np.complex64)
            packets.extend(capture.feed(part, cursor, 'weak-lora', cursor/c.output_rate))
        self.assertEqual(len(packets), 1)
        self.assertLessEqual(packets[0].sample_start, start)
        # Weak payload symbols need not pass energy squelch. The capture must
        # nevertheless retain the entire normal 288ms BFT packet.
        self.assertGreater(packets[0].sample_end, start+round(c.output_rate*.288))


class LivePipelineTests(unittest.TestCase):
    def configuration(self):
        return AppConfig(center_frequency_hz=433800000, sample_rate_hz=250000,
                         channels=[ChannelConfig(id='p', modulation='pocsag', frequency_hz=433800000,
                                                 bandwidth_hz=25000)])

    def test_invalid_frame_flushes_previous_capture(self):
        pipeline = LivePipeline(self.configuration())
        quiet = IQFrame(np.full((5, 25000), .01, np.complex64), 250000, 433800000,
                        0, 'same', 0, {'valid': True})
        pipeline.process(quiet)
        loud = IQFrame(np.ones((5, 25000), np.complex64), 250000, 433800000,
                       25000, 'same', .1, {'valid': True})
        pipeline.process(loud)
        self.assertTrue(pipeline.captures[0].pending)
        invalid = IQFrame(np.empty((5, 0), np.complex64), 250000, 433800000,
                          50000, 'same', .2, {'valid': False})
        self.assertEqual(pipeline.process(invalid), ({}, []))
        self.assertEqual(pipeline.captures, [])
        resumed = IQFrame(quiet.iq, 250000, 433800000, 50000, 'same', .3, {'valid': True})
        _, packets = pipeline.process(resumed)
        self.assertEqual(packets, [])

    def test_sample_gap_restarts_filter_state_even_with_same_epoch(self):
        pipeline = LivePipeline(self.configuration())
        first = IQFrame(np.full((5, 5000), .01, np.complex64), 250000, 433800000,
                        0, 'same', 0, {'valid': True})
        pipeline.process(first)
        previous = pipeline.captures[0]
        after_gap = IQFrame(first.iq, 250000, 433800000, 15000, 'same', .1, {'valid': True})
        pipeline.process(after_gap)
        self.assertIsNot(previous, pipeline.captures[0])


if __name__ == '__main__':
    unittest.main()
