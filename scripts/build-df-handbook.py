#!/usr/bin/env python3
"""Build the German DF handbook and its explanatory figures.

Documentation tooling only: Python + matplotlib, pandoc, XeLaTeX and DejaVu
fonts. No application dependencies or hardware are required. Run from any cwd.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "krakendf-matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "krakendf-doc-cache"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FIGURES = DOCS / "abbildungen"
INK, MUTED, CYAN, BLUE, LIGHT = "#142b38", "#516572", "#007f91", "#3462a6", "#eaf4f6"


def save(fig, name):
    FIGURES.mkdir(exist_ok=True)
    fig.savefig(FIGURES / f"{name}.png", dpi=240, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def signal_chain():
    fig, ax = plt.subplots(figsize=(10.4, 3.35))
    ax.set(xlim=(0, 10.4), ylim=(0, 3.35))
    ax.axis("off")
    nodes = [
        (0.15, 2.05, "1  ANTENNEN + KRAKEN", "5 gleichzeitige IQ-Kanäle\ninterne Zeit-/Phasenkohärenz"),
        (3.65, 2.05, "2  RF-KANALFILTER", "Gleicher Filter auf CH0–CH4\nPOCSAG / LoRa getrennt"),
        (7.15, 2.05, "3  PAKET DECODIEREN", "Rufzeichen + BFT-Position\nPaketgrenzen als Sampleindizes"),
        (7.15, 0.35, "4  MUSIC-PEILUNG", "Dasselbe Paket auf 5 Kanälen\nlokaler Azimut zur Arrayfront"),
        (3.65, 0.35, "5  NORD + KORREKTUR", "Heading addieren\noptionale Winkelkorrektur"),
        (0.15, 0.35, "6  ANZEIGE + AUSWERTUNG", "Peilung gegen GPS-Sollrichtung\nKarte · RMS · Record · Excel"),
    ]
    for x, y, title, body in nodes:
        ax.add_patch(FancyBboxPatch((x, y), 3.05, 1.05, boxstyle="round,pad=0.07,rounding_size=0.08", linewidth=1, edgecolor=CYAN, facecolor=LIGHT))
        ax.text(x + .13, y + .77, title, fontsize=9.0, weight="bold", color=INK, va="center")
        ax.text(x + .13, y + .40, body, fontsize=9, color=INK, va="center", linespacing=1.6)
    for start, end in [((3.28, 2.58), (3.52, 2.58)), ((6.78, 2.58), (7.02, 2.58)),
                       ((8.68, 1.94), (8.68, 1.50)), ((7.02, .87), (6.78, .87)),
                       ((3.52, .87), (3.28, .87))]:
        ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=16, lw=1.5, color=CYAN))
    save(fig, "df-signalweg")


def arrays():
    by_id = {preset["id"]: preset for preset in json.loads(
        (ROOT / "config/antenna-presets.json").read_text())["presets"]}
    presets = [by_id[key] for key in ("uca_300mm", "uca_560mm", "square_center_320mm")]
    fig, axes = plt.subplots(1, 3, figsize=(10.4, 3.7))
    titles = ["Kreis kompakt · Ø 300 mm", "Kreis groß · Ø 560 mm", "Quadrat + Mitte · 320 mm"]
    for ax, preset, title in zip(axes, presets, titles):
        positions = np.array([[a["x_m"] * 1000, a["y_m"] * 1000] for a in preset["antennas"]])
        outline = positions if "square" not in preset["id"] else positions[:4]
        ax.plot(*np.vstack([outline, outline[0]]).T, color="#b3c9ce", lw=1.2)
        for channel, (x, y) in enumerate(positions):
            ax.scatter(x, y, s=170, facecolor=CYAN, edgecolor="white", linewidth=1.3, zorder=3)
            offset = (13, 8) if x >= 0 else (-35, 8)
            if x == 0 and y > 0:
                offset = (10, -9)
            ax.annotate(f"CH{channel}", (x, y), xytext=offset, textcoords="offset points", color=INK, fontsize=9, weight="bold")
        ax.annotate("vorne / 0°", (0, 327), ha="center", color=CYAN, fontsize=10)
        ax.annotate("", (0, 317), (0, 292), arrowprops={"arrowstyle": "->", "color": CYAN, "lw": 1.4})
        ax.axhline(0, color="#dce5e8", lw=.6)
        ax.axvline(0, color="#dce5e8", lw=.6)
        ax.set(xlim=(-370, 370), ylim=(-330, 355), aspect="equal", xticks=[-300, 0, 300], yticks=[-300, 0, 300])
        ax.set_xlabel("x nach rechts [mm]", color=MUTED, fontsize=9)
        ax.set_title(title, fontsize=10.5, color=INK, weight="bold", pad=12)
        ax.tick_params(colors=MUTED, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#dce5e8")
    axes[0].set_ylabel("y nach vorne [mm]", color=MUTED, fontsize=9)
    fig.subplots_adjust(wspace=.30)
    save(fig, "df-antennen")


def compact_arrays():
    """Named compact profiles; all dimensions refer to antenna centres."""
    by_id = {preset["id"]: preset for preset in json.loads(
        (ROOT / "config/antenna-presets.json").read_text())["presets"]}
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.6))
    for ax, key in zip(axes, ("pegasus_95", "pentagon_105")):
        preset = by_id[key]
        antennas = sorted(preset["antennas"], key=lambda item: item["hardware_channel"])
        positions = np.array([[item["x_m"] * 1000, item["y_m"] * 1000] for item in antennas])
        ax.set(xlim=(-165, 165), ylim=(-150, 165), aspect="equal",
               xticks=[-100, 0, 100], yticks=[-100, 0, 100])
        ax.axhline(0, color="#dce5e8", lw=.7)
        ax.axvline(0, color="#dce5e8", lw=.7)
        ax.text(0, 153, "VORNE / +y / 0°", ha="center", va="center", color=CYAN, fontsize=9)
        ax.annotate("", (0, 141), (0, 120),
                    arrowprops={"arrowstyle": "->", "color": CYAN, "lw": 1.4})
        ax.set_title(preset["name"], fontsize=13, color=INK, weight="bold", pad=13)
        if key == "pegasus_95":
            for x, y in positions[:4]:
                ax.plot([0, x], [0, y], color="#b3c9ce", lw=1.4)
            ax.plot([95, 135], [0, 0], color=BLUE, lw=.7)
            ax.plot([0, 135], [95, 95], color=BLUE, lw=.7)
            ax.annotate("", (135, 95), (135, 0),
                        arrowprops={"arrowstyle": "<->", "color": BLUE, "lw": 1})
            ax.text(144, 47.5, "95 mm", rotation=90, ha="center", va="center", color=BLUE, fontsize=9)
            half_width = preset["dimensions"]["opposite_element_spacing_m"] * 500
            label = "190 mm · gegenüberliegende Antennen"
        else:
            radius = preset["dimensions"]["circle_radius_m"] * 1000
            angles = np.linspace(0, 2 * np.pi, 180)
            ax.plot(radius * np.sin(angles), radius * np.cos(angles),
                    color="#b3c9ce", lw=1, linestyle=(0, (3, 3)))
            ax.plot(*np.vstack([positions, positions[0]]).T, color="#b3c9ce", lw=1.2)
            endpoint = (radius / np.sqrt(2), radius / np.sqrt(2))
            ax.annotate("", endpoint, (0, 0),
                        arrowprops={"arrowstyle": "->", "color": BLUE, "lw": 1})
            ax.text(29, 53, "R = 105 mm", ha="center", va="center", color=BLUE, fontsize=9,
                    bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.4})
            half_width = radius
            label = "Ø 210 mm · Antennenkreis"
        for x in (-half_width, half_width):
            ax.plot([x, x], [-113, -129], color=BLUE, lw=.7)
        ax.annotate("", (half_width, -123), (-half_width, -123),
                    arrowprops={"arrowstyle": "<->", "color": BLUE, "lw": 1})
        ax.text(0, -138, label, ha="center", va="center", color=BLUE, fontsize=8.5)
        for antenna, (x, y) in zip(antennas, positions):
            ax.scatter(x, y, s=195, facecolor=CYAN, edgecolor="white", linewidth=1.3, zorder=3)
            if x < -.01:
                offset, align = (-12, 8), "right"
            else:
                offset, align = (12, 8), "left"
            if abs(x) < .01 and y > 0:
                offset = (12, -3)
            if key == "pegasus_95" and antenna["hardware_channel"] == 0:
                offset, align = (-12, -3), "right"
            elif key == "pegasus_95" and antenna["hardware_channel"] == 1:
                offset, align = (0, -17), "center"
            ax.annotate(f"CH{antenna['hardware_channel']}", (x, y), xytext=offset,
                        textcoords="offset points", ha=align, va="center", color=INK,
                        fontsize=10, weight="bold")
        ax.set_xlabel("x nach rechts [mm]", color=MUTED, fontsize=9)
        ax.tick_params(colors=MUTED, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color("#dce5e8")
    axes[0].set_ylabel("y nach vorne [mm]", color=MUTED, fontsize=9)
    fig.subplots_adjust(wspace=.22, bottom=.13, top=.85)
    save(fig, "df-kompaktprofile")


def heading():
    fig = plt.figure(figsize=(7.8, 3.8))
    ax = fig.add_axes([.05, .08, .48, .85], projection="polar")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_ylim(0, 1.12)
    ax.set_yticklabels([])
    ax.set_xticks(np.deg2rad([0, 90, 180, 270]), ["N / 0°", "O / 90°", "S / 180°", "W / 270°"])
    ax.tick_params(labelsize=9, colors=MUTED)
    ax.grid(color="#dce5e8")
    ax.spines["polar"].set_color("#dce5e8")
    for angle, color, style in [(35, BLUE, "-"), (75, CYAN, "-")]:
        ax.plot([np.deg2rad(angle)] * 2, [0, 1], color=color, lw=2.5, linestyle=style)
    for start, end, radius, label, color in [(0, 35, .45, "H = 35°", BLUE), (35, 75, .72, "θ = 40°", CYAN)]:
        a = np.linspace(start, end, 80)
        ax.plot(np.deg2rad(a), [radius] * len(a), color=color, lw=1.5)
        ax.text(np.deg2rad((start + end) / 2), radius + .13, label, ha="center", fontsize=8, color=color)
    ax.scatter([np.deg2rad(75)], [1], color=CYAN, s=55, zorder=5)
    fig.text(.59, .73, "Bekannter BFT", color=INK, fontsize=12, weight="bold")
    fig.text(.59, .60, "GPS-Sollrichtung β = 75°\nLokale Peilung θ = 40°", color=INK, fontsize=11, linespacing=1.8)
    fig.text(.59, .38, "Heading H = β − θ = 35°", color=BLUE, fontsize=12, weight="bold")
    fig.text(.59, .23, "Danach: θ + H = 75° geografisch", color=INK, fontsize=10)
    fig.text(.59, .12, "Rechenbeispiel, keine Hardwaremessung.", color=MUTED, fontsize=8)
    save(fig, "df-heading")


def rms_example():
    fig, ax = plt.subplots(figsize=(10.4, 2.8))
    azimuth = np.arange(24) * 15 + 7.5
    before = np.sqrt((4 * np.sin(np.deg2rad(azimuth)) + 2 * np.cos(np.deg2rad(2 * azimuth))) ** 2 + 2.2 ** 2)
    after = 2.1 + .35 * np.cos(np.deg2rad(2 * azimuth))
    ax.bar(azimuth - 3.1, before, width=6.2, label="Rohpeilung", color=BLUE)
    ax.bar(azimuth + 3.1, after, width=6.2, label="Mit Winkelkorrektur", color=CYAN)
    ax.set(xlim=(0, 360), ylim=(0, 7.5), xticks=np.arange(0, 361, 45), xlabel="GPS-Referenzazimut [°] · Sektoren zu 15°", ylabel="RMS [°]")
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#dce5e8")
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(MUTED)
    ax.spines["bottom"].set_color(MUTED)
    ax.tick_params(colors=MUTED)
    ax.legend(frameon=False, ncol=2, loc="upper left", fontsize=9)
    fig.text(.125, .98, "ILLUSTRATION · erfundene Beispieldaten, kein gemessener Kalibriergewinn", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, .94))
    save(fig, "df-rms-beispiel")


def main():
    for program in ("pandoc", "xelatex"):
        if not shutil.which(program):
            raise SystemExit(f"Missing documentation tool: {program}")
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK, "axes.labelcolor": MUTED, "font.size": 10})
    signal_chain()
    arrays()
    compact_arrays()
    heading()
    rms_example()
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="krakendf-docs-") as temp:
        environment["XDG_CACHE_HOME"] = temp
        subprocess.run([
            "pandoc", "df-handbuch.md", "--from=markdown+raw_tex", "--pdf-engine=xelatex",
            "--number-sections", "--resource-path=.", "--include-in-header=df-handbuch-layout.tex",
            "--output=KrakenDF-Funktion-und-Bedienung.pdf",
        ], cwd=DOCS, env=environment, check=True)
    print(DOCS / "KrakenDF-Funktion-und-Bedienung.pdf")


if __name__ == "__main__":
    main()
