"""Validated, versioned measurement configuration. All frequencies are Hz."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    label: str = Field(default="", max_length=80)
    frequency_hz: float = Field(ge=24e6, le=1766e6)
    bandwidth_hz: float = Field(ge=10000, le=600000)
    modulation: Literal["pocsag", "lora"]
    enabled: bool = True
    lora_sf: int = Field(default=9, ge=7, le=12)
    lora_bandwidth_hz: float = Field(default=125000, ge=125000, le=500000)

    @model_validator(mode="after")
    def check_modulation(self):
        if self.modulation == "lora" and self.bandwidth_hz < self.lora_bandwidth_hz * 1.1:
            raise ValueError("LoRa-Kanalbandbreite benötigt mindestens 10 % Filterreserve.")
        if self.modulation == "lora" and self.lora_bandwidth_hz not in (125000, 250000, 500000):
            raise ValueError("LoRa-Bandbreite muss 125, 250 oder 500 kHz sein.")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    center_frequency_hz: float = Field(default=434000000, ge=24e6, le=1766e6)
    sample_rate_hz: float = Field(default=2400000, ge=250000, le=2400000)
    gain_db: float = Field(default=20.7, ge=0, le=49.6)
    fft_size: int = Field(default=2048, ge=256, le=65536)
    heading_deg: float = Field(default=0, ge=0, lt=360)
    station_latitude: float = Field(default=52.0, ge=-89.9, le=89.9)
    station_longitude: float = Field(default=13.0, ge=-180, le=180)
    station_mode: Literal["gps", "manual"] = "gps"
    array_id: str = "uca_560mm"
    gps_port: str = Field(default="", max_length=256)
    gps_baud: int = Field(default=9600, ge=4800, le=230400)
    tx_notes: str = Field(default="", max_length=2000)
    channels: list[ChannelConfig] = Field(default_factory=lambda: [
        ChannelConfig(id="pocsag", label="POCSAG", frequency_hz=433800000,
                      bandwidth_hz=25000, modulation="pocsag"),
        ChannelConfig(id="lora", label="LoRa", frequency_hz=434200000,
                      bandwidth_hz=150000, modulation="lora"),
    ], min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_geometry_and_band(self):
        if self.fft_size & (self.fft_size - 1):
            raise ValueError("FFT-Länge muss eine Zweierpotenz sein.")
        arrays = load_presets()
        if self.array_id not in {a["id"] for a in arrays}:
            raise ValueError("Unbekanntes Antennenprofil.")
        ids = [c.id for c in self.channels]
        if len(ids) != len(set(ids)):
            raise ValueError("Kanal-IDs müssen eindeutig sein.")
        enabled = [c for c in self.channels if c.enabled]
        for channel in enabled:
            if abs(channel.frequency_hz - self.center_frequency_hz) + channel.bandwidth_hz / 2 > self.sample_rate_hz * .45:
                raise ValueError(f"Kanal {channel.id} liegt außerhalb des nutzbaren Empfangsfensters (10 % Randreserve).")
        for index, a in enumerate(enabled):
            for b in enabled[index + 1:]:
                if abs(a.frequency_hz - b.frequency_hz) < (a.bandwidth_hz + b.bandwidth_hz) / 2:
                    raise ValueError(f"Die Empfangskanäle {a.id} und {b.id} überlappen.")
        return self

    @property
    def profile_id(self) -> str:
        encoded = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]


def load_presets() -> list[dict]:
    source = PROJECT_ROOT / "config" / "antenna-presets.json"
    if not source.exists():
        source = Path(__file__).parent / "resources" / "antenna-presets.json"
    return json.loads(source.read_text())["presets"]


def positions_for(config: AppConfig):
    import numpy as np
    preset = next(p for p in load_presets() if p["id"] == config.array_id)
    antennas = sorted(preset["antennas"], key=lambda a: a["hardware_channel"])
    return np.array([[a["x_m"], a["y_m"], a["z_m"]] for a in antennas], dtype=float)
