"""Packet signal-to-background power estimate in the channelized RF band.

This is not calibrated RF SNR or LoRa's dechirped processing gain. A quiet
reference outside the captured activity is assumed to represent the packet's
noise/interference background. All five antennas contribute noncoherently,
so an unknown direction cannot cancel the signal in this measurement.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def _power(data: np.ndarray) -> float:
    return float(np.mean(np.abs(data) ** 2, dtype=np.float64))


def _noise_region(data: np.ndarray, block_length: int) -> tuple[float, bool]:
    """Check for burst contamination or changing noise in an idle region."""
    count = data.shape[1] // block_length
    power = _power(data)
    if count < 3:
        return power, True
    blocks = data[:, :count * block_length].reshape(5, count, block_length)
    powers = np.mean(np.abs(blocks) ** 2, axis=(0, 2), dtype=np.float64)
    positive = powers[powers > 0]
    if positive.size != powers.size:
        return power, False
    spread = float(np.quantile(positive, .8) / np.quantile(positive, .2))
    return power, spread <= 2.0


def estimate_packet_snr(iq: Any, signal_start: int, signal_end: int, *,
                        sample_rate_hz: float,
                        capture_quality: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate ``10 log10((packet_power - noise_power) / noise_power)``.

    ``signal_start``/``signal_end`` are decoder-relative sample indices in
    the full five-channel capture. Noise regions must also lie outside the
    capture detector's entire activity interval, when supplied. Two-ms
    guards avoid modulation/filter edges; each reference requires at least
    five ms and 64 samples. Inconsistent references make SNR unavailable.

    A capture baseline can substitute only when the caller explicitly sets
    ``noise_reference_valid=True``. The legacy detector's unqualified
    ``noise_power`` alone does not establish a quiet noise measurement.

    Negative dB estimates are valid. Nonpositive noise-subtracted packet
    power is unresolved, not ``0 dB`` or an infinite/fabricated SNR.
    """
    result: dict[str, Any] = {
        "snr_db": None,
        "snr_kind": "in_channel_power_ratio",
        "snr_status": "unavailable",
        "snr_noise_source": None,
        "snr_signal_power": None,
        "snr_noise_power": None,
        "snr_noise_samples": 0,
        "snr_total_power": None,
    }
    data = np.asarray(iq)
    quality = capture_quality or {}
    if (data.ndim != 2 or data.shape[0] != 5 or not np.iscomplexobj(data)
            or not np.isfinite(data).all()):
        result["snr_status"] = "invalid_iq"
        return result
    try:
        rate = float(sample_rate_hz)
        left, right = int(signal_start), int(signal_end)
    except (TypeError, ValueError, OverflowError):
        result["snr_status"] = "invalid_window"
        return result
    if (not math.isfinite(rate) or rate <= 0
            or left < 0 or right > data.shape[1] or right - left < 16):
        result["snr_status"] = "invalid_window"
        return result

    with np.errstate(over="ignore", invalid="ignore"):
        total = _power(data[:, left:right])
    if not math.isfinite(total):
        result["snr_status"] = "invalid_signal_power"
        return result
    result["snr_total_power"] = total
    guard = max(1, round(rate * .002))
    minimum = max(64, math.ceil(rate * .005))
    activity_left, activity_right = left, right
    try:
        start = int(quality.get("signal_start_offset", left))
        end = int(quality.get("signal_end_offset", right))
        if not 0 <= start <= end <= data.shape[1]:
            raise ValueError("Invalid capture bounds")
        activity_left = min(activity_left, start)
        activity_right = max(activity_right, end)
    except (TypeError, ValueError, OverflowError):
        # Unknown activity extent cannot certify a quiet in-capture region.
        activity_left, activity_right = 0, data.shape[1]

    regions = []
    prefix_end = max(0, activity_left - guard)
    suffix_start = min(data.shape[1], activity_right + guard)
    if prefix_end >= minimum:
        regions.append(("pretrigger", data[:, :prefix_end]))
    if data.shape[1] - suffix_start >= minimum:
        regions.append(("posttrigger", data[:, suffix_start:]))

    if regions:
        references = []
        for source, region in regions:
            power, stable = _noise_region(region, minimum)
            if not math.isfinite(power) or power <= 0:
                result["snr_status"] = "invalid_noise_reference"
                return result
            if not stable:
                result["snr_status"] = "unstable_noise_reference"
                return result
            references.append((source, power, region.shape[1]))
        powers = [item[1] for item in references]
        if max(powers) / min(powers) > 2.0:
            result["snr_status"] = "unstable_noise_reference"
            return result
        count = sum(item[2] for item in references)
        noise = sum(power * length for _, power, length in references) / count
        result["snr_noise_source"] = "+".join(item[0] for item in references)
        result["snr_noise_samples"] = count
    elif quality.get("noise_reference_valid") is True:
        try:
            noise = float(quality["noise_power"])
        except (KeyError, TypeError, ValueError, OverflowError):
            noise = math.nan
        if not math.isfinite(noise) or noise <= 0:
            result["snr_status"] = "invalid_noise_reference"
            return result
        result["snr_noise_source"] = "capture_baseline"
    else:
        result["snr_status"] = "no_noise_reference"
        return result

    result["snr_noise_power"] = noise
    signal_power = total - noise
    if signal_power <= 0:
        result["snr_signal_power"] = 0.0
        result["snr_status"] = "below_noise_resolution"
        return result
    result["snr_signal_power"] = signal_power
    result["snr_db"] = 10.0 * math.log10(signal_power / noise)
    result["snr_status"] = "estimated"
    return result
