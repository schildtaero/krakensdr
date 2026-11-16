"""Burst-oriented BFT decoders with RF-sample-relative provenance.

The caller owns the continuous five-channel ring buffer and gives a *single*
RF channel to these demodulators.  Returned indexes always refer to the input
IQ array, never to the wall clock when decoding happened.  POCSAG decoding is
implemented here from its BCH, framing and FSK definitions; no upstream
SimpleSDR or BFT receiver implementation is embedded.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import math
import re
from typing import Any

import numpy as np
from scipy.signal import resample_poly


@dataclass(frozen=True)
class DecodedPacket:
    payload: str
    modulation: str
    sample_start: int
    sample_end: int
    quality: dict[str, Any]


_CALL = r"(?P<callsign>[A-Z0-9][A-Z0-9/\-]{0,8})"
_POSITION = re.compile(
    rf"^{_CALL}\s+(?P<latitude>[+-]?\d{{1,3}}\.\d{{1,8}})\s+"
    r"(?P<longitude>[+-]?\d{1,3}\.\d{1,8})\s+"
    r"(?P<altitude>[+-]?\d{1,6})m\s+"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})Z\s+"
    r"(?P<satellites>\d{1,2})sat$",
    re.IGNORECASE,
)
_NO_POSITION = re.compile(
    rf"^{_CALL}\s+(?:NO GPS Test|TESTmessageLoRaTST)$", re.IGNORECASE
)


def parse_bft_payload(text: str) -> dict[str, Any] | None:
    """Parse the actual T-Deck text envelope, including its no-GPS test mode.

    ``position_valid`` indicates a complete, syntactically plausible coordinate
    message.  The message does not carry fix age or HDOP, so this flag is *not*
    an independent assertion of a current or sufficiently accurate GPS fix.
    UTC is deliberately returned without an invented date.
    """
    if not isinstance(text, str) or not 1 <= len(text) <= 160:
        return None
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        return None
    value = text.strip()
    match = _POSITION.fullmatch(value)
    if match is None:
        no_fix = _NO_POSITION.fullmatch(value)
        if no_fix is None:
            return None
        return {
            "callsign": no_fix["callsign"].upper(),
            "latitude": None,
            "longitude": None,
            "altitude_m": None,
            "position_utc": None,
            "satellites": None,
            "position_valid": False,
        }
    latitude = float(match["latitude"])
    longitude = float(match["longitude"])
    altitude = float(match["altitude"])
    hour, minute, second = (int(match[k]) for k in ("hour", "minute", "second"))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if not (-1000 <= altitude <= 100000):
        return None
    # The firmware emits ordinary UTC seconds; do not infer leap-second support.
    if hour > 23 or minute > 59 or second > 59:
        return None
    return {
        "callsign": match["callsign"].upper(),
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": altitude,
        "position_utc": f"{hour:02d}{minute:02d}{second:02d}Z",
        "satellites": int(match["satellites"]),
        "position_valid": True,
    }


_POCSAG_SYNC = 0x7CD215D8
_POCSAG_IDLE = 0x7A89C197
_BCH_GENERATOR = 0x769  # x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1


def _bch_remainder(value: int) -> int:
    while value.bit_length() >= 11:
        value ^= _BCH_GENERATOR << (value.bit_length() - 11)
    return value


def _syndrome(word: int) -> int:
    """Syndrome of the *extended* BCH(32,21,6), including overall parity."""
    return (_bch_remainder(word >> 1) << 1) | (word.bit_count() & 1)


@lru_cache(maxsize=1)
def _error_patterns() -> dict[int, tuple[int, int]]:
    # Distance six guarantees unique syndromes for all zero, one and two-bit
    # error patterns. Including parity avoids accepting a three-bit error as a
    # supposedly repaired two-bit word.
    table = {0: (0, 0)}
    for first in range(32):
        mask = 1 << first
        table[_syndrome(mask)] = (mask, 1)
        for second in range(first):
            mask = (1 << first) | (1 << second)
            table[_syndrome(mask)] = (mask, 2)
    return table


def _repair_word(word: int) -> tuple[int, int] | None:
    pattern = _error_patterns().get(_syndrome(word))
    if pattern is None:
        return None
    fixed = word ^ pattern[0]
    return (fixed, pattern[1]) if _syndrome(fixed) == 0 else None


def _make_word(information: int) -> int:
    """Encode one 21-bit information word; used by reproducible test fixtures."""
    if not 0 <= information < 1 << 21:
        raise ValueError("POCSAG information must fit in 21 bits")
    unextended = (information << 10) | _bch_remainder(information << 10)
    return (unextended << 1) | (unextended.bit_count() & 1)


def encode_pocsag_bits(
    payload: str, ric: int = 1000000, function: int = 3, preamble_bits: int = 576
) -> np.ndarray:
    """Create standard alphanumeric POCSAG bits for tests/demo fixtures.

    This is an encoder, not evidence of RF interoperability. The function is
    intentionally independent of the external tracker source tree.
    """
    if not payload or len(payload) > 512 or any(not 32 <= ord(c) <= 126 for c in payload):
        raise ValueError("POCSAG payload must contain 1..512 printable ASCII characters")
    if not 0 <= ric < 1 << 21 or not 0 <= function <= 3:
        raise ValueError("Invalid POCSAG address or function")
    if preamble_bits < 0:
        raise ValueError("Preamble length must be nonnegative")
    words = [_POCSAG_IDLE] * (2 * (ric & 7))
    address = ((ric >> 3) << 2) | function
    words.append(_make_word(address))
    text_bits = [((ord(char) >> bit) & 1) for char in payload for bit in range(7)]
    text_bits.extend([0] * ((-len(text_bits)) % 20))
    for start in range(0, len(text_bits), 20):
        value = 0
        for bit in text_bits[start : start + 20]:
            value = (value << 1) | bit
        words.append(_make_word((1 << 20) | value))
    words.extend([_POCSAG_IDLE] * ((-len(words)) % 16))
    output = [1 - (i & 1) for i in range(preamble_bits)]
    for batch in range(0, len(words), 16):
        for word in [_POCSAG_SYNC, *words[batch : batch + 16]]:
            output.extend((word >> i) & 1 for i in range(31, -1, -1))
    return np.asarray(output, dtype=np.uint8)


def synthesize_pocsag(
    payload: str,
    sample_rate_hz: float = 48000,
    *,
    ric: int = 1000000,
    invert: bool = False,
    frequency_offset_hz: float = 0.0,
    noise_std: float = 0.02,
    padding_seconds: float = 0.04,
    seed: int = 1,
) -> np.ndarray:
    """Generate a phase-continuous 1200-baud FSK fixture, explicitly synthetic."""
    if not math.isfinite(sample_rate_hz) or sample_rate_hz < 16000:
        raise ValueError("Sample rate must be finite and at least 16 kHz")
    bits = encode_pocsag_bits(payload, ric=ric)
    count = int(math.ceil(len(bits) * sample_rate_hz / 1200))
    bit_indices = np.minimum(
        (np.arange(count) * 1200 / sample_rate_hz).astype(np.int64), len(bits) - 1
    )
    levels = 2 * bits[bit_indices].astype(float) - 1
    if invert:
        levels *= -1
    frequencies = frequency_offset_hz + 4500 * levels
    burst = np.exp(2j * np.pi * np.cumsum(frequencies) / sample_rate_hz)
    padding = np.zeros(int(round(padding_seconds * sample_rate_hz)), dtype=complex)
    iq = np.concatenate((padding, burst, padding))
    if noise_std:
        generator = np.random.default_rng(seed)
        iq += noise_std * (
            generator.standard_normal(iq.size) + 1j * generator.standard_normal(iq.size)
        )
    return iq.astype(np.complex64)


def _sync_positions(bits: np.ndarray) -> list[int]:
    if bits.size < 32:
        return []
    size = bits.size - 31
    windows = np.zeros(size, dtype=np.uint32)
    for shift in range(32):
        windows = (windows << np.uint32(1)) | bits[shift : shift + size].astype(np.uint32)
    differences = windows ^ np.uint32(_POCSAG_SYNC)
    # np.bitwise_count was added after some supported NumPy releases.
    if hasattr(np, "bitwise_count"):
        distance = np.bitwise_count(differences)
    else:
        distance = np.fromiter((int(x).bit_count() for x in differences), np.uint8, size)
    return np.flatnonzero(distance <= 2).tolist()


def _read_word(bits: np.ndarray, start: int) -> int:
    result = 0
    for bit in bits[start : start + 32]:
        result = (result << 1) | int(bit)
    return result


def _decode_alphanumeric(bit_values: list[int]) -> str | None:
    chars: list[str] = []
    for at in range(0, len(bit_values) - 6, 7):
        number = sum(bit_values[at + shift] << shift for shift in range(7))
        if number in (0, 3, 4):  # zero padding / ETX / EOT
            break
        if number not in (10, 13) and not 32 <= number <= 126:
            return None
        chars.append(chr(number))
    text = "".join(chars).strip()
    return text if 3 <= len(text) <= 512 else None


def _preamble_start(bits: np.ndarray, sync_at: int) -> tuple[int, bool]:
    if sync_at < 64:
        return sync_at, False
    count = min(576, sync_at)
    segment = bits[sync_at - count : sync_at]
    alternation = np.arange(count, dtype=np.uint8) & 1
    agreement = max(float(np.mean(segment == alternation)), float(np.mean(segment != alternation)))
    return (sync_at - count, True) if agreement >= 0.94 else (sync_at, False)


def _decode_bits(bits: np.ndarray) -> list[dict[str, Any]]:
    """Frame-aware POCSAG parser. Never join text across distinct addresses."""
    packets: list[dict[str, Any]] = []
    for first_sync in _sync_positions(bits):
        preamble_start, preamble_found = _preamble_start(bits, first_sync)
        active: dict[str, Any] | None = None
        slot = 0
        at = first_sync + 32
        batches = 1
        address_seen = False

        def finish(reason: str) -> None:
            nonlocal active
            if active is not None and not active["invalid"] and active["data"]:
                text = _decode_alphanumeric(active["data"])
                if text is not None:
                    packets.append({
                        "payload": text,
                        "bit_start": active["start"],
                        "bit_end": active["end"],
                        "ric": active["ric"],
                        "function": active["function"],
                        "corrected_bits": active["corrected"],
                        "codeword_count": active["codewords"],
                        "sync_bit_start": first_sync,
                        "preamble_detected": preamble_found,
                        "termination": reason,
                    })
            active = None

        while at + 32 <= bits.size and batches <= 64:
            raw = _read_word(bits, at)
            if slot == 16:
                if (raw ^ _POCSAG_SYNC).bit_count() > 2:
                    finish("end_of_batch_stream")
                    break
                slot = 0
                batches += 1
                at += 32
                continue
            repaired = _repair_word(raw)
            if repaired is None:
                if active is not None:
                    active["invalid"] = True
            else:
                word, corrections = repaired
                if word == _POCSAG_IDLE:
                    finish("idle")
                elif not word & 0x80000000:
                    finish("next_address")
                    information = word >> 11
                    active = {
                        "start": preamble_start if not address_seen else at,
                        "end": at + 32,
                        "data": [],
                        "ric": ((information >> 2) << 3) | (slot // 2),
                        "function": information & 3,
                        "corrected": corrections,
                        "codewords": 1,
                        "invalid": False,
                    }
                    address_seen = True
                elif active is not None:
                    data_word = (word >> 11) & 0xFFFFF
                    active["data"].extend((data_word >> shift) & 1 for shift in range(19, -1, -1))
                    active["end"] = at + 32
                    active["corrected"] += corrections
                    active["codewords"] += 1
                    if len(active["data"]) > 7 * 512 + 20:
                        active["invalid"] = True
            slot += 1
            at += 32
        else:
            finish("end_of_capture")
    return packets


def _validate_iq(iq: np.ndarray, sample_rate_hz: float) -> np.ndarray:
    x = np.asarray(iq)
    if x.ndim != 1:
        raise ValueError("Decoder input must be one RF channel, a one-dimensional IQ array")
    if not np.iscomplexobj(x):
        raise ValueError("Decoder input must contain complex IQ samples")
    if not math.isfinite(sample_rate_hz) or not 12000 <= sample_rate_hz <= 10_000_000:
        raise ValueError("Invalid decoder sample rate (supported: 12 kHz..10 MHz)")
    if not np.all(np.isfinite(x)):
        raise ValueError("IQ contains NaN or infinity")
    return x


def decode_pocsag(iq: np.ndarray, sample_rate_hz: float) -> list[DecodedPacket]:
    """Decode 1200-baud, ±4.5 kHz POCSAG from one isolated RF-channel capture.

    Both FSK polarities and twenty clock phases are considered. Extended BCH
    correction repairs up to two errors per codeword and includes even parity.
    A damaged word invalidates its message rather than silently deleting bits.
    No payload CRC exists in POCSAG; callers must still validate the BFT text.
    """
    x = _validate_iq(iq, sample_rate_hz)
    if x.size < sample_rate_hz * 0.04 or not np.any(x):
        return []
    ratio = Fraction(24000 / sample_rate_hz).limit_denominator(8192)
    reduced = resample_poly(x, ratio.numerator, ratio.denominator)
    reduced_rate = sample_rate_hz * ratio.numerator / ratio.denominator
    if reduced.size < 64:
        return []
    products = reduced[1:] * reduced[:-1].conj()
    frequency = np.angle(products).astype(float) * (reduced_rate / (2 * np.pi))
    amplitudes = np.sqrt(np.abs(products))
    # Avoid zero padding and very weak noise when estimating the two FSK levels.
    amplitude_cutoff = max(float(np.quantile(amplitudes, 0.9)) * 0.3, 1e-12)
    useful = frequency[amplitudes >= amplitude_cutoff]
    if useful.size < 32:
        return []
    low, high = np.quantile(useful, [0.2, 0.8])
    for _ in range(12):
        threshold = (low + high) / 2
        below, above = useful[useful < threshold], useful[useful >= threshold]
        if below.size == 0 or above.size == 0:
            break
        next_low, next_high = np.mean(below), np.mean(above)
        if abs(next_low - low) + abs(next_high - high) < 0.1:
            low, high = next_low, next_high
            break
        low, high = next_low, next_high
    threshold = float((low + high) / 2)
    samples_per_bit = reduced_rate / 1200
    integral = np.concatenate(([0.0], np.cumsum(frequency)))
    integral_times = np.arange(integral.size)
    candidates: list[DecodedPacket] = []
    for phase in np.arange(20) * samples_per_bit / 20:
        edges = np.arange(phase, len(frequency), samples_per_bit)
        if edges.size < 34:
            continue
        integrals = np.interp(edges, integral_times, integral)
        means = np.diff(integrals) / samples_per_bit
        hard_bits = (means > threshold).astype(np.uint8)
        eye_opening = float(np.median(np.abs(means - threshold)))
        for inverted in (False, True):
            bits = 1 - hard_bits if inverted else hard_bits
            for result in _decode_bits(bits):
                start = int(round((phase + result["bit_start"] * samples_per_bit + 0.5) * sample_rate_hz / reduced_rate))
                end = int(round((phase + result["bit_end"] * samples_per_bit + 0.5) * sample_rate_hz / reduced_rate))
                quality = {
                    "valid": True,
                    "bch_ok": True,
                    "parity_ok": True,
                    "crc_present": False,
                    "corrected_bits": result["corrected_bits"],
                    "codeword_count": result["codeword_count"],
                    "ric": result["ric"],
                    "function": result["function"],
                    "baud": 1200,
                    "inverted": inverted,
                    "preamble_detected": result["preamble_detected"],
                    "termination": result["termination"],
                    "clock_phase_samples": float(phase),
                    "eye_opening_hz": eye_opening,
                    "estimated_offset_hz": threshold,
                    "estimated_fsk_separation_hz": float(high - low),
                    "bft_payload_valid": parse_bft_payload(result["payload"]) is not None,
                }
                packet = DecodedPacket(
                    result["payload"], "pocsag", max(0, start), min(x.size, end), quality
                )
                # Different clock phases find the same message. Preserve the
                # best-codeword candidate, not an arbitrary last processing time.
                for index, previous in enumerate(candidates):
                    overlap = min(packet.sample_end, previous.sample_end) - max(packet.sample_start, previous.sample_start)
                    same = (
                        previous.payload == packet.payload
                        and previous.quality["ric"] == packet.quality["ric"]
                        and overlap > 0
                    )
                    if same:
                        rank = (-packet.quality["corrected_bits"], packet.quality["preamble_detected"], packet.quality["eye_opening_hz"])
                        old_rank = (-previous.quality["corrected_bits"], previous.quality["preamble_detected"], previous.quality["eye_opening_hz"])
                        if rank > old_rank:
                            candidates[index] = packet
                        break
                else:
                    candidates.append(packet)
    return sorted(candidates, key=lambda packet: (packet.sample_start, packet.sample_end))


def decode_lora(
    iq: np.ndarray,
    sample_rate_hz: float,
    sf: int = 9,
    bandwidth_hz: float = 125000,
) -> list[DecodedPacket]:
    """Decode a LoRa burst using the independent PHY engine when available."""
    x = _validate_iq(iq, sample_rate_hz)
    from .lora_phy import decode_lora_iq

    results = decode_lora_iq(x, sample_rate_hz, sf=sf, bandwidth_hz=bandwidth_hz)
    output: list[DecodedPacket] = []
    for result in results:
        payload = result["payload"]
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("ascii")
            except UnicodeDecodeError:
                continue
        quality = dict(result.get("quality", {}))
        # A successful header or printable payload alone does not establish
        # packet validity and must not associate a position to a bearing.
        if not quality.get("header_ok") or not quality.get("crc_ok"):
            continue
        start = int(result["sample_start"])
        end = int(result["sample_end"])
        if not 0 <= start < end <= x.size:
            continue
        quality["valid"] = True
        quality["bft_payload_valid"] = parse_bft_payload(payload) is not None
        output.append(DecodedPacket(
            payload, "lora", start, end, quality
        ))
    return output


def decoder_capabilities() -> dict[str, dict[str, Any]]:
    try:
        from .lora_phy import lora_capability
        lora = lora_capability()
    except ImportError:
        lora = {
            "available": False,
            "status": "unavailable",
            "message": "LoRa-PHY engine is not installed",
        }
    return {
        "pocsag": {
            "available": True,
            "status": "ready_for_hardware_validation",
            "hardware_validated": False,
            "baud": 1200,
            "deviation_hz": 4500,
            "bch": "extended BCH(32,21,6), up to 2 corrected bits per codeword",
            "message": "Synthetic tests; real 433-MHz/Kraken reception still needs validation",
        },
        "lora": lora,
    }
