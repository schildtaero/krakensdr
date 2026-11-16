"""Stateful common channelization and coherent packet capture across DAQ frames."""
from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
from scipy import signal

from .config import AppConfig


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

    def __init__(self, channel: dict, sample_rate_hz: float, center_hz: float):
        self.channel = channel
        self.input_rate = sample_rate_hz
        self.offset = channel["frequency_hz"] - center_hz
        self.factor = max(1, math.floor(sample_rate_hz / (2 * channel["bandwidth_hz"])))
        self.output_rate = sample_rate_hz / self.factor
        self.sos = signal.butter(8, channel["bandwidth_hz"] * .48,
                                 fs=sample_rate_hz, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 5, 2), dtype=np.complex128)

    def process(self, iq: np.ndarray, sample_start: int) -> tuple[np.ndarray, int]:
        phase_start = (self.offset * sample_start / self.input_rate) % 1
        phase = phase_start + self.offset / self.input_rate * np.arange(iq.shape[1])
        mixed = iq * np.exp(-2j * np.pi * phase)[None, :]
        filtered, self.zi = signal.sosfilt(self.sos, mixed, axis=1, zi=self.zi)
        skip = (-sample_start) % self.factor
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
    def __init__(self, config: AppConfig):
        self.config = config
        self.epoch = None
        self.captures: list[BurstCapture] = []
        self.next_sample = None
        self.input_rate = None
        self.input_center = None

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
            self.captures = [BurstCapture(Channelizer(c.model_dump(), frame.sample_rate_hz,
                                                     frame.center_frequency_hz))
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
