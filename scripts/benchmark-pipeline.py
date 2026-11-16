#!/usr/bin/env python3
"""Reproducible software-only channelization/capture benchmark.

Run with the project venv, e.g.:
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python scripts/benchmark-pipeline.py

Compares the original full-rate complex128 mix/SciPy SOS path with cached-NCO
SciPy fallback and optional fused Numba. Includes spectrum and burst capture
on the same deterministic five-channel noise windows. Excludes DAQ, USB,
packet decoding, MUSIC, recording and browser rendering: not a hardware or
end-to-end real-time acceptance test. JIT preparation is timed separately.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from krakendf.config import AppConfig
from krakendf.pipeline import BurstCapture, Channelizer, prepare_acceleration, spectrum


class OriginalChannelizer(Channelizer):
    """Reference implementation before cached oscillators/fused filtering."""
    def __init__(self, channel, rate, center):
        super().__init__(channel, rate, center, use_acceleration=False)

    def process(self, iq, sample_start):
        phase = ((self.offset * sample_start / self.input_rate) % 1
                 + self.offset / self.input_rate * np.arange(iq.shape[1]))
        mixed = iq * np.exp(-2j * np.pi * phase)[None, :]
        filtered, self.zi = signal.sosfilt(self.sos, mixed, axis=1, zi=self.zi)
        skip = (-sample_start) % self.factor
        return np.asarray(filtered[:, skip::self.factor], dtype=np.complex64), sample_start + skip


def run(config, iq, count, warmup, backend):
    captures = []
    for channel in config.channels:
        if channel.enabled:
            if backend == 'original':
                c = OriginalChannelizer(channel.model_dump(), config.sample_rate_hz, config.center_frequency_hz)
            else:
                c = Channelizer(channel.model_dump(), config.sample_rate_hz, config.center_frequency_hz,
                                use_acceleration=backend == 'accelerated')
            captures.append(BurstCapture(c))
    timings = []
    for index in range(count + warmup):
        start = index * iq.shape[1]
        before = time.perf_counter()
        spectrum(iq, config.sample_rate_hz, config.center_frequency_hz, config.fft_size)
        for capture in captures:
            channelized, mapped = capture.c.process(iq, start)
            capture.feed(channelized, mapped, 'benchmark', mapped / config.sample_rate_hz)
        if index >= warmup:
            timings.append(time.perf_counter() - before)
    median = float(np.median(timings))
    rf_seconds = iq.shape[1] / config.sample_rate_hz
    return {'backend': backend, 'median_seconds': median, 'min_seconds': min(timings),
            'max_seconds': max(timings), 'rf_seconds_per_frame': rf_seconds,
            'processing_to_rf_ratio': median / rf_seconds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame-samples', type=int, default=1048576)
    parser.add_argument('--frames', type=int, default=8)
    parser.add_argument('--warmup', type=int, default=2)
    args = parser.parse_args()
    if args.frame_samples < 16 or args.frames < 1 or args.warmup < 0:
        parser.error('positive frame size/count and nonnegative warmup required')
    config = AppConfig()
    rng = np.random.default_rng(433)
    iq = (rng.standard_normal((5, args.frame_samples), dtype=np.float32)
          + 1j * rng.standard_normal((5, args.frame_samples), dtype=np.float32)).astype(np.complex64)
    before = time.perf_counter()
    acceleration = prepare_acceleration()
    print(json.dumps({'acceleration': acceleration, 'preparation_seconds': time.perf_counter() - before,
                      'sample_rate_hz': config.sample_rate_hz, 'rf_channels': len(config.channels),
                      'frame_samples': args.frame_samples, 'measured_frames': args.frames}), flush=True)
    results = []
    for backend in ['original', 'scipy', 'accelerated']:
        if backend == 'accelerated' and acceleration['backend'] != 'numba_fused':
            print(json.dumps({'benchmark_skipped': backend,
                              'reason': acceleration['fallback_reason']}), flush=True)
            continue
        result = run(config, iq, args.frames, args.warmup, backend)
        result['actual_backend'] = 'numba_fused' if backend == 'accelerated' else 'scipy'
        results.append(result)
        result['speedup_vs_original'] = results[0]['median_seconds'] / result['median_seconds']
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
