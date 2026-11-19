"""Bounded two-channel bistatic passive radar for coherent Kraken IQ.

The displayed distance is excess propagation path c*tau, never monostatic
range c*tau/2. A detection is an ambiguity-map candidate, not an aircraft
identification or a geographic position. Positive Doppler means decreasing
excess path; its rate is -wavelength*Doppler, not target ground speed.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy import fft, ndimage, signal

from .hardware import IQFrame, MANUAL_GAINS_DB, MAX_SAMPLES_PER_CHANNEL

C = 299_792_458.0
MAX_CPI_SAMPLES = 2_400_000
MAX_WORK_ELEMENTS = 8_000_000


class PassiveRadarConfig(BaseModel):
    """Immutable acquisition/processing settings; defaults are a DAB example."""
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    illuminator: Literal["dab", "fm"] = "dab"
    center_frequency_hz: float = 220_352_000
    sample_rate_hz: int = Field(default=2_400_000, ge=225_001, le=2_400_000)
    bandwidth_hz: float = Field(default=1_536_000, ge=50_000, le=1_800_000)
    gain_db: float = 20.7
    reference_channel: int = Field(default=0, ge=0, le=4)
    surveillance_channel: int = Field(default=1, ge=0, le=4)
    cpi_seconds: float = Field(default=0.25, ge=0.05, le=1.0)
    range_bins: int = Field(default=128, ge=16, le=512)
    max_doppler_hz: float = Field(default=200, ge=10, le=1000)
    doppler_bins: int = Field(default=128, ge=32, le=4096)
    clutter_taps: int = Field(default=8, ge=1, le=32)
    clutter_enabled: bool = True
    threshold_db: float = Field(default=12, ge=6, le=40)
    zero_doppler_guard_hz: float = Field(default=8, ge=0, le=100)
    zero_delay_guard_bins: int = Field(default=2, ge=0, le=32)

    @model_validator(mode="before")
    @classmethod
    def defaults_for_band(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("illuminator") == "fm":
            value = dict(value)
            value.setdefault("center_frequency_hz", 100_000_000)
            value.setdefault("bandwidth_hz", 200_000)
        return value

    @model_validator(mode="after")
    def compatible(self) -> "PassiveRadarConfig":
        if not (225_001 <= self.sample_rate_hz <= 300_000 or
                900_001 <= self.sample_rate_hz <= 2_400_000):
            raise ValueError("RTL sample rate must be 225001..300000 or 900001..2400000 Hz")
        if self.reference_channel == self.surveillance_channel:
            raise ValueError("Reference and surveillance channels must differ")
        if self.bandwidth_hz > self.sample_rate_hz * 0.9:
            raise ValueError("Illuminator bandwidth must fit inside 90% of the IQ sample rate")
        if self.illuminator == "dab" and not 174_000_000 <= self.center_frequency_hz <= 240_000_000:
            raise ValueError("DAB preset requires a Band III frequency, 174..240 MHz")
        if self.illuminator == "fm":
            if not 87_500_000 <= self.center_frequency_hz <= 108_000_000:
                raise ValueError("FM preset requires an 87.5..108 MHz frequency")
            if self.bandwidth_hz > 250_000:
                raise ValueError("FM processing bandwidth must not exceed 250 kHz")
        if min(abs(self.gain_db - x) for x in MANUAL_GAINS_DB) > 1e-6:
            raise ValueError("Unsupported Kraken manual gain")
        if self.clutter_taps > self.range_bins:
            raise ValueError("Clutter taps must not exceed the delay-bin count")
        if self.zero_delay_guard_bins >= self.range_bins:
            raise ValueError("Zero-delay guard must leave searchable delay bins")
        if self.zero_doppler_guard_hz >= self.max_doppler_hz:
            raise ValueError("Zero-Doppler guard must be smaller than the Doppler span")
        _layout(self.processing_sample_rate_hz, self)
        return self

    @property
    def decimation_factor(self) -> int:
        # Keep >=2*occupied bandwidth: generous transition band and adequate
        # delay sampling. Both channels always receive the identical filter.
        return max(1, int(self.sample_rate_hz // (2 * self.bandwidth_hz)))

    @property
    def processing_sample_rate_hz(self) -> float:
        return self.sample_rate_hz / self.decimation_factor


def _layout(rate: float, config: PassiveRadarConfig) -> tuple[int, int, int, int]:
    blocks = max(16, math.ceil(4 * config.max_doppler_hz * config.cpi_seconds))
    length = int(round(rate * config.cpi_seconds)) // blocks
    if length < 16:
        raise ValueError("Too few samples per Doppler block")
    count = blocks * length
    delay = config.range_bins - 1
    nfft = fft.next_fast_len(length + delay)
    if count > MAX_CPI_SAMPLES or blocks * nfft > MAX_WORK_ELEMENTS:
        raise ValueError("Radar CPI exceeds the bounded processing allocation")
    slow_fft = 1 << (max(blocks, config.doppler_bins) - 1).bit_length()
    if slow_fft * config.range_bins > MAX_WORK_ELEMENTS:
        raise ValueError("Radar Doppler map exceeds the bounded processing allocation")
    map_rows = min(slow_fft, 2 * math.floor(config.max_doppler_hz * length * slow_fft / rate) + 1)
    if map_rows * config.range_bins > 512_000:
        raise ValueError("Displayed radar map exceeds 512000 cells; reduce CPI, span, FFT or delay bins")
    return count, blocks, length, slow_fft


@dataclass(frozen=True)
class RadarResult:
    data: dict[str, Any]
    iq: np.ndarray
    sample_rate_hz: float
    sample_start: int
    sample_end: int
    stream_epoch: str
    timestamp: float


def _cancel_clutter(reference: np.ndarray, surveillance: np.ndarray,
                    delay: int, taps: int) -> tuple[np.ndarray, float]:
    """Exact finite-window regularized FIR least squares with bounded work.

    No circular reference shifts: the recorded prefix supplies real samples
    for every fitted delay. A small diagonal ridge handles correlated FM IQ.
    """
    count = len(surveillance)
    gram = np.zeros((taps, taps), np.complex128)
    cross = np.zeros(taps, np.complex128)
    # Equal-lag Gram entries differ only at their finite-window edges.
    # Compute one full dot product per lag, then update those edges exactly.
    # This is O(N*K), avoids N*K design matrices and avoids small matrix
    # BLAS calls spawning many threads on hosts with broad CPU defaults.
    for lag in range(taps):
        value = np.sum(reference[delay:delay + count].conj() *
                       reference[delay - lag:delay - lag + count], dtype=np.complex128)
        for k in range(taps - lag):
            gram[k, k + lag] = value
            gram[k + lag, k] = value.conjugate()
            if k + 1 < taps - lag:
                left = delay - k - 1
                right = delay - k + count - 1
                value += (np.conj(reference[left]) * reference[left - lag]
                          - np.conj(reference[right]) * reference[right - lag])
        cross[lag] = np.sum(reference[delay - lag:delay - lag + count].conj() *
                            surveillance, dtype=np.complex128)
    ridge = max(float(np.trace(gram).real / taps) * 1e-5, 1e-15)
    coefficients = np.linalg.solve(gram + ridge * np.eye(taps), cross)
    residual = surveillance.copy()
    for k, coefficient in enumerate(coefficients):
        residual -= coefficient * reference[delay - k:delay - k + count]
    before = float(np.mean(np.abs(surveillance) ** 2))
    after = float(np.mean(np.abs(residual) ** 2))
    cancellation = 10 * math.log10(max(before, 1e-30) / max(after, 1e-30))
    return residual, cancellation


def _cross_ambiguity(reference: np.ndarray, surveillance: np.ndarray,
                     delay: int, blocks: int, length: int, slow_fft: int,
                     sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Linear fast correlation per block followed by a slow-time Hann FFT.

    reference includes delay real prefix samples. Only valid convolution
    indices L-1..L-1+D are used. Padding to L+D is sufficient for that crop:
    the circularly wrapped tail ends at L-2 and cannot enter the valid crop.
    The slow FFT is never shorter than its input (no discarded CPI blocks).
    """
    windows = np.lib.stride_tricks.sliding_window_view(reference, length + delay)[::length]
    windows = windows[:blocks]
    nfft = fft.next_fast_len(length + delay)
    ref_fft = fft.fft(windows[:, ::-1].conj(), n=nfft, axis=1, workers=1)
    sur_fft = fft.fft(surveillance.reshape(blocks, length), n=nfft, axis=1, workers=1)
    ref_fft *= sur_fft
    del sur_fft
    correlation = fft.ifft(ref_fft, axis=1, workers=1)[:, length - 1:length + delay].copy()
    del ref_fft
    correlation *= np.hanning(blocks).astype(np.float32)[:, None]
    ambiguity = fft.fftshift(fft.fft(correlation, n=slow_fft, axis=0, workers=1), axes=0)
    doppler = fft.fftshift(fft.fftfreq(slow_fft, d=length / sample_rate_hz))
    return np.abs(ambiguity).astype(np.float64) ** 2, doppler


def analyze_window(pair: np.ndarray, sample_rate_hz: float,
                   config: PassiveRadarConfig, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Analyze one channelized pair, including its real delay prefix.

    Recorded replay must use this function without filtering a second time.
    Input length must equal the derived CPI length + range_bins - 1.
    Power is relative ambiguity power, not calibrated received dBm; contrast
    is a robust local-map floor comparison, not RF SNR or a calibrated CFAR.
    """
    started = time.perf_counter()
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0 or rate > 2_400_000:
        raise ValueError("Invalid radar processing rate")
    if abs(rate - config.processing_sample_rate_hz) > 1e-6:
        raise ValueError("Recorded processing rate does not match radar settings")
    count, blocks, length, slow_fft = _layout(rate, config)
    delay = config.range_bins - 1
    if pair.ndim != 2 or pair.shape != (2, count + delay):
        raise ValueError(f"Radar pair must have shape (2, {count + delay})")
    if not np.iscomplexobj(pair) or not np.isfinite(pair).all():
        raise ValueError("Radar pair must contain finite complex IQ")
    pair = np.asarray(pair, dtype=np.complex64)
    reference = pair[0]
    surveillance = pair[1, delay:]
    reference_power = float(np.mean(np.abs(reference[delay:]) ** 2))
    surveillance_power = float(np.mean(np.abs(surveillance) ** 2))
    reference_peak = float(np.max(np.abs(reference)))
    surveillance_peak = float(np.max(np.abs(surveillance)))
    valid = reference_power > 1e-10 and surveillance_power > 1e-12
    reason = "ok" if valid else "reference_or_surveillance_has_no_signal"
    cancellation_db = 0.0
    if config.clutter_enabled and valid:
        surveillance, cancellation_db = _cancel_clutter(reference, surveillance, delay, config.clutter_taps)
    power, doppler = _cross_ambiguity(reference, surveillance, delay, blocks, length, slow_fft, rate)
    keep = np.abs(doppler) <= config.max_doppler_hz + 1e-9
    power, doppler = power[keep], doppler[keep]
    peak = max(float(np.max(power)), 1e-30)
    power_db = np.clip(10 * np.log10(np.maximum(power, 1e-30) / peak), -100, 0)
    if not valid:
        power_db.fill(-100)
    searchable = np.ones(power.shape, dtype=bool)
    searchable[:, :config.zero_delay_guard_bins] = False
    searchable[np.abs(doppler) < config.zero_doppler_guard_hz, :] = False
    # A frequency-dependent floor prevents a strong zero-Doppler row from
    # defining the noise estimate for all Doppler rows. Delay medians are
    # smoothed over five slow-frequency cells to reduce short-map variance.
    row_floor = np.median(np.maximum(power[:, config.zero_delay_guard_bins:], 1e-30), axis=1) / math.log(2)
    row_floor = ndimage.median_filter(row_floor, size=5, mode="nearest")
    floor = np.maximum(row_floor[:, None], max(float(np.median(power[searchable])) if searchable.any() else 0, 1e-30) * .1)
    contrast = 10 * np.log10(np.maximum(power, 1e-30) / floor)
    peaks = (power == ndimage.maximum_filter(power, size=(3, 3), mode="nearest"))
    peaks &= searchable & (contrast >= config.threshold_db)
    candidates: list[dict[str, Any]] = []
    if valid:
        rows, columns = np.nonzero(peaks)
        order = np.argsort(power[rows, columns])[::-1][:20]
        for index in order:
            row, column = int(rows[index]), int(columns[index])
            candidates.append({
                "delay_samples": column, "delay_seconds": column / rate,
                "excess_path_m": column * C / rate, "doppler_hz": float(doppler[row]),
                "excess_path_rate_m_s": -C / config.center_frequency_hz * float(doppler[row]),
                "contrast_db": float(contrast[row, column]), "power_db": float(power_db[row, column]),
            })
    metadata = dict(meta or {})
    timestamp = float(metadata.get("timestamp_unix", metadata.get("timestamp", time.time())))
    factor = config.decimation_factor
    sample_start = int(metadata.get("sample_start", metadata.get("iq_sample_start", 0)))
    sample_end = int(metadata.get("sample_end", sample_start + pair.shape[1] * factor))
    if sample_end - sample_start != pair.shape[1] * factor:
        raise ValueError("Recorded radar sample bounds do not match its decimation grid")
    result = {
        "schema_version": 1, "mode": "passive_radar", "illuminator": config.illuminator,
        "excess_path_m": (np.arange(config.range_bins) * C / rate).tolist(),
        "doppler_hz": doppler.tolist(), "power_db": np.round(power_db, 2).tolist(),
        "candidates": candidates,
        "quality": {"valid": valid, "reason": reason, "reference_power": reference_power,
                    "surveillance_power": surveillance_power, "reference_peak": reference_peak,
                    "surveillance_peak": surveillance_peak, "cancellation_db": cancellation_db,
                    "contrast_kind": "robust_map_floor_not_rf_snr", "hardware_overload": False,
                    "reference_presence_check": "nonzero_iq_power_not_broadcast_decode",
                    "reference_power_floor": 1e-10},
        "cpi_seconds": count / rate, "range_resolution_m": C / config.bandwidth_hz,
        "range_resolution_kind": "nominal_c_over_configured_bandwidth",
        "range_bin_m": C / rate, "doppler_resolution_hz": rate / count,
        "doppler_bin_hz": rate / (length * slow_fft), "window_kind": "hann",
        "doppler_resolution_note": "1/T nominal; Hann main lobe first null is 2/T",
        "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "timestamp_unix": timestamp, "sample_start": sample_start, "sample_end": sample_end,
        "timestamp_source": "receiver_host_clock_not_gps_pps",
        "cpi_sample_start": sample_start + delay * factor, "cpi_sample_end": sample_end,
        "iq_stage": "channelized_pair_with_prefix", "iq_sample_rate_hz": rate,
        "iq_frequency_hz": config.center_frequency_hz, "iq_sample_start": sample_start,
        "iq_sample_end": sample_end,
        "decimation_factor": factor, "stream_epoch": str(metadata.get("stream_epoch", "replay")),
        "reference_channel": config.reference_channel, "surveillance_channel": config.surveillance_channel,
        "zero_doppler_guard_hz": config.zero_doppler_guard_hz,
        "zero_delay_guard_bins": config.zero_delay_guard_bins,
        "processing_seconds": time.perf_counter() - started,
    }
    return result


class PassiveRadarProcessor:
    """Streaming common filter, decimation and nonoverlapping coherent CPIs."""
    def __init__(self, config: PassiveRadarConfig):
        self.config = config
        self.sample_rate_hz = config.processing_sample_rate_hz
        self.required_samples = _layout(self.sample_rate_hz, config)[0] + config.range_bins - 1
        self._sos = signal.butter(8, config.bandwidth_hz * .48, fs=config.sample_rate_hz, output="sos")
        self.reset("waiting_for_coherent_iq")

    def reset(self, reason: str = "reset") -> None:
        self._zi = np.zeros((len(self._sos), 2, 2), dtype=np.complex128)
        self._buffer = np.empty((2, 0), dtype=np.complex64)
        self._expected: int | None = None
        self._epoch: str | None = None
        self._buffer_start = 0
        self._buffer_timestamp = 0.0
        self.last_reason = reason
        self.discontinuities = getattr(self, "discontinuities", 0) + 1

    def status(self) -> dict[str, Any]:
        return {"reason": self.last_reason, "buffered_samples": self._buffer.shape[1],
                "required_samples": self.required_samples, "sample_rate_hz": self.sample_rate_hz,
                "discontinuities": self.discontinuities}

    def process(self, frame: IQFrame) -> list[RadarResult]:
        config = self.config
        if (not frame.quality.get("valid", False) or frame.quality.get("noise_source", False)
                or frame.quality.get("overdrive_flags", 0) or frame.quality.get("frame_type", 0) != 0
                or not frame.quality.get("delay_lock", True) or not frame.quality.get("iq_lock", True)):
            self.reset("invalid_or_unlocked_iq")
            return []
        if frame.sample_rate_hz != config.sample_rate_hz or abs(frame.center_frequency_hz - config.center_frequency_hz) > 1:
            self.reset("acquisition_settings_mismatch")
            return []
        if (frame.iq.ndim != 2 or frame.iq.shape[0] != 5 or frame.iq.shape[1] > MAX_SAMPLES_PER_CHANNEL
                or frame.iq.shape[1] == 0 or not np.iscomplexobj(frame.iq)):
            self.reset("invalid_iq_shape")
            return []
        if frame.sample_start < 0 or not math.isfinite(frame.timestamp):
            self.reset("invalid_sample_metadata")
            return []
        if (self._epoch != frame.stream_epoch or frame.quality.get("discontinuity", False)
                or (self._expected is not None and self._expected != frame.sample_start)):
            self.reset("stream_discontinuity")
            self._epoch = frame.stream_epoch
        self._expected = frame.sample_start + frame.iq.shape[1]
        outputs = []
        factor = config.decimation_factor
        for offset in range(0, frame.iq.shape[1], 262_144):
            end = min(offset + 262_144, frame.iq.shape[1])
            pair = frame.iq[[config.reference_channel, config.surveillance_channel], offset:end]
            if not np.isfinite(pair).all():
                self.reset("nonfinite_iq")
                return []
            filtered, self._zi = signal.sosfilt(self._sos, pair, axis=1, zi=self._zi)
            raw_start = frame.sample_start + offset
            skip = (-raw_start) % factor
            filtered = filtered[:, skip::factor].astype(np.complex64)
            if not filtered.shape[1]:
                continue
            if not self._buffer.shape[1]:
                self._buffer_start = raw_start + skip
                self._buffer_timestamp = frame.timestamp + (offset + skip) / config.sample_rate_hz
            self._buffer = np.concatenate((self._buffer, filtered), axis=1)
            while self._buffer.shape[1] >= self.required_samples:
                clip = self._buffer[:, :self.required_samples].copy()
                sample_end = self._buffer_start + self.required_samples * factor
                metadata = {"sample_start": self._buffer_start, "sample_end": sample_end,
                            "timestamp_unix": self._buffer_timestamp, "stream_epoch": frame.stream_epoch}
                data = analyze_window(clip, self.sample_rate_hz, config, metadata)
                outputs.append(RadarResult(data, clip, self.sample_rate_hz, self._buffer_start,
                                           sample_end, frame.stream_epoch, self._buffer_timestamp))
                advance = self.required_samples - config.range_bins + 1
                self._buffer = self._buffer[:, advance:].copy()
                self._buffer_start += advance * factor
                self._buffer_timestamp += advance / self.sample_rate_hz
                self.last_reason = data["quality"]["reason"]
        if not outputs:
            self.last_reason = "integrating"
        return outputs


class RadarSimulator:
    """Deterministic coherent IQ fixture with two moving echoes and clutter.

    The signal is band-limited pseudo-noise, not a DAB/FM protocol emulator.
    Known targets exercise the same filter/canceller/CAF as actual reception.
    Absolute sample-index noise makes successive calls exactly continuous.
    """
    def __init__(self, config: PassiveRadarConfig, seed: int = 433):
        self.config, self.seed = config, int(seed)
        self.started = time.time()
        self.epoch = f"passive-radar-simulation-{self.seed}"
        self.fir = signal.firwin(65, config.bandwidth_hz * .43, fs=config.sample_rate_hz)
        factor = config.decimation_factor
        self.truth = [
            {"delay_samples": max(8, config.range_bins // 3), "doppler_hz": min(80., config.max_doppler_hz * .4), "amplitude": .5},
            {"delay_samples": max(12, 2 * config.range_bins // 3), "doppler_hz": -min(120., config.max_doppler_hz * .6), "amplitude": .35},
        ]
        for target in self.truth:
            target["raw_delay_samples"] = int(target["delay_samples"]) * factor
            target["excess_path_m"] = float(target["raw_delay_samples"]) * C / config.sample_rate_hz

    def _noise(self, indices: np.ndarray, salt: int) -> np.ndarray:
        # Integer hashing is intentionally modulo 2**64 (no random-state or
        # call-boundary dependence). Independent salts separate receiver noise.
        with np.errstate(over="ignore"):
            key = np.uint64((self.seed + salt) & ((1 << 64) - 1)) * np.uint64(0x9e3779b97f4a7c15)
            value = indices.astype(np.uint64) ^ key
            value = (value ^ (value >> np.uint64(30))) * np.uint64(0xbf58476d1ce4e5b9)
            value = (value ^ (value >> np.uint64(27))) * np.uint64(0x94d049bb133111eb)
            value ^= value >> np.uint64(31)
        uniform = ((value & np.uint64(0xffffffff)).astype(np.float64) + .5) / 2**32
        phase = (value >> np.uint64(32)).astype(np.float64) * (2 * np.pi / 2**32)
        # Box-Muller: unit mean complex power, independent Gaussian I/Q.
        radius = np.sqrt(-np.log(uniform))
        return (radius * (np.cos(phase) + 1j * np.sin(phase))).astype(np.complex64)

    def frame(self, sample_start: int, count: int, timestamp: float | None = None) -> IQFrame:
        if sample_start < 0 or not 1 <= count <= MAX_SAMPLES_PER_CHANNEL:
            raise ValueError("Invalid simulated IQ frame bounds")
        maximum_delay = max(int(t["raw_delay_samples"]) for t in self.truth)
        indices = np.arange(sample_start - maximum_delay - len(self.fir) + 1,
                            sample_start + count, dtype=np.int64)
        source = signal.lfilter(self.fir, [1.], self._noise(indices, 7)).astype(np.complex64)
        start = maximum_delay + len(self.fir) - 1
        direct = source[start:start + count]
        absolute = np.arange(sample_start, sample_start + count, dtype=np.int64)
        iq = np.empty((5, count), dtype=np.complex64)
        for channel in range(5):
            iq[channel] = self._noise(absolute, 1000 + channel * 431) * .015
        iq[self.config.reference_channel] += direct
        surveillance = 2.5 * direct + .3 * source[start - 2:start - 2 + count]
        for target in self.truth:
            delay = int(target["raw_delay_samples"])
            phase = np.exp(2j * np.pi * float(target["doppler_hz"]) * absolute / self.config.sample_rate_hz)
            surveillance += float(target["amplitude"]) * source[start - delay:start - delay + count] * phase
        iq[self.config.surveillance_channel] += surveillance
        return IQFrame(iq, self.config.sample_rate_hz, self.config.center_frequency_hz,
                       sample_start, self.epoch,
                       self.started + sample_start / self.config.sample_rate_hz if timestamp is None else timestamp,
                       {"valid": True, "simulated": True, "delay_lock": True, "iq_lock": True,
                        "gains_db": [self.config.gain_db] * 5, "frame_type": 0,
                        "noise_source": False, "overdrive_flags": 0})
