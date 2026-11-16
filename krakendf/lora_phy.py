"""Independent, bounded LoRa explicit-header receiver for isolated IQ bursts.

No GNU Radio is required.  This is a hard-decision implementation of the
Semtech chirp, diagonal interleaver, shortened Hamming code, whitening LFSR,
and on-air CRC conventions.  Only CRC-verified packets are emitted.  Timing
indices always refer to the caller's IQ vector, never to processing time.

The synchronizer is intentionally labelled experimental until validated with
the user's 433-MHz hardware.  It searches preamble/SFD correlations instead of
requiring an energy threshold.  The caller should provide channelized IQ with
the entire packet and a little preceding context, not an arbitrary long RF
recording.  Upstream frame gaps must reset the surrounding burst buffer.
"""

from __future__ import annotations

from fractions import Fraction
import math

import numpy as np
from scipy.signal import fftconvolve, find_peaks, resample_poly


class UnsupportedLoRaError(ValueError):
    """An IQ format or PHY configuration outside this receiver's contract."""


def lora_capability() -> dict:
    return {
        "available": True,
        "status": "experimental",
        "backend": "numpy-scipy-hard-decision",
        "spreading_factors": [7, 8, 9, 10, 11, 12],
        "bandwidths_hz": [125000, 250000, 500000],
        "explicit_header": True,
        "crc_required": True,
        "hardware_validated": False,
        "limitations": [
            "Requires a complete isolated burst with preamble and SFD.",
            "No guaranteed sensitivity or minimum SNR.",
            "Implicit headers and CRC-free packets are not emitted.",
            "No 433-MHz Kraken hardware validation yet.",
        ],
    }


def _check(sf: int, bandwidth_hz: float, sample_rate_hz: float) -> None:
    if sf not in range(7, 13):
        raise UnsupportedLoRaError("LoRa spreading factor must be 7 through 12")
    if bandwidth_hz not in (125000, 250000, 500000):
        raise UnsupportedLoRaError("Supported LoRa bandwidths: 125/250/500 kHz")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz < bandwidth_hz:
        raise UnsupportedLoRaError("IQ sample rate must cover the complete LoRa bandwidth")


def _chirp(sf: int, os: int = 2, symbol: int = 0) -> np.ndarray:
    """A unit-power cyclic upchirp; `os` samples per bandwidth interval."""
    chips = 1 << sf
    t = np.arange(chips * os, dtype=np.float64) / os
    # A cyclic frequency shift wraps at the band edge, preserving phase.
    cycles = t * (t / (2 * chips) - 0.5 + symbol / chips)
    cycles -= np.where(t >= chips - symbol, t, 0.0)
    return np.exp(2j * np.pi * cycles).astype(np.complex64)


def _whitening(length: int) -> np.ndarray:
    """LoRa's byte-wise x^8+x^6+x^5+x^4+1 whitening state, seed 0xff."""
    state = 255
    result = np.empty(length, dtype=np.uint8)
    for i in range(length):
        result[i] = state
        feedback = ((state >> 7) ^ (state >> 5) ^ (state >> 4) ^ (state >> 3)) & 1
        state = ((state << 1) | feedback) & 255
    return result


def _crc16(data: bytes) -> int:
    result = 0
    for octet in data:
        result ^= octet << 8
        for _ in range(8):
            result = ((result << 1) ^ (0x1021 if result & 0x8000 else 0)) & 65535
    return result


def _payload_crc(payload: bytes) -> int:
    # The silicon convention leaves the last two bytes outside the shift
    # register, then XORs them into its final state in network byte order.
    if len(payload) < 2:
        raise UnsupportedLoRaError("CRC validation requires at least two payload bytes")
    return _crc16(payload[:-2]) ^ (payload[-2] << 8) ^ payload[-1]


def _header_check(nibbles: list[int]) -> int:
    # The five checksum bits are linear parity checks on the 12 header bits.
    a, b, c = nibbles[:3]
    v = a | (b << 4) | (c << 8)
    terms = (
        (0, 1, 2, 3),
        (3, 7, 6, 5, 8),
        (2, 7, 4, 11, 9),
        (1, 6, 4, 10, 9, 8),
        (0, 5, 11, 10, 9, 8),
    )
    value = 0
    for positions in terms:
        value = (value << 1) | ((v & sum(1 << p for p in positions)).bit_count() & 1)
    return value


def _codeword(nibble: int, coding_rate: int) -> int:
    data = [(nibble >> i) & 1 for i in range(4)]
    if coding_rate == 1:
        parity = [sum(data) & 1]
    else:
        parity = [
            data[0] ^ data[1] ^ data[2],
            data[1] ^ data[2] ^ data[3],
            data[0] ^ data[1] ^ data[3],
            data[0] ^ data[2] ^ data[3],
        ][:coding_rate]
    value = 0
    for bit in data + parity:
        value = (value << 1) | bit
    return value


_CODEBOOK = {cr: [_codeword(n, cr) for n in range(16)] for cr in range(1, 5)}


def _unpack_block(bins: list[int], sf: int, cr: int, reduced: bool) -> tuple[list[int], int] | None:
    width = sf - 2 if reduced else sf
    rows = cr + 4
    if len(bins) != rows:
        return None
    words = [0] * width
    for time, bin_value in enumerate(bins):
        value = (int(bin_value) - 1) % (1 << sf)
        if reduced:
            value //= 4
        gray = value ^ (value >> 1)
        # A diagonal runs around the effective SF dimension, not CR+4.
        for col in range(width):
            word = (time - col - 1) % width
            bit = (gray >> (width - 1 - col)) & 1
            words[word] |= bit << (rows - 1 - time)
    decoded: list[int] = []
    errors = 0
    for word in words:
        distances = [(word ^ code).bit_count() for code in _CODEBOOK[cr]]
        best = min(distances)
        # CR4/5 and 4/6 detect errors; ambiguous words are never guessed.
        allowed = 1 if cr >= 3 else 0
        if best > allowed or distances.count(best) != 1:
            return None
        decoded.append(distances.index(best))
        errors += best
    return decoded, errors


def _symbol_peaks(iq: np.ndarray, start: int, count: int, sf: int) -> tuple[list[int], list[float]]:
    size = (1 << sf) * 2
    if start < 0 or start + count * size > len(iq):
        return [], []
    blocks = iq[start:start + count * size].reshape(count, size)
    # At an integer oversampling factor the two frequency aliases are
    # separated by N bins. Summing power preserves their common symbol ID.
    transformed = np.abs(np.fft.fft(blocks * np.conj(_chirp(sf)), axis=1)) ** 2
    folded = transformed[:, :size // 2] + transformed[:, size // 2:]
    maxima = np.argmax(folded, axis=1)
    concentration = folded[np.arange(count), maxima] / np.maximum(folded.sum(axis=1), 1e-20)
    return maxima.astype(int).tolist(), concentration.tolist()


def _decode_at(iq: np.ndarray, data_start: int, sf: int, bw: int) -> dict | None:
    first, header_strength = _symbol_peaks(iq, data_start, 8, sf)
    decoded = _unpack_block(first, sf, 4, True)
    if decoded is None:
        return None
    nibbles, corrected = decoded
    if len(nibbles) < 5:
        return None
    length = (nibbles[0] << 4) | nibbles[1]
    cr = nibbles[2] >> 1
    has_crc = bool(nibbles[2] & 1)
    received_check = ((nibbles[3] & 1) << 4) | nibbles[4]
    if received_check != _header_check(nibbles) or not 2 <= length <= 255 or cr not in range(1, 5) or not has_crc:
        return None
    payload_nibbles = nibbles[5:]
    required = 2 * (length + 2)
    reduced = (1 << sf) / bw >= 0.016
    width = sf - 2 if reduced else sf
    blocks = math.ceil(max(0, required - len(payload_nibbles)) / width)
    payload_bins, strengths = _symbol_peaks(iq, data_start + 8 * (1 << sf) * 2, blocks * (cr + 4), sf)
    if len(payload_bins) < blocks * (cr + 4):
        return None
    for block in range(blocks):
        unpacked = _unpack_block(payload_bins[block * (cr + 4):(block + 1) * (cr + 4)], sf, cr, reduced)
        if unpacked is None:
            return None
        extra, errors = unpacked
        payload_nibbles.extend(extra)
        corrected += errors
    octets = np.array([payload_nibbles[i] | (payload_nibbles[i + 1] << 4)
                       for i in range(0, required, 2)], dtype=np.uint8)
    octets[:length] ^= _whitening(length)
    payload = octets[:length].tobytes()
    received_crc = int(octets[length]) | (int(octets[length + 1]) << 8)
    if _payload_crc(payload) != received_crc:
        return None
    symbols = 8 + blocks * (cr + 4)
    return {
        "payload": payload,
        "sample_end_os2": data_start + symbols * (1 << sf) * 2,
        "quality": {
            "crc_ok": True,
            "header_ok": True,
            "coding_rate": f"4/{cr + 4}",
            "corrected_bits": corrected,
            "mean_symbol_concentration": float(np.mean(header_strength + strengths)),
            "experimental": True,
        },
    }


def _signed_bin(v: float, modulo: int) -> float:
    return (v + modulo / 2) % modulo - modulo / 2


def _sfd_candidates(iq: np.ndarray, sf: int) -> list[tuple[int, float]]:
    """Find preamble→downchirp transitions and derive coarse carrier offset."""
    chips = 1 << sf
    size = chips * 2
    if len(iq) < size * 10:
        return []
    # One-symbol grid always leaves at least one complete downchirp window
    # in the 2.25-symbol delimiter, regardless of the packet's alignment.
    count = len(iq) // size
    blocks = iq[:count * size].reshape(count, size)
    base = _chirp(sf)
    up_power = np.abs(np.fft.fft(blocks * base.conj(), axis=1)) ** 2
    down_power = np.abs(np.fft.fft(blocks * base, axis=1)) ** 2
    up = up_power[:, :chips] + up_power[:, chips:]
    down = down_power[:, :chips] + down_power[:, chips:]
    up_idx = np.argmax(up, axis=1)
    down_idx = np.argmax(down, axis=1)
    up_fraction = up.max(axis=1) / np.maximum(up.sum(axis=1), 1e-20)
    down_fraction = down.max(axis=1) / np.maximum(down.sum(axis=1), 1e-20)
    # Constant concentration would impose a different sensitivity at each SF.
    # This is a 12-dB peak-to-background trigger, followed by repeated-bin
    # agreement; it is not a calibrated RF-SNR gate.
    threshold = 16 / (chips + 15)
    results = []
    for index in range(6, count):
        if down_fraction[index] < threshold:
            continue
        earlier = list(range(max(0, index - 11), max(0, index - 2)))
        strong = [i for i in earlier if up_fraction[i] > threshold]
        if len(strong) < 4:
            continue
        votes: dict[int, list[int]] = {}
        for i in strong:
            votes.setdefault(int(up_idx[i]), []).append(i)
        group = max(votes.values(), key=len)
        if len(group) < 3:
            continue
        up_bin = int(up_idx[group[-1]])
        cfo_bin = _signed_bin((up_bin + int(down_idx[index])) / 2, chips // 2)
        # The lag between repeated preamble symbols resolves fractional CFO.
        pair = group[1]
        z0 = blocks[pair - 1]
        z1 = blocks[pair]
        fractional = np.angle(np.vdot(z0, z1)) / (2 * np.pi)
        cfo_bin = round(cfo_bin - fractional) + fractional
        results.append((index * size, float(cfo_bin)))
    return results[:16]


def detect_lora_preamble(iq: np.ndarray, sample_rate_hz: float, sf: int = 9,
                         bandwidth_hz: int = 125000) -> dict | None:
    """Correlative trigger for a repeated upchirp, including below-noise RF.

    This is deliberately not a packet decoder. The returned start/end span is
    a coarse preamble interval on the caller's sample clock. A surrounding
    ring buffer must preserve preceding samples and wait for the complete
    packet. `peak_background_db` describes dechirped FFT concentration, not
    a receiver-calibrated SNR. Four successive agreeing symbols are required.
    """
    _check(sf, bandwidth_hz, sample_rate_hz)
    signal = np.asarray(iq)
    if signal.ndim != 1 or not np.iscomplexobj(signal):
        raise UnsupportedLoRaError("LoRa requires one-dimensional complex IQ")
    if len(signal) > sample_rate_hz * 12:
        raise UnsupportedLoRaError("Preamble trigger accepts at most 12 seconds")
    if not len(signal) or not np.isfinite(signal).all():
        return None
    ratio = Fraction((2 * bandwidth_hz) / sample_rate_hz).limit_denominator(100000)
    samples = resample_poly(signal.astype(np.complex64, copy=False), ratio.numerator, ratio.denominator)
    chips = 1 << sf
    size = 2 * chips
    count = len(samples) // size
    if count < 4:
        return None
    chunks = samples[:count * size].reshape(count, size)
    power = np.abs(np.fft.fft(chunks * _chirp(sf).conj(), axis=1)) ** 2
    power = power[:, :chips] + power[:, chips:]
    bins = np.argmax(power, axis=1)
    peaks = power[np.arange(count), bins]
    background = (power.sum(axis=1) - peaks) / (chips - 1)
    ratio_db = 10 * np.log10(np.maximum(peaks, 1e-30) / np.maximum(background, 1e-30))
    for first in range(count - 3):
        chosen = bins[first:first + 4]
        distance = np.abs((chosen - chosen[0] + chips // 2) % chips - chips // 2)
        if np.max(distance) <= 1 and np.min(ratio_db[first:first + 4]) >= 12:
            factor = sample_rate_hz / (2 * bandwidth_hz)
            return {
                "sample_start": int(round(first * size * factor)),
                "sample_end": min(len(signal), int(round((first + 4) * size * factor))),
                "peak_background_db": float(np.median(ratio_db[first:first + 4])),
                "sf": sf,
                "bandwidth_hz": bandwidth_hz,
                "coarse_timing": True,
            }
    return None


def decode_lora_iq(iq: np.ndarray, sample_rate_hz: float, sf: int = 9,
                   bandwidth_hz: int = 125000) -> list[dict]:
    """Return only header- and CRC-valid explicit LoRa packets.

    Returned sample indices are bounded to the original input. A packet's
    start estimates the beginning of its eight-symbol preamble; decode/DF
    association should retain the enclosing caller-provided burst ID too.
    Vectors are limited to 12 seconds to bound synchronous DSP work.
    """
    _check(sf, bandwidth_hz, sample_rate_hz)
    signal = np.asarray(iq)
    if signal.ndim != 1 or not np.iscomplexobj(signal):
        raise UnsupportedLoRaError("LoRa requires a one-dimensional complex IQ vector")
    if len(signal) > sample_rate_hz * 12:
        raise UnsupportedLoRaError("Provide isolated LoRa bursts of at most 12 seconds")
    if len(signal) == 0 or not np.isfinite(signal).all():
        return []
    # Preserve enough oversampling for sub-chip SFD timing, independent of the
    # channelizer's original integer or rational output rate.
    target = bandwidth_hz * 2
    ratio = Fraction(target / sample_rate_hz).limit_denominator(100000)
    samples = resample_poly(signal.astype(np.complex64, copy=False), ratio.numerator, ratio.denominator)
    size = (1 << sf) * 2
    template = np.tile(np.conj(_chirp(sf)), 2)
    results: list[dict] = []
    already_until = -1
    for approximate, cfo_bins in _sfd_candidates(samples, sf):
        cfo_hz = cfo_bins * bandwidth_hz / (1 << sf)
        adjusted = samples * np.exp(-2j * np.pi * cfo_hz * np.arange(len(samples)) / target)
        start = max(0, approximate - 2 * size)
        stop = min(len(samples), approximate + 3 * size)
        region = samples[start:stop]
        phase = np.exp(-2j * np.pi * cfo_hz * np.arange(start, stop) / target)
        correlation = np.abs(fftconvolve(region * phase, np.conj(template[::-1]), mode="valid"))
        if not len(correlation):
            continue
        # Try several genuine local correlations, then narrow sample offsets.
        peaks, _ = find_peaks(correlation, distance=max(1, size // 8))
        order = peaks[np.argsort(correlation[peaks])[-3:]] if len(peaks) else np.array([int(np.argmax(correlation))])
        for peak in order[::-1]:
            sfd = start + int(peak)
            packet_start = max(0, sfd - 10 * size)
            if packet_start < already_until:
                continue
            base_start = sfd + int(2.25 * size)
            # A one-chip discrepancy is common at a resampler boundary. Header
            # checksum and payload CRC jointly decide, never syntax guessing.
            for delta in (0, -1, 1, -2, 2, -4, 4):
                data_start = base_start + delta
                decoded = _decode_at(adjusted, data_start, sf, bandwidth_hz)
                if decoded is None:
                    continue
                end = decoded.pop("sample_end_os2")
                factor = sample_rate_hz / target
                decoded["sample_start"] = max(0, int(round(packet_start * factor)))
                decoded["sample_end"] = min(len(signal), int(round(end * factor)))
                decoded["quality"].update(sf=sf, bandwidth_hz=bandwidth_hz, cfo_hz=cfo_hz)
                results.append(decoded)
                already_until = end
                break
            if already_until > sfd:
                break
    return results


def synthesize_lora(payload: bytes, sample_rate_hz: int = 250000, sf: int = 9,
                    bandwidth_hz: int = 125000, coding_rate: int = 1,
                    sync_word: int = 0x12, corrupt_crc: bool = False) -> np.ndarray:
    """Protocol test fixture generator; not a hardware-emulator claim."""
    _check(sf, bandwidth_hz, sample_rate_hz)
    if not 2 <= len(payload) <= 255 or coding_rate not in range(1, 5):
        raise UnsupportedLoRaError("Fixture payload length 2..255 and coding rate 1..4 required")
    raw = np.frombuffer(payload, dtype=np.uint8) ^ _whitening(len(payload))
    crc = _payload_crc(payload) ^ (1 if corrupt_crc else 0)
    octets = raw.tolist() + [crc & 255, crc >> 8]
    length = len(payload)
    header = [length >> 4, length & 15, (coding_rate << 1) | 1]
    check = _header_check(header)
    values = header + [check >> 4, check & 15]
    for octet in octets:
        values.extend([octet & 15, octet >> 4])
    symbols: list[int] = []
    first = True
    while values:
        reduced = first or (1 << sf) / bandwidth_hz >= 0.016
        width = sf - 2 if reduced else sf
        cr = 4 if first else coding_rate
        batch, values = values[:width], values[width:]
        batch += [0] * (width - len(batch))
        words = [_codeword(n, cr) for n in batch]
        for time in range(cr + 4):
            bits = [(words[(time - col - 1) % width] >> (cr + 3 - time)) & 1 for col in range(width)]
            if reduced:
                bits += [sum(bits) & 1, 0]
            value = 0
            for bit in bits:
                value = (value << 1) | bit
            inverse_gray = value
            for shift in range(1, sf):
                inverse_gray ^= value >> shift
            symbols.append((inverse_gray + 1) % (1 << sf))
        first = False
    base = _chirp(sf)
    parts = [base] * 8
    parts += [_chirp(sf, symbol=((sync_word >> 4) & 15) * 8), _chirp(sf, symbol=(sync_word & 15) * 8)]
    parts += [base.conj(), base.conj(), base.conj()[:len(base) // 4]]
    parts += [_chirp(sf, symbol=s) for s in symbols]
    result = np.concatenate(parts)
    ratio = Fraction(sample_rate_hz / (bandwidth_hz * 2)).limit_denominator(100000)
    return resample_poly(result, ratio.numerator, ratio.denominator).astype(np.complex64)
