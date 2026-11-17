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


def _nmea_fields(sentence: str) -> list[str] | None:
    """Return fields only for bounded ASCII NMEA with a valid checksum."""
    text = sentence.strip()
    if len(text) > 1024 or not text.startswith("$") or "*" not in text:
        return None
    body, checksum_text = text[1:].rsplit("*", 1)
    if len(checksum_text) != 2 or any(ord(char) > 127 for char in body):
        return None
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    try:
        if checksum != int(checksum_text, 16):
            return None
    except ValueError:
        return None
    fields = body.split(",")
    # Proprietary messages also demonstrate a working NMEA data stream, but
    # are not interpreted as receiver position or satellite measurements.
    name = fields[0]
    if not 3 <= len(name) <= 10 or not name.isalnum() or name.upper() != name:
        return None
    return fields


def _nmea_number(value: str, minimum: float, maximum: float) -> float | None:
    if value == "":
        return None
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("NMEA number outside supported range")
    return number


def parse_nmea(sentence: str) -> dict[str, Any] | None:
    """Parse checksummed RMC/GGA position and GSA/GSV satellite telemetry.

    Only RMC/GGA contain a ``valid`` position flag. Supplementary messages
    must neither establish a position nor refresh its receipt timestamp.
    """
    fields = _nmea_fields(sentence)
    if fields is None:
        return None
    try:
        name = fields[0]
        kind, talker = name[-3:], name[:-3]
        if len(talker) != 2:
            return None
        result: dict[str, Any] = {"sentence_type": kind, "talker": talker,
                                  "received_at": time.time(), "source": "nmea_serial"}
        if kind == "RMC" and len(fields) >= 10:
            if fields[2] not in ("A", "V"):
                return None
            result.update(utc_time=fields[1], valid=fields[2] == "A", utc_date=fields[9])
            result["speed_knots"] = _nmea_number(fields[7], 0, 100000)
            result["course_deg"] = _nmea_number(fields[8], 0, 360)
            if fields[3] and fields[5]:
                result["latitude"] = _coordinate(fields[3], fields[4], True)
                result["longitude"] = _coordinate(fields[5], fields[6], False)
        elif kind == "GGA" and len(fields) >= 10:
            quality = int(fields[6] or "0")
            if not 0 <= quality <= 8:
                return None
            used = int(fields[7]) if fields[7] else None
            if used is not None and not 0 <= used <= 99:
                return None
            result.update(utc_time=fields[1], fix_quality=quality, valid=quality > 0,
                          satellites=used, satellites_used=used)
            result["hdop"] = _nmea_number(fields[8], 0, 9999)
            result["altitude_m"] = _nmea_number(fields[9], -20000, 100000)
            if fields[2] and fields[4]:
                result["latitude"] = _coordinate(fields[2], fields[3], True)
                result["longitude"] = _coordinate(fields[4], fields[5], False)
        elif kind == "GSA" and len(fields) >= 18:
            fix_type = int(fields[2])
            if fields[1] not in ("A", "M") or fix_type not in (1, 2, 3):
                return None
            used_prns = [str(int(value)) for value in fields[3:15] if value]
            if any(not 1 <= int(prn) <= 999 for prn in used_prns):
                return None
            system_id = int(fields[18]) if len(fields) > 18 and fields[18] else None
            if system_id is not None and not 1 <= system_id <= 9:
                return None
            result.update(fix_type=fix_type, used_prns=used_prns, system_id=system_id,
                          pdop=_nmea_number(fields[15], 0, 9999),
                          hdop=_nmea_number(fields[16], 0, 9999),
                          vdop=_nmea_number(fields[17], 0, 9999))
        elif kind == "GSV" and len(fields) >= 4:
            total, part, count = map(int, fields[1:4])
            if not 1 <= total <= 64 or not 1 <= part <= total or not 0 <= count <= 256:
                return None
            items = fields[4:]
            signal_id = None
            if len(items) % 4 == 1:
                signal_id = int(items[-1]) if items[-1] else None
                if signal_id is not None and not 0 <= signal_id <= 99:
                    return None
                items = items[:-1]
            if len(items) % 4 or len(items) > 16:
                return None
            satellites = []
            for offset in range(0, len(items), 4):
                prn, elevation, azimuth, cn0 = items[offset:offset + 4]
                if not prn:
                    if any((elevation, azimuth, cn0)):
                        return None
                    continue
                if not 1 <= int(prn) <= 999:
                    return None
                satellites.append({"prn": str(int(prn)), "talker": talker,
                                   "elevation_deg": _nmea_number(elevation, 0, 90),
                                   "azimuth_deg": _nmea_number(azimuth, 0, 360),
                                   "cn0_dbhz": _nmea_number(cn0, 0, 99),
                                   "signal_id": signal_id})
            result.update(gsv_total=total, gsv_part=part, satellites_reported=count,
                          signal_id=signal_id, satellites_detail=satellites)
        else:
            return None
        if result.get("valid") and ("latitude" not in result or "longitude" not in result):
            return None
        return result
    except (ValueError, IndexError, OverflowError):
        return None


class GPSReader:
    """USB serial transport, position freshness and separate satellite telemetry.

    Serial-open, arriving bytes, checksummed NMEA, a valid position and a
    complete GSV cycle are deliberately distinct observations.
    """
    TELEMETRY_MAX_AGE = 10.0

    def __init__(self, port: str, baud: int = 9600):
        self.port, self.baud = port, int(baud)
        self._serial: Any = None
        self.latest: dict[str, Any] | None = None
        self.connected_at = None
        self.bytes_received = self.lines_received = self.nmea_received = self.invalid_sentences = 0
        self.last_data_at = self.last_nmea_at = self.last_fix_at = None
        self.last_satellite_at = None
        self._sentence_types: set[str] = set()
        self._gsv_pending: dict[tuple, dict] = {}
        self._gsv_complete: dict[tuple, dict] = {}
        self._gsa: dict[tuple, dict] = {}
        self._gga = None

    async def connect(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise HardwareError("GPS serial support requires pyserial: install the project's GPS extra") from exc
        try:
            self._serial = await asyncio.to_thread(serial.Serial, self.port, self.baud, timeout=1)
            self.connected_at = time.time()
        except (OSError, serial.SerialException) as exc:
            raise HardwareError(f"Cannot open GPS serial port {self.port}: {exc}") from exc

    def _observe_gsv(self, parsed: dict) -> None:
        now = parsed["received_at"]
        key = parsed["talker"], parsed["signal_id"]
        # Bound noisy/corrupt receivers even when many signal/talker IDs arrive.
        for collection in (self._gsv_pending, self._gsv_complete):
            for old_key in list(collection):
                if now - collection[old_key]["received_at"] > self.TELEMETRY_MAX_AGE:
                    del collection[old_key]
        if key not in self._gsv_pending and len(self._gsv_pending) >= 64:
            self._gsv_pending.clear()
        if parsed["gsv_part"] == 1:
            self._gsv_pending[key] = {"received_at": now, "total": parsed["gsv_total"],
                                      "count": parsed["satellites_reported"], "next": 1, "rows": []}
        pending = self._gsv_pending.get(key)
        if (pending is None or pending["next"] != parsed["gsv_part"]
                or pending["total"] != parsed["gsv_total"]
                or pending["count"] != parsed["satellites_reported"]):
            self._gsv_pending.pop(key, None)
            return
        pending["rows"].extend(parsed["satellites_detail"])
        pending["next"] += 1
        if parsed["gsv_part"] == parsed["gsv_total"]:
            if len(self._gsv_complete) >= 64 and key not in self._gsv_complete:
                self._gsv_complete.pop(min(self._gsv_complete, key=lambda k: self._gsv_complete[k]["received_at"]))
            self._gsv_complete[key] = {"received_at": now, "count": pending["count"],
                                       "rows": pending["rows"]}
            self.last_satellite_at = now
            del self._gsv_pending[key]

    async def read_fix(self) -> dict[str, Any] | None:
        if self._serial is None:
            raise HardwareError("Connect GPS reader before reading fixes")
        raw = await asyncio.to_thread(self._serial.readline)
        if not raw:
            return None
        now = time.time()
        self.bytes_received += len(raw)
        self.lines_received += 1
        self.last_data_at = now
        text = raw.decode("ascii", "replace")
        fields = _nmea_fields(text)
        if fields is None:
            self.invalid_sentences += 1
            return None
        self.nmea_received += 1
        self.last_nmea_at = now
        self._sentence_types.add(fields[0][-3:])
        parsed = parse_nmea(text)
        if parsed is None:
            return None
        kind = parsed["sentence_type"]
        if kind in ("RMC", "GGA"):
            self.latest = {**(self.latest or {}), **parsed}
            if parsed["valid"]:
                self.last_fix_at = parsed["received_at"]
            if kind == "GGA":
                self._gga = parsed
        elif kind == "GSV":
            self._observe_gsv(parsed)
        elif kind == "GSA":
            key = parsed["talker"], parsed["system_id"]
            if len(self._gsa) >= 64 and key not in self._gsa:
                self._gsa.pop(min(self._gsa, key=lambda k: self._gsa[k]["received_at"]))
            self._gsa[key] = parsed
            if (parsed["fix_type"] == 1 and self.latest is not None
                    and parsed["talker"] == self.latest.get("talker")
                    and (parsed["talker"] != "GN" or parsed["system_id"] is None)):
                # Explicit receiver fix loss is meaningful even before its
                # next RMC/GGA. It must not refresh a position timestamp;
                # conversely a 2D/3D GSA cannot create a new position fix.
                self.latest = {**self.latest, "valid": False}
        return parsed

    def status(self) -> dict[str, Any]:
        now = time.time()
        connected = self._serial is not None and bool(getattr(self._serial, "is_open", True))
        def age(stamp):
            return max(0.0, now - stamp) if stamp is not None else None
        latest = self.latest or {}
        position_age = age(latest.get("received_at"))
        recent = lambda row: connected and now - row["received_at"] <= self.TELEMETRY_MAX_AGE
        groups = [(key, group) for key, group in self._gsv_complete.items() if recent(group)]
        # GN denotes combined receiver reports. Prefer these to overlapping
        # per-constellation reports rather than summing the same satellites.
        if any(key[0] == "GN" for key, _ in groups):
            groups = [(key, group) for key, group in groups if key[0] == "GN"]
        rows = {}
        reported = {}
        for (talker, _), group in groups:
            reported[talker] = max(reported.get(talker, 0), group["count"])
            for row in group["rows"]:
                key = talker, row["prn"]
                if key not in rows or (row["cn0_dbhz"] or 0) > (rows[key]["cn0_dbhz"] or 0):
                    rows[key] = row.copy()
        gsa_all = [row for row in self._gsa.values() if recent(row)]
        gsa = gsa_all
        if any(row["talker"] == "GN" and row["system_id"] is None for row in gsa):
            gsa = [row for row in gsa if row["talker"] == "GN" and row["system_id"] is None]
        systems = {"GP": 1, "GL": 2, "GA": 3, "GB": 4, "BD": 4, "GQ": 5, "GI": 6}
        def system(row):
            return row["system_id"] or systems.get(row["talker"], row["talker"])
        used_ids = {(system(row), prn) for row in gsa for prn in row["used_prns"]}
        gga = self._gga if self._gga is not None and recent(self._gga) else None
        used = (gga["satellites_used"] if gga is not None and gga["satellites_used"] is not None
                else len(used_ids) if gsa else None)
        used_at = (gga["received_at"] if gga is not None and gga["satellites_used"] is not None
                   else max((row["received_at"] for row in gsa), default=None))
        if used_at is None:
            historical_used = [row["received_at"] for row in self._gsa.values()]
            if self._gga is not None and self._gga["satellites_used"] is not None:
                historical_used.append(self._gga["received_at"])
            used_at = max(historical_used, default=None)
        for (talker, prn), row in rows.items():
            # GN GSA system IDs use constellation-local identifiers. A
            # Galileo PRN 1 must not mark GPS PRN 1 as used. Combined GN GSV
            # rows without a constellation remain unknown when ambiguous.
            matching = [entry for entry in gsa
                        if ((talker != "GN" and system(entry) == systems.get(talker, talker))
                            or (talker == "GN" and entry["talker"] == "GN"
                                and entry["system_id"] is None))]
            row["used"] = any(prn in entry["used_prns"] for entry in matching) if matching else None
        details = sorted(rows.values(), key=lambda row: (row["talker"], int(row["prn"])))
        satellite_stamp = max((group["received_at"] for _, group in groups),
                              default=self.last_satellite_at)
        gsa_latest = max(gsa, key=lambda row: row["received_at"], default=None)
        position_gsa = [row for row in gsa_all if row["talker"] == latest.get("talker")
                        and (row["talker"] != "GN" or row["system_id"] is None)]
        position_gsa_latest = max(position_gsa, key=lambda row: row["received_at"], default=None)
        fix_type = (position_gsa_latest["fix_type"] if position_gsa_latest
                    else max((row["fix_type"] for row in gsa_all), default=None)
                    if latest.get("talker") == "GN" else gsa_latest["fix_type"] if gsa_latest else None)
        # Match the legacy receipt time to the latest position sentence only.
        # GSV/GSA can keep arriving while that position becomes stale.
        result = {**latest, "connected": connected, "connected_at": self.connected_at,
                  "port": self.port, "baud": self.baud,
                  "bytes_received": self.bytes_received, "lines_received": self.lines_received,
                  "nmea_received": self.nmea_received, "invalid_sentences": self.invalid_sentences,
                  "last_data_at": self.last_data_at, "last_nmea_at": self.last_nmea_at,
                  "last_fix_at": self.last_fix_at, "data_age_s": age(self.last_data_at),
                  "nmea_age_s": age(self.last_nmea_at), "fix_age_s": age(self.last_fix_at),
                  "age_seconds": position_age,
                  "valid": bool(connected and latest.get("valid") and position_age is not None and position_age < 5),
                  "satellites_used": used, "satellites": used,
                  "satellites_used_age_s": age(used_at),
                  "satellites_in_view": max(sum(reported.values()), len(details)) if groups else None,
                  "satellites_with_signal": sum((row["cn0_dbhz"] or 0) > 0 for row in details) if groups else None,
                  "satellites_detail": details, "satellite_age_s": age(satellite_stamp),
                  "sentence_types": sorted(self._sentence_types),
                  "fix_type": fix_type,
                  "fix_quality": gga["fix_quality"] if gga is not None else None,
                  "hdop": (gga.get("hdop") if gga is not None and gga.get("hdop") is not None
                           else gsa_latest.get("hdop") if gsa_latest else None)}
        return result

    async def close(self) -> None:
        port, self._serial = self._serial, None
        if port is not None:
            await asyncio.to_thread(port.close)
