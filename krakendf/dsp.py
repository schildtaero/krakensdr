"""Numerical direction finding and measurement evaluation.

Coordinate convention: array x points right, y points forward, azimuth starts
at +y and increases clockwise.  The steering phase is positive for a source
at ``(sin(azimuth), cos(azimuth))``.  Hardware integrations must verify their
IQ convention with a known emitter before relying on an absolute bearing.

All geodesics use the WGS84 ellipsoid, with independently implemented Vincenty
direct/inverse iterations.  The inverse rejects non-convergent antipodal
positions instead of returning an apparently precise spherical fallback.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

SPEED_OF_LIGHT_M_S = 299_792_458.0
WGS84_A_M = 6_378_137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B_M = WGS84_A_M * (1.0 - WGS84_F)


def wrap180(angle: Any) -> Any:
    """Wrap degrees onto [-180, 180), preserving array input shape."""
    result = (np.asarray(angle, dtype=float) + 180.0) % 360.0 - 180.0
    return float(result) if result.ndim == 0 else result


def _coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    lat, lon = float(latitude), float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ValueError("Coordinates must be finite")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValueError("Coordinates outside latitude/longitude bounds")
    return math.radians(lat), math.radians(lon)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial geographic bearing on WGS84 in [0, 360).

    Raises ValueError for coincident coordinates or a non-convergent inverse
    geodesic.  Coincident positions cannot provide a DF reference direction.
    """
    phi1, lam1 = _coordinates(lat1, lon1)
    phi2, lam2 = _coordinates(lat2, lon2)
    longitude_difference = math.radians(wrap180(math.degrees(lam2 - lam1)))
    u1 = math.atan((1.0 - WGS84_F) * math.tan(phi1))
    u2 = math.atan((1.0 - WGS84_F) * math.tan(phi2))
    su1, cu1, su2, cu2 = math.sin(u1), math.cos(u1), math.sin(u2), math.cos(u2)
    lam = longitude_difference
    for _ in range(200):
        sl, cl = math.sin(lam), math.cos(lam)
        x, y = cu2 * sl, cu1 * su2 - su1 * cu2 * cl
        sin_sigma = math.hypot(x, y)
        cos_sigma = su1 * su2 + cu1 * cu2 * cl
        if sin_sigma < 1e-15:
            if cos_sigma > 0.0:
                raise ValueError("Coincident positions have no reference bearing")
            raise ValueError("Antipodal positions have no unique reference bearing")
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cu1 * cu2 * sl / sin_sigma
        cos2_alpha = max(0.0, 1.0 - sin_alpha * sin_alpha)
        cos2_sigma_m = (cos_sigma - 2.0 * su1 * su2 / cos2_alpha
                        if cos2_alpha > 1e-15 else 0.0)
        coefficient = (WGS84_F / 16.0 * cos2_alpha
                       * (4.0 + WGS84_F * (4.0 - 3.0 * cos2_alpha)))
        next_lam = longitude_difference + (1.0 - coefficient) * WGS84_F * sin_alpha * (
            sigma + coefficient * sin_sigma * (
                cos2_sigma_m + coefficient * cos_sigma * (-1.0 + 2.0 * cos2_sigma_m ** 2)))
        if abs(next_lam - lam) < 1e-13:
            lam = next_lam
            break
        lam = next_lam
    else:
        raise ValueError("WGS84 inverse did not converge; reference positions unsupported")
    initial = math.atan2(cu2 * math.sin(lam), cu1 * su2 - su1 * cu2 * math.cos(lam))
    return math.degrees(initial) % 360.0


def destination(lat: float, lon: float, bearing: float, distance_m: float) -> tuple[float, float]:
    """Position reached along a WGS84 geodesic; distance must be nonnegative."""
    phi1, lam1 = _coordinates(lat, lon)
    if not math.isfinite(float(bearing)) or not math.isfinite(float(distance_m)) or distance_m < 0:
        raise ValueError("Bearing and nonnegative distance must be finite")
    if distance_m == 0:
        return float(lat), float(lon)
    alpha1 = math.radians(bearing % 360.0)
    tan_u1 = (1.0 - WGS84_F) * math.tan(phi1)
    cos_u1 = 1.0 / math.sqrt(1.0 + tan_u1 * tan_u1)
    sin_u1 = tan_u1 * cos_u1
    sin_alpha1, cos_alpha1 = math.sin(alpha1), math.cos(alpha1)
    sigma1 = math.atan2(tan_u1, cos_alpha1)
    sin_alpha = cos_u1 * sin_alpha1
    cos2_alpha = 1.0 - sin_alpha * sin_alpha
    u_squared = cos2_alpha * (WGS84_A_M ** 2 - WGS84_B_M ** 2) / WGS84_B_M ** 2
    big_a = 1.0 + u_squared / 16384.0 * (4096.0 + u_squared * (-768.0 + u_squared * (320.0 - 175.0 * u_squared)))
    big_b = u_squared / 1024.0 * (256.0 + u_squared * (-128.0 + u_squared * (74.0 - 47.0 * u_squared)))
    distance_term = float(distance_m) / (WGS84_B_M * big_a)
    sigma = distance_term
    for _ in range(200):
        ss, cs = math.sin(sigma), math.cos(sigma)
        c2sm = math.cos(2.0 * sigma1 + sigma)
        delta = big_b * ss * (c2sm + big_b / 4.0 * (
            cs * (-1.0 + 2.0 * c2sm ** 2)
            - big_b / 6.0 * c2sm * (-3.0 + 4.0 * ss ** 2) * (-3.0 + 4.0 * c2sm ** 2)))
        next_sigma = distance_term + delta
        if abs(next_sigma - sigma) < 1e-13:
            sigma = next_sigma
            break
        sigma = next_sigma
    else:
        raise ValueError("WGS84 direct geodesic did not converge")
    ss, cs = math.sin(sigma), math.cos(sigma)
    c2sm = math.cos(2.0 * sigma1 + sigma)
    tmp = sin_u1 * ss - cos_u1 * cs * cos_alpha1
    phi2 = math.atan2(sin_u1 * cs + cos_u1 * ss * cos_alpha1,
                     (1.0 - WGS84_F) * math.hypot(sin_alpha, tmp))
    lam = math.atan2(ss * sin_alpha1, cos_u1 * cs - sin_u1 * ss * cos_alpha1)
    coefficient = WGS84_F / 16.0 * cos2_alpha * (4.0 + WGS84_F * (4.0 - 3.0 * cos2_alpha))
    longitude_term = lam - (1.0 - coefficient) * WGS84_F * sin_alpha * (
        sigma + coefficient * ss * (c2sm + coefficient * cs * (-1.0 + 2.0 * c2sm ** 2)))
    return math.degrees(phi2), float(wrap180(math.degrees(lam1 + longitude_term)))


def _positions(positions_m: Any) -> np.ndarray:
    positions = np.asarray(positions_m, dtype=float)
    if positions.shape not in ((5, 2), (5, 3)) or not np.isfinite(positions).all():
        raise ValueError("Five finite antenna positions with two or three coordinates required")
    if np.unique(positions[:, :2], axis=0).shape[0] != 5:
        raise ValueError("Antenna positions must be distinct in the array plane")
    return positions


def steering_vector(positions_m: Any, frequency_hz: float, azimuth_deg: float) -> np.ndarray:
    """Five-channel steering vector for a horizontal incident plane wave."""
    positions = _positions(positions_m)
    if not math.isfinite(frequency_hz) or frequency_hz <= 0 or not math.isfinite(azimuth_deg):
        raise ValueError("Positive frequency and finite azimuth required")
    theta = math.radians(azimuth_deg)
    projection = positions[:, 0] * math.sin(theta) + positions[:, 1] * math.cos(theta)
    return np.exp(2j * np.pi * frequency_hz / SPEED_OF_LIGHT_M_S * projection)


def estimate_doa(iq: Any, positions_m: Any, frequency_hz: float,
                 method: str = "music", step_deg: float = 1.0) -> dict[str, Any]:
    """Estimate one source's azimuth from one coherent five-channel IQ burst.

    MUSIC assumes one dominant source within the channelized RF packet.  The
    returned confidence is an eigenvalue-gap score, not a calibrated angular
    uncertainty.  The pseudospectrum is normalized to its maximum (0 dB).
    """
    data = np.asarray(iq)
    positions = _positions(positions_m)
    if data.ndim != 2 or data.shape[0] != 5 or data.shape[1] < 16:
        raise ValueError("A coherent (5, N) burst with at least 16 samples is required")
    if not np.isfinite(data).all():
        raise ValueError("IQ contains non-finite samples")
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("Frequency must be positive")
    if not math.isfinite(step_deg) or not 0.1 <= step_deg <= 10.0:
        raise ValueError("Scan step must lie between 0.1 and 10 degrees")
    n_angles = round(360.0 / step_deg)
    if not math.isclose(n_angles * step_deg, 360.0, abs_tol=1e-9):
        raise ValueError("Scan step must divide 360 degrees")
    if method.lower() not in ("music", "bartlett"):
        raise ValueError("Supported DF methods are music and bartlett")
    # Evenly spaced covariance samples bound CPU work without introducing
    # antenna-relative resampling or a change to the common RF window.
    stride = max(1, math.ceil(data.shape[1] / 32768))
    samples = np.asarray(data[:, ::stride], dtype=np.complex128)
    covariance = samples @ samples.conj().T / samples.shape[1]
    power = float(np.trace(covariance).real / 5.0)
    if power <= np.finfo(float).tiny:
        raise ValueError("Zero-power IQ has no direction")
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    eigenvalues = np.maximum(eigenvalues.real, 0.0)
    angles = np.arange(n_angles, dtype=float) * step_deg
    theta = np.deg2rad(angles)
    projections = positions[:, 0, None] * np.sin(theta) + positions[:, 1, None] * np.cos(theta)
    scan = np.exp(2j * np.pi * frequency_hz / SPEED_OF_LIGHT_M_S * projections)
    if method.lower() == "music":
        noise_basis = eigenvectors[:, :-1]
        denominator = np.sum(np.abs(noise_basis.conj().T @ scan) ** 2, axis=0)
        spectrum = 1.0 / np.maximum(denominator, 1e-14)
    else:
        spectrum = np.maximum(np.real(np.sum(scan.conj() * (covariance @ scan), axis=0)), 1e-20)
    peak_index = int(np.argmax(spectrum))
    log_spectrum = np.log(np.maximum(spectrum, 1e-30))
    left, middle, right = log_spectrum[(peak_index - 1) % n_angles], log_spectrum[peak_index], log_spectrum[(peak_index + 1) % n_angles]
    divisor = left - 2.0 * middle + right
    adjustment = float(np.clip(0.5 * (left - right) / divisor, -0.5, 0.5)) if abs(divisor) > 1e-15 else 0.0
    azimuth = ((peak_index + adjustment) * step_deg) % 360.0
    confidence = float(np.clip((eigenvalues[-1] - eigenvalues[-2]) / max(eigenvalues[-1], 1e-30), 0.0, 1.0))
    spectrum_db = 10.0 * np.log10(np.maximum(spectrum / spectrum.max(), 1e-8))
    return {
        "azimuth_deg": azimuth,
        "confidence": confidence,
        "confidence_kind": "single_source_eigengap_not_angular_uncertainty",
        "power_db": 10.0 * math.log10(power),
        "angles_deg": angles.tolist(),
        "spectrum_db": spectrum_db.tolist(),
        "method": method.lower(),
        "sample_count": int(data.shape[1]),
    }


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _rms(values: Iterable[float]) -> float | None:
    values = list(values)
    return float(np.sqrt(np.mean(np.square(values)))) if values else None


def _circular_mean(values: Iterable[float]) -> float | None:
    values = list(values)
    if not values:
        return None
    average = np.mean(np.exp(1j * np.deg2rad(values)))
    if abs(average) < 1e-10:
        return None
    return float(wrap180(np.rad2deg(np.angle(average))))


def statistics(observations: list[dict[str, Any]], bin_width_deg: float = 15) -> dict[str, Any]:
    """Packet RMS and reference-azimuth bins; nonaccepted records are excluded.

    RMS angles are wrapped before squaring. Corrected errors default to raw
    errors when no correction has been applied. Missing bins remain None,
    rather than becoming spurious zero-error measurements.
    """
    if not _finite(bin_width_deg) or not 1.0 <= bin_width_deg <= 180.0:
        raise ValueError("Bin width must lie between 1 and 180 degrees")
    n_bins = round(360.0 / bin_width_deg)
    if not math.isclose(n_bins * bin_width_deg, 360.0, abs_tol=1e-9):
        raise ValueError("Bin width must divide 360 degrees")
    usable = []
    for record in observations:
        if record.get("accepted", True) and _finite(record.get("reference_deg")) and _finite(record.get("error_raw_deg")):
            raw = float(wrap180(float(record["error_raw_deg"])))
            corrected_value = record.get("error_corrected_deg", raw)
            corrected = float(wrap180(float(corrected_value))) if _finite(corrected_value) else None
            usable.append((record, float(record["reference_deg"]) % 360.0, raw, corrected))
    bins = []
    for index in range(n_bins):
        subset = [r for r in usable if min(int(r[1] / bin_width_deg), n_bins - 1) == index]
        bins.append({
            "start_deg": float(index * bin_width_deg),
            "end_deg": float((index + 1) * bin_width_deg),
            "count": len(subset),
            "rms_raw_deg": _rms(r[2] for r in subset),
            "rms_corrected_deg": _rms(r[3] for r in subset if r[3] is not None),
            "bias_raw_deg": _circular_mean(r[2] for r in subset),
        })
    profile_ids = sorted({str(r[0]["profile_id"]) for r in usable if r[0].get("profile_id") is not None})
    result: dict[str, Any] = {
        "count": len(usable),
        "excluded_count": len(observations) - len(usable),
        "rms_raw_deg": _rms(r[2] for r in usable),
        "rms_corrected_deg": _rms(r[3] for r in usable if r[3] is not None),
        "bias_raw_deg": _circular_mean(r[2] for r in usable),
        "bias_corrected_deg": _circular_mean(r[3] for r in usable if r[3] is not None),
        "bins": bins,
        "bin_width_deg": float(bin_width_deg),
        "covered_sectors": sum(b["count"] > 0 for b in bins),
        "total_sectors": n_bins,
        "coverage_fraction": sum(b["count"] > 0 for b in bins) / n_bins,
        "equal_sector_rms_raw_deg": _rms(b["rms_raw_deg"] for b in bins if b["count"]),
        "equal_sector_rms_corrected_deg": _rms(b["rms_corrected_deg"] for b in bins if b["rms_corrected_deg"] is not None),
        "mixed_profiles": len(profile_ids) > 1,
        "profile_ids": profile_ids,
    }
    if len(profile_ids) > 1:
        result["by_profile"] = {
            profile_id: statistics([r for r in observations if str(r.get("profile_id")) == profile_id], bin_width_deg)
            for profile_id in profile_ids
        }
    return result


def _calibration_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        if not record.get("accepted", True) or not _finite(record.get("raw_deg")) or not _finite(record.get("reference_deg")):
            continue
        error = float(wrap180(float(record["raw_deg"]) - float(record["reference_deg"])))
        if abs(error) > 45.0:
            # A failed heading or grossly ambiguous DF must not become a
            # large learned correction. It needs a new heading/mount check.
            continue
        rows.append({**record, "raw_deg": float(record["raw_deg"]) % 360.0,
                     "reference_deg": float(record["reference_deg"]) % 360.0, "error_deg": error})
    return rows


def _fourier_matrix(angles: Any, order: int = 3) -> np.ndarray:
    theta = np.deg2rad(np.asarray(angles, dtype=float))
    columns = [np.ones_like(theta)]
    for harmonic in range(1, order + 1):
        columns.extend((np.sin(harmonic * theta), np.cos(harmonic * theta)))
    return np.column_stack(columns)


def _predict_bias(raw_angles: Any, coefficients: Any) -> np.ndarray:
    return _fourier_matrix(raw_angles) @ np.asarray(coefficients, dtype=float)


def fit_calibration(training: list[dict[str, Any]], validation: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit a smooth periodic bias and validate on separate measurement runs.

    Fixed third Fourier order avoids selecting model complexity against the
    purported independent validation data. Training requires >=3 accepted
    samples in every 15-degree reference sector. Validation requires >=1 per
    sector and explicitly disjoint nonempty run_id sets. Caller must also
    isolate hardware/RX/heading profiles; mixed profile IDs are rejected.
    """
    train, check = _calibration_rows(training), _calibration_rows(validation)
    train_counts = np.zeros(24, dtype=int)
    validation_counts = np.zeros(24, dtype=int)
    for row in train:
        train_counts[min(int(row["reference_deg"] / 15.0), 23)] += 1
    for row in check:
        validation_counts[min(int(row["reference_deg"] / 15.0), 23)] += 1
    coverage = [{"start_deg": 15 * i, "end_deg": 15 * (i + 1), "count": int(train_counts[i]),
                 "validation_count": int(validation_counts[i]), "ready": bool(train_counts[i] >= 3)}
                for i in range(24)]
    model: dict[str, Any] = {
        "schema_version": 1,
        "model_kind": "periodic_fourier_bias",
        "order": 3,
        "coefficient_order": ["constant", "sin1", "cos1", "sin2", "cos2", "sin3", "cos3"],
        "coefficients": [],
        "eligible": False,
        "validated": False,
        "improved": False,
        "reason": "Kalibrierdaten decken noch nicht alle 24 Richtungssektoren mit je drei gültigen Messungen ab.",
        "training_count": len(train),
        "validation_count": len(check),
        "training_excluded_count": len(training) - len(train),
        "validation_excluded_count": len(validation) - len(check),
        "coverage": coverage,
        "coverage_complete": bool(np.all(train_counts >= 3)),
        "validation_coverage_complete": bool(np.all(validation_counts >= 1)),
        "validation_before_deg": None,
        "validation_after_deg": None,
        "max_training_error_deg": 45.0,
        "independent_runs": False,
    }
    profiles = {str(row["profile_id"]) for row in train + check if row.get("profile_id") is not None}
    if len(profiles) > 1:
        model["reason"] = "Kalibrierung enthält verschiedene Messprofile; Profile getrennt auswerten."
        return model
    if not model["coverage_complete"]:
        return model
    x = _fourier_matrix([row["raw_deg"] for row in train])
    y = np.array([row["error_deg"] for row in train])
    # Each direction has equal influence even when one moving emitter paused
    # longer in an easy direction. Weak ridge stabilizes sparse/noisy sectors.
    weights = np.array([1.0 / train_counts[min(int(row["reference_deg"] / 15.0), 23)] for row in train])
    regularization = np.diag([0.0, 0.01, 0.01, 0.04, 0.04, 0.09, 0.09])
    coefficients = np.linalg.solve(x.T @ (weights[:, None] * x) + regularization, x.T @ (weights * y))
    model["coefficients"] = coefficients.tolist()
    model["eligible"] = True
    train_run_ids = {str(row["run_id"]) for row in train if row.get("run_id") is not None}
    validation_run_ids = {str(row["run_id"]) for row in check if row.get("run_id") is not None}
    all_have_run_ids = all(row.get("run_id") is not None for row in train + check)
    model["training_run_ids"] = sorted(train_run_ids)
    model["validation_run_ids"] = sorted(validation_run_ids)
    model["independent_runs"] = bool(all_have_run_ids and train_run_ids and validation_run_ids
                                      and not train_run_ids.intersection(validation_run_ids))
    if not model["independent_runs"]:
        model["reason"] = "Ein separater Prüflauf mit unabhängiger run_id ist für die Freigabe erforderlich."
        return model
    def packet_ids(rows: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
        return {(row.get("stream_epoch"), row["burst_id"]) for row in rows if row.get("burst_id") is not None}
    if packet_ids(train).intersection(packet_ids(check)):
        model["independent_runs"] = False
        model["reason"] = "Trainings- und Prüflauf enthalten dieselben Burst-IDs."
        return model
    if not model["validation_coverage_complete"]:
        model["reason"] = "Der unabhängige Prüflauf deckt noch nicht alle Richtungssektoren ab."
        return model
    raw_angles = np.array([row["raw_deg"] for row in check])
    references = np.array([row["reference_deg"] for row in check])
    before = wrap180(raw_angles - references)
    corrected = wrap180(raw_angles - _predict_bias(raw_angles, coefficients) - references)
    model["validation_before_deg"] = _rms(before)
    model["validation_after_deg"] = _rms(corrected)
    model["validated"] = True
    # Require a small absolute and relative improvement rather than enabling
    # a numerically insignificant change. This is an acceptance rule, not a
    # statistical claim about an unseen measurement environment.
    required_gain = max(0.05, 0.01 * float(model["validation_before_deg"]))
    model["improved"] = float(model["validation_after_deg"]) < float(model["validation_before_deg"]) - required_gain
    model["reason"] = ("Unabhängiger Prüflauf zeigt eine Verbesserung; Korrektur kann aktiviert werden."
                       if model["improved"] else "Keine ausreichende Verbesserung im unabhängigen Prüflauf; Korrektur bleibt deaktiviert.")
    return model


def apply_calibration(raw_deg: float, model: dict[str, Any] | None) -> float:
    """Apply only a trained, independently validated, improving correction."""
    if not _finite(raw_deg):
        raise ValueError("Raw bearing must be finite")
    raw = float(raw_deg) % 360.0
    if not model or not all(model.get(key) for key in ("eligible", "validated", "improved")):
        return raw
    coefficients = np.asarray(model.get("coefficients", []), dtype=float)
    if coefficients.shape != (7,) or not np.isfinite(coefficients).all():
        raise ValueError("Malformed Fourier correction coefficients")
    bias = float(_predict_bias([raw], coefficients)[0])
    return (raw - bias) % 360.0
