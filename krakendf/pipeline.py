"""Stateful common channelization and coherent packet capture across DAQ frames."""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import numpy as np
from scipy import signal

from .config import AppConfig


def _mix_filter_decimate(iq, oscillator, rotation, sos, state, skip, factor):
    """Same causal SOS recurrence as scipy, emitting only retained samples.

    Compiled lazily when Numba is available. No fast-math or parallel loops:
    all five antenna channels retain the same oscillator, filter and sample
    grid; float64 coefficients/complex128 state match the reference path.
    """
    size = iq.shape[1]
    count = max(0, (size - skip + factor - 1) // factor)
    result = np.empty((iq.shape[0], count), dtype=np.complex64)
    for antenna in range(iq.shape[0]):
        next_output = skip
        output_index = 0
        for sample in range(size):
            value = iq[antenna, sample] * oscillator[sample] * rotation
            for section in range(sos.shape[0]):
                filtered = sos[section, 0] * value + state[section, antenna, 0]
                state[section, antenna, 0] = (sos[section, 1] * value
                                             - sos[section, 4] * filtered
                                             + state[section, antenna, 1])
                state[section, antenna, 1] = sos[section, 2] * value - sos[section, 5] * filtered
                value = filtered
            if sample == next_output:
                result[antenna, output_index] = value
                output_index += 1
                next_output += factor
    return result


_ACCELERATOR_LOCK = threading.Lock()
_ACCELERATOR_CHECKED = False
_ACCELERATOR = None
_ACCELERATOR_ERROR = None


def prepare_acceleration() -> dict:
    """Warm optional acceleration before reception starts, with safe fallback.

    Numba already belongs to the managed-DAQ extra. App-only installations
    continue to work using SciPy. Disk caching is deliberately disabled so
    read-only wheels/source folders cannot break reception or cache writes.
    """
    global _ACCELERATOR_CHECKED, _ACCELERATOR, _ACCELERATOR_ERROR
    with _ACCELERATOR_LOCK:
        if not _ACCELERATOR_CHECKED:
            try:
                from numba import njit
                kernel = njit(cache=False, nogil=True)(_mix_filter_decimate)
                # Compile the actual C-contiguous live-input signature using
                # an empty window; no RF samples or filter state are consumed.
                kernel(np.empty((5, 0), dtype=np.complex64),
                       np.empty(0, dtype=np.complex128), 1 + 0j,
                       np.ascontiguousarray(signal.butter(8, .1, output="sos")),
                       np.zeros((4, 5, 2), dtype=np.complex128), 0, 1)
                _ACCELERATOR = kernel
            except Exception as exc:
                _ACCELERATOR_ERROR = f"{type(exc).__name__}: {exc}"
            _ACCELERATOR_CHECKED = True
    return {"backend": "numba_fused" if _ACCELERATOR is not None else "scipy",
            "fallback_reason": _ACCELERATOR_ERROR}


@dataclass
class Burst:
    iq: np.ndarray
    sample_rate_hz: float
    frequency_hz: float
    sample_start: int
    sample_end: int
    stream_epoch: str
    channel: dict
    timestamp: float
    quality: dict


def spectrum(iq: np.ndarray, sample_rate_hz: float, center_hz: float, fft_size: int) -> dict:
    """Mean input power spectrum; dBFS-like units, no absolute RF calibration."""
    x = iq[:, -min(iq.shape[1], fft_size):]
    if x.shape[1] < 8:
        return {"start_hz": center_hz - sample_rate_hz / 2, "step_hz": sample_rate_hz / fft_size, "power_db": []}
    window = np.hanning(x.shape[1])
    transformed = np.fft.fftshift(np.fft.fft(x * window, n=fft_size, axis=1), axes=1)
    power = np.mean(abs(transformed) ** 2, axis=0) / max(float(window.sum()) ** 2, 1)
    # Bound transport size while preserving narrow peaks.
    groups = max(1, fft_size // 1024)
    power = power.reshape(-1, groups).max(axis=1)
    return {"start_hz": center_hz - sample_rate_hz / 2,
            "step_hz": sample_rate_hz / fft_size * groups,
            "power_db": (10 * np.log10(np.maximum(power, 1e-16))).round(2).tolist()}


class Channelizer:
    """Identical continuous NCO, causal IIR and decimation for all antennas.

    The common filter has a frequency-dependent delay, identical for all five
    antennas. Source_sample_start is the capture interval, not a claimed exact
    physical modulation boundary; decoder-relative boundaries refine it.
    """

    def __init__(self, channel: dict, sample_rate_hz: float, center_hz: float,
                 use_acceleration: bool = True):
        self.channel = channel
        self.input_rate = sample_rate_hz
        self.offset = channel["frequency_hz"] - center_hz
        self.factor = max(1, math.floor(sample_rate_hz / (2 * channel["bandwidth_hz"])))
        self.output_rate = sample_rate_hz / self.factor
        self.sos = signal.butter(8, channel["bandwidth_hz"] * .48,
                                 fs=sample_rate_hz, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 5, 2), dtype=np.complex128)
        self._oscillator = np.empty(0, dtype=np.complex128)
        self._kernel = None
        if use_acceleration:
            prepare_acceleration()
            self._kernel = _ACCELERATOR

    def process(self, iq: np.ndarray, sample_start: int) -> tuple[np.ndarray, int]:
        phase_start = (self.offset * sample_start / self.input_rate) % 1
        if self._oscillator.shape[0] != iq.shape[1]:
            phase = self.offset / self.input_rate * np.arange(iq.shape[1])
            self._oscillator = np.exp(-2j * np.pi * phase)
        rotation = complex(np.exp(-2j * np.pi * phase_start))
        skip = (-sample_start) % self.factor
        if self._kernel is not None:
            # Keep the warmed signature even for strided/read-only callers;
            # normal live frames already satisfy these flags without a copy.
            data = np.require(iq, dtype=np.complex64, requirements=["C", "W", "A"])
            result = self._kernel(data, self._oscillator, rotation, self.sos,
                                  self.zi, skip, self.factor)
            return result, sample_start + skip
        mixed = iq * (self._oscillator * rotation)[None, :]
        filtered, self.zi = signal.sosfilt(self.sos, mixed, axis=1, zi=self.zi)
        return np.asarray(filtered[:, skip::self.factor], dtype=np.complex64), sample_start + skip


class BurstCapture:
    """Stateful burst detector with bounded pretrigger and finite posttrigger.

    The recorded clip includes context; signal_start_offset/signal_end_offset
    identify the active part in output-rate samples for the DF covariance.
    A detected LoRa preamble is not evidence of a successfully decoded payload.
    """
    def __init__(self, channelizer: Channelizer):
        self.c = channelizer
        self.tail = np.zeros((5, 0), dtype=np.complex64)
        self.pending: list[np.ndarray] = []
        self.start = 0
        self.timestamp = 0.0
        self.epoch = ""
        self.noise = None
        self.pending_length = 0
        self.quiet_samples = 0
        self.signal_start_offset = 0
        self.signal_end_offset = 0
        self.hold_samples = 0
        self._chirp_tail = np.zeros((5, 0), dtype=np.complex64)
        self._expected_source_start = None
        self._input_epoch = None

    def _reset(self) -> None:
        self.tail = np.zeros((5, 0), dtype=np.complex64)
        self.pending = []
        self.pending_length = self.quiet_samples = 0
        self.hold_samples = 0
        self.noise = None
        self._chirp_tail = np.zeros((5, 0), dtype=np.complex64)

    def _chirp_activity(self, x: np.ndarray, block: int) -> np.ndarray:
        """Repeated dechirped peaks, preserving history across input frames."""
        sf = self.c.channel.get("lora_sf", 9)
        bw = self.c.channel.get("lora_bandwidth_hz", 125000)
        fs = self.c.output_rate
        n = round((2 ** sf) * fs / bw)
        active = np.zeros(math.ceil(x.shape[1] / block), dtype=bool)
        if n < 16:
            return active
        prefix_length = self._chirp_tail.shape[1]
        combined = np.concatenate((self._chirp_tail, x), axis=1)
        self._chirp_tail = combined[:, -min(combined.shape[1], 3 * n):].copy()
        if combined.shape[1] < n * 3:
            return active
        t = np.arange(n) / fs
        duration = 2 ** sf / bw
        ref = np.exp(-2j * np.pi * (bw / (2 * duration) * t * t - bw / 2 * t))
        last_bin = None
        run = 0
        # Anchor the final window at the newest sample. With tiny DAQ frames,
        # a grid anchored at the old prefix would otherwise examine only old
        # chirps and never mark any current input samples active.
        window_offset = combined.shape[1] % n
        for start in range(window_offset, combined.shape[1] - n + 1, n):
            # Noncoherent antenna-power combination avoids cancellation from
            # unknown spatial phase and a trigger null in a single antenna.
            values = np.mean(abs(np.fft.fft(combined[:, start:start+n] * ref[None, :], axis=1)) ** 2, axis=0)
            peak = int(values.argmax())
            score = float(values[peak] / (np.median(values) + 1e-20))
            distance = n if last_bin is None else abs((peak - last_bin + n // 2) % n - n // 2)
            run = run + 1 if score > 25 and distance <= 3 else (1 if score > 25 else 0)
            if run >= 3:
                left = max(0, (start - 2 * n - prefix_length) // block)
                right = min(len(active), math.ceil((start+n-prefix_length) / block))
                if right > left:
                    active[left:right] = True
            last_bin = peak
        return active

    def _packet_hold_samples(self) -> int:
        """Retain a weak LoRa payload even when only its preamble is visible.

        The BFT text buffer is 96 bytes. Use that upper payload bound, explicit
        header, CRC and conservative 4/8 coding overhead so a firmware coding
        rate change cannot truncate the packet. The payload decoder still has
        to independently establish its actual header, length and CRC.
        """
        if self.c.channel["modulation"] != "lora":
            return 0
        sf = int(self.c.channel.get("lora_sf", 9))
        bw = float(self.c.channel.get("lora_bandwidth_hz", 125000))
        maximum_bytes = int(self.c.channel.get("max_payload_bytes", 96))
        symbol_seconds = 2 ** sf / bw
        low_rate = int(symbol_seconds >= .016)
        coded_groups = max(0, math.ceil((8 * maximum_bytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * low_rate))))
        payload_symbols = 8 + coded_groups * 8
        seconds = (8 + 4.25 + payload_symbols) * symbol_seconds + .02
        return math.ceil(seconds * self.c.output_rate)

    def feed(self, x: np.ndarray, source_start: int, epoch: str, timestamp: float) -> list[Burst]:
        if x.ndim != 2 or x.shape[0] != 5:
            raise ValueError("Burst capture requires five channelized IQ channels")
        if not x.shape[1]:
            return []
        if (self._input_epoch is not None and
                (epoch != self._input_epoch or source_start != self._expected_source_start)):
            self._reset()
        self._input_epoch = epoch
        self._expected_source_start = source_start + x.shape[1] * self.c.factor
        fs = self.c.output_rate
        block = max(1, round(fs * .002))
        powers = np.mean(abs(x) ** 2, axis=0)
        chunk_power = np.array([np.mean(powers[i:i+block]) for i in range(0, len(powers), block)])
        candidate_noise = max(float(np.quantile(chunk_power, .2)), 1e-14)
        if self.noise is None:
            self.noise = candidate_noise
        else:
            # Follow falling noise immediately, while limiting contamination
            # from a fully occupied input frame to three percent per frame.
            self.noise = min(self.noise * 1.03, candidate_noise)
        active = chunk_power > self.noise * 6.3
        if self.c.channel["modulation"] == "lora":
            active |= self._chirp_activity(x, block)
        prefix_length = self.tail.shape[1]
        history = np.concatenate((self.tail, x), axis=1)
        maximum_pretrigger = max(1, round(fs * .12))
        result = []
        postquiet = max(1, round(fs * .06))
        maximum_capture = max(1, round(fs * 10))
        for index, is_active in enumerate(active):
            begin = index * block
            segment = x[:, begin:min(begin+block, x.shape[1])]
            if is_active and not self.pending:
                history_end = prefix_length + begin
                pretrigger = history[:, max(0, history_end-maximum_pretrigger):history_end]
                self.start = source_start + begin * self.c.factor - pretrigger.shape[1] * self.c.factor
                self.timestamp = timestamp + begin / fs - pretrigger.shape[1] / fs
                self.epoch = epoch
                self.pending = [pretrigger.copy()]
                self.pending_length = pretrigger.shape[1]
                self.signal_start_offset = self.pending_length
                self.signal_end_offset = self.pending_length
                self.quiet_samples = 0
                self.hold_samples = self._packet_hold_samples()
            if self.pending:
                self.pending.append(segment.copy())
                self.pending_length += segment.shape[1]
                self.hold_samples = max(0, self.hold_samples-segment.shape[1])
                if is_active:
                    self.quiet_samples = 0
                    self.signal_end_offset = self.pending_length
                else:
                    self.quiet_samples += segment.shape[1]
                if (self.quiet_samples >= postquiet and self.hold_samples == 0) or self.pending_length >= maximum_capture:
                    captured = np.concatenate(self.pending, axis=1)
                    capped = self.pending_length >= maximum_capture
                    result.append(Burst(captured, fs, self.c.channel["frequency_hz"], self.start,
                                        self.start + captured.shape[1] * self.c.factor,
                                        self.epoch, self.c.channel, self.timestamp,
                                        {"valid": True, "capture": "energy_or_chirp", "noise_power": self.noise,
                                         "max_duration_reached": capped,
                                         "decimation_factor": self.c.factor,
                                         "signal_start_offset": self.signal_start_offset,
                                         "signal_end_offset": self.signal_end_offset,
                                         "lora_assumed_max_payload_bytes": 96 if self.c.channel["modulation"] == "lora" else None}))
                    self.pending = []
                    self.pending_length = self.quiet_samples = 0
        self.tail = history[:, -maximum_pretrigger:].copy()
        return result


class LivePipeline:
    def __init__(self, config: AppConfig, use_acceleration: bool = True):
        self.config = config
        self.use_acceleration = use_acceleration
        self.acceleration = {"backend": "unprepared", "fallback_reason": None}
        self.epoch = None
        self.captures: list[BurstCapture] = []
        self.next_sample = None
        self.input_rate = None
        self.input_center = None

    def prepare(self) -> dict:
        """Call before starting the receive pump to avoid a JIT startup gap."""
        self.acceleration = (prepare_acceleration() if self.use_acceleration else
                             {"backend": "scipy", "fallback_reason": "explicitly_disabled"})
        return self.acceleration.copy()

    def process(self, frame) -> tuple[dict, list[Burst]]:
        if not frame.quality.get("valid", False):
            self.epoch = None
            self.captures = []
            self.next_sample = None
            return {}, []
        discontinuous = (self.next_sample is not None and frame.sample_start != self.next_sample)
        changed = (frame.sample_rate_hz != self.input_rate or frame.center_frequency_hz != self.input_center)
        if self.epoch != frame.stream_epoch or discontinuous or changed or frame.quality.get("discontinuity", False):
            self.epoch = frame.stream_epoch
            self.prepare()
            self.captures = [BurstCapture(Channelizer(c.model_dump(), frame.sample_rate_hz,
                                                     frame.center_frequency_hz, self.use_acceleration))
                             for c in self.config.channels if c.enabled]
        self.next_sample = frame.sample_start + frame.iq.shape[1]
        self.input_rate = frame.sample_rate_hz
        self.input_center = frame.center_frequency_hz
        spec = spectrum(frame.iq, frame.sample_rate_hz, frame.center_frequency_hz, self.config.fft_size)
        bursts = []
        for capture in self.captures:
            x, source_start = capture.c.process(frame.iq, frame.sample_start)
            aligned_timestamp = frame.timestamp + (source_start-frame.sample_start) / frame.sample_rate_hz
            bursts.extend(capture.feed(x, source_start, frame.stream_epoch, aligned_timestamp))
        return spec, bursts
