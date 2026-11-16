"""Heimdall TCP and NMEA adapters, implemented from the public wire format.

Supported IQ protocol: header version 7, little-endian 64-bit Linux C layout,
post-synchronizer float32 IQ. A frame timestamp is the DAQ host's clock, not PPS.
No third-party implementation is imported or required for the IQ adapter.
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

HEADER_BYTES = 1024
SYNC_WORD = 0x2BF7B95A
MAX_SAMPLES_PER_CHANNEL = 4_194_304
MAX_PAYLOAD_BYTES = 192 * 1024 * 1024
MANUAL_GAINS_DB = (0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4,
                   15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7, 32.8,
                   33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5,
                   48.0, 49.6)


class HardwareError(RuntimeError):
    """A transport or unsupported protocol condition; reconnect is explicit."""


@dataclass
class IQFrame:
    iq: np.ndarray
    sample_rate_hz: float
    center_frequency_hz: float
    sample_start: int
    stream_epoch: str
    timestamp: float
    quality: dict[str, Any]


def parse_header(data: bytes) -> dict[str, Any]:
    """Decode a bounded v7 header using explicit offsets, independent of host ABI.

    The padding at offsets 36 and 68 is part of the upstream 64-bit Linux
    protocol. A big-endian or differently aligned sender is rejected rather
    than guessed. Five antennas are required by this application's DSP.
    """
    if len(data) != HEADER_BYTES:
        raise HardwareError("Heimdall header must contain exactly 1024 bytes")
    def u32(offset: int) -> int:
        return struct.unpack_from("<I", data, offset)[0]
    def u64(offset: int) -> int:
        return struct.unpack_from("<Q", data, offset)[0]
    if u32(0) != SYNC_WORD:
        raise HardwareError("Wrong Heimdall sync word / unsupported byte order")
    if u32(1020) != 7:
        raise HardwareError(f"Unsupported Heimdall header version {u32(1020)}; expected 7")
    kind, channels, count = u32(4), u32(28), u32(64)
    if kind not in (0, 1, 2, 3, 4):
        raise HardwareError(f"Unknown Heimdall frame type {kind}")
    if channels != 5:
        raise HardwareError(f"Five coherent antenna channels required, received {channels}")
    if count > MAX_SAMPLES_PER_CHANNEL:
        raise HardwareError("Heimdall CPI exceeds the configured allocation limit")
    if kind == 1 and count != 0:
        raise HardwareError("A DUMMY frame must have zero samples")
    if kind != 1 and (u32(100) != 32 or u32(96) != 3):
        raise HardwareError("Expected post-Heimdall complex float32 IQ (data_type=3, depth=32)")
    rate, center = u64(56), u64(40)
    if not 1 <= rate <= 10_000_000:
        raise HardwareError("Implausible Heimdall output sample rate")
    if not 1_000_000 <= center <= 3_000_000_000:
        raise HardwareError("Implausible Heimdall RF center frequency")
    payload = count * channels * 8
    if payload > MAX_PAYLOAD_BYTES:
        raise HardwareError("Heimdall payload exceeds the configured allocation limit")
    return {
        "frame_type": kind, "channels": channels, "sample_count": count,
        "sample_rate_hz": rate, "center_frequency_hz": center,
        "adc_sample_rate_hz": u64(48), "timestamp_ms": u64(72),
        "daq_block_index": u32(80), "cpi_index": u32(84),
        "data_type": u32(96), "sample_bit_depth": u32(100),
        "overdrive_flags": u32(104),
        "gains_db": [u32(108 + 4 * i) / 10.0 for i in range(channels)],
        "delay_lock": bool(u32(236)), "iq_lock": bool(u32(240)),
        "sync_state": u32(244), "noise_source": bool(u32(248)),
        "header_version": 7, "payload_bytes": payload,
        "hardware_id": data[8:24].split(b"\0", 1)[0].decode("ascii", "replace"),
    }


class HeimdallClient:
    """One IQ stream, with bounded exact reads and explicit discontinuities.

    The sender accepts one IQ client. Disable the upstream DoA consumer or use
    a separate DAQ instance rather than opening competing clients to port 5000.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 5000,
                 timeout: float = 5, control_port: int = 5001):
        self.host, self.port = host, int(port)
        self.control_port, self.timeout = int(control_port), float(timeout)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._previous: dict[str, Any] | None = None
        self._previous_valid = False
        self._next_sample = 0
        self._epoch = uuid.uuid4().hex
        self._read_lock = asyncio.Lock()
        self._control_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
            self._writer.write(b"streaming")
            await asyncio.wait_for(self._writer.drain(), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise HardwareError(f"Cannot connect to Heimdall IQ at {self.host}:{self.port}: {exc}") from exc
        self._previous, self._previous_valid = None, False
        self._epoch, self._next_sample = uuid.uuid4().hex, 0

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.timeout)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _exact(self, count: int) -> bytes:
        if self._reader is None:
            raise HardwareError("Heimdall IQ stream is not connected")
        try:
            return await asyncio.wait_for(self._reader.readexactly(count), self.timeout)
        except asyncio.IncompleteReadError as exc:
            raise HardwareError(f"Heimdall disconnected during frame ({len(exc.partial)}/{count} bytes)") from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise HardwareError(f"Heimdall frame read failed: {type(exc).__name__}") from exc

    async def read_frame(self) -> IQFrame:
        async with self._read_lock:
            try:
                return await self._read_frame()
            except (HardwareError, OSError):
                await self.close()  # Do not reuse a partially consumed frame.
                raise

    async def _read_frame(self) -> IQFrame:
        if not self.connected:
            raise HardwareError("Connect Heimdall before reading IQ frames")
        header = parse_header(await self._exact(HEADER_BYTES))
        raw = await self._exact(header["payload_bytes"]) if header["payload_bytes"] else b""
        iq = np.frombuffer(raw, dtype="<c8").reshape(5, header["sample_count"]).copy()
        reasons: list[str] = []
        kind = header["frame_type"]
        if kind != 0:
            reasons.append({1: "dummy_frame", 2: "ramp_frame", 3: "calibration_frame", 4: "trigger_wait_frame"}[kind])
        if not header["delay_lock"]:
            reasons.append("delay_not_locked")
        if not header["iq_lock"]:
            reasons.append("iq_not_locked")
        if header["overdrive_flags"]:
            reasons.append("adc_overdrive")
        if header["noise_source"]:
            reasons.append("calibration_noise_active")
        if header["sample_count"] == 0:
            reasons.append("empty_frame")
        if not np.isfinite(iq).all():
            reasons.append("nonfinite_iq")
        discontinuities = []
        previous = self._previous
        if previous is not None:
            if header["cpi_index"] != ((previous["cpi_index"] + 1) & 0xFFFFFFFF):
                discontinuities.append("cpi_index_gap")
            if header["sample_rate_hz"] != previous["sample_rate_hz"]:
                discontinuities.append("sample_rate_changed")
            if header["center_frequency_hz"] != previous["center_frequency_hz"]:
                discontinuities.append("frequency_changed")
            if header["gains_db"] != previous["gains_db"]:
                discontinuities.append("gain_changed")
            if header["timestamp_ms"] < previous["timestamp_ms"]:
                discontinuities.append("host_time_went_backwards")
            if not self._previous_valid:
                discontinuities.append("recovered_after_invalid_frame")
        valid = not reasons
        if discontinuities or not valid:
            self._epoch, self._next_sample = uuid.uuid4().hex, 0
        start = self._next_sample
        if valid:
            self._next_sample += header["sample_count"]
        quality = {
            **header, "valid": valid, "reasons": reasons,
            "discontinuity": bool(discontinuities) or not valid,
            "discontinuity_reasons": discontinuities,
            "timestamp_source": "daq_host_clock_ms_not_gps_pps",
            "timestamp_semantics": "estimated_first_sample_from_frame_end",
            "header_timestamp_semantics": "approximate_frame_end_with_host_usb_latency",
        }
        self._previous, self._previous_valid = header, valid
        assert self._writer is not None
        self._writer.write(b"IQDownload")
        await asyncio.wait_for(self._writer.drain(), self.timeout)
        return IQFrame(iq=iq, sample_rate_hz=float(header["sample_rate_hz"]),
                       center_frequency_hz=float(header["center_frequency_hz"]),
                       sample_start=start, stream_epoch=self._epoch,
                       timestamp=header["timestamp_ms"] / 1000.0 - header["sample_count"] / header["sample_rate_hz"],
                       quality=quality)

    async def configure(self, center_frequency_hz: float | None = None,
                        gain_db: float | None = None) -> dict[str, Any]:
        """Send documented FREQ/GAIN commands, not sample-rate reconfiguration.

        FNSD acknowledges the controller. Only subsequent IQ headers establish
        the observed frequency/gain; retuning requires coherent re-lock.
        """
        commands: list[bytes] = []
        if center_frequency_hz is not None:
            if not math.isfinite(center_frequency_hz) or not 24e6 <= center_frequency_hz <= 1766e6:
                raise ValueError("RF center frequency must be between 24 and 1766 MHz")
            commands.append(b"FREQ" + struct.pack("<Q", round(center_frequency_hz)))
        if gain_db is not None:
            if not math.isfinite(gain_db) or not 0 <= gain_db <= 49.6:
                raise ValueError("Manual gain must be between 0 and 49.6 dB")
            if not any(abs(gain_db - gain) < 1e-6 for gain in MANUAL_GAINS_DB):
                nearest = min(MANUAL_GAINS_DB, key=lambda gain: abs(gain_db - gain))
                raise ValueError(f"Unsupported discrete manual gain {gain_db:g} dB; nearest supported value is {nearest:g} dB")
            commands.append(b"GAIN" + struct.pack("<5I", *([round(gain_db * 10)] * 5)))
        if not commands:
            return {"acknowledged": False, "reason": "No hardware settings supplied"}
        async with self._control_lock:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.control_port), self.timeout)
                for command in commands:
                    writer.write(command.ljust(128, b"\0"))
                    await asyncio.wait_for(writer.drain(), self.timeout)
                    answer = await asyncio.wait_for(reader.readexactly(128), self.timeout)
                    if answer[:4] != b"FNSD":
                        raise HardwareError(f"Heimdall controller rejected {command[:4].decode('ascii')}")
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                raise HardwareError(f"Heimdall control failed at {self.host}:{self.control_port}: {type(exc).__name__}") from exc
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), self.timeout)
                    except (OSError, asyncio.TimeoutError):
                        pass
        return {"acknowledged": True, "center_frequency_hz": center_frequency_hz,
                "gain_db": gain_db, "requires_frame_verification": True,
                "message": "Controller acknowledged; verify actual values and coherent lock in subsequent IQ headers"}


def _coordinate(value: str, hemisphere: str, latitude: bool) -> float:
    width = 2 if latitude else 3
    degrees = int(value[:width])
    minutes = float(value[width:])
    if not 0 <= minutes < 60 or hemisphere not in (("N", "S") if latitude else ("E", "W")):
        raise ValueError("Invalid NMEA coordinate")
    result = degrees + minutes / 60
    if result > (90 if latitude else 180):
        raise ValueError("NMEA coordinate outside valid range")
    return -result if hemisphere in ("S", "W") else result


def parse_nmea(sentence: str) -> dict[str, Any] | None:
    """Parse checked RMC/GGA sentences; invalid fixes remain explicit events.

    ``received_at`` is this process's receipt time. ``age_seconds`` from a GPS
    receiver is not present in NMEA; callers must age the fix after receipt.
    """
    text = sentence.strip()
    if not text.startswith("$") or "*" not in text:
        return None
    body, checksum_text = text[1:].rsplit("*", 1)
    if len(checksum_text) != 2:
        return None
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    try:
        if checksum != int(checksum_text, 16):
            return None
        fields = body.split(",")
        kind = fields[0][-3:]
        result: dict[str, Any] = {"sentence_type": kind, "received_at": time.time(),
                                  "utc_time": fields[1], "valid": False,
                                  "source": "nmea_serial"}
        if kind == "RMC" and len(fields) >= 10:
            result["valid"] = fields[2] == "A"
            result["utc_date"] = fields[9]
            result["speed_knots"] = float(fields[7]) if fields[7] else None
            result["course_deg"] = float(fields[8]) if fields[8] else None
            if fields[3] and fields[5]:
                result["latitude"] = _coordinate(fields[3], fields[4], True)
                result["longitude"] = _coordinate(fields[5], fields[6], False)
        elif kind == "GGA" and len(fields) >= 10:
            result["fix_quality"] = int(fields[6] or "0")
            result["valid"] = result["fix_quality"] > 0
            result["satellites"] = int(fields[7] or "0")
            result["hdop"] = float(fields[8]) if fields[8] else None
            result["altitude_m"] = float(fields[9]) if fields[9] else None
            if fields[2] and fields[4]:
                result["latitude"] = _coordinate(fields[2], fields[3], True)
                result["longitude"] = _coordinate(fields[4], fields[5], False)
        else:
            return None
        if result["valid"] and ("latitude" not in result or "longitude" not in result):
            return None
        for value in result.values():
            if isinstance(value, float) and not math.isfinite(value):
                return None
        return result
    except (ValueError, IndexError, OverflowError):
        return None


class GPSReader:
    """Optional USB-serial NMEA reader. ``pip install pyserial`` enables it."""
    def __init__(self, port: str, baud: int = 9600):
        self.port, self.baud = port, int(baud)
        self._serial: Any = None
        self.latest: dict[str, Any] | None = None

    async def connect(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise HardwareError("GPS serial support requires pyserial: install the project's GPS extra") from exc
        try:
            self._serial = await asyncio.to_thread(serial.Serial, self.port, self.baud, timeout=1)
        except (OSError, serial.SerialException) as exc:
            raise HardwareError(f"Cannot open GPS serial port {self.port}: {exc}") from exc

    async def read_fix(self) -> dict[str, Any] | None:
        if self._serial is None:
            raise HardwareError("Connect GPS reader before reading fixes")
        raw = await asyncio.to_thread(self._serial.readline)
        parsed = parse_nmea(raw.decode("ascii", "replace"))
        if parsed is not None:
            previous = self.latest or {}
            # Merge only supplementary data; position validity always follows
            # the latest receiver sentence, including explicit loss of fix.
            self.latest = {**previous, **parsed}
        return parsed

    def status(self) -> dict[str, Any]:
        if self.latest is None:
            return {"connected": self._serial is not None, "valid": False, "reason": "No GPS fix received"}
        age = max(0.0, time.time() - self.latest["received_at"])
        return {**self.latest, "connected": self._serial is not None,
                "age_seconds": age, "valid": bool(self.latest["valid"] and age < 5)}

    async def close(self) -> None:
        port, self._serial = self._serial, None
        if port is not None:
            await asyncio.to_thread(port.close)
