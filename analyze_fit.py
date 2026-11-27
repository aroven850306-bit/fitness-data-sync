#!/usr/bin/env python3
"""
전체 FIT 파일 일괄 분석기 (fit_analysis_report.csv 생성)
- 공식 FTP(기본 194W) 기준 Coggan 파워 7존(Z1~Z7) 체류 시간(분) 산출
- 공식 LTHR(기본 147 bpm) 기준 Joe Friel 심박 5존(HRZ1~HRZ5) 체류 시간(분) 산출
- 기존 5존 컬럼과 호환되면서 Z6_anaerobic_min, Z7_neuromuscular_min 및 HRZ1~HRZ5 컬럼 추가
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fitparse import FitFile

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = DATA_DIR / "fit_files"
OUT_CSV = DATA_DIR / "fit_analysis_report.csv"

FTP_WATTS = float(os.getenv("FTP_WATTS", "194"))
LTHR_BPM = float(os.getenv("LTHR_BPM", "147"))


def _compute_np(power_filled: np.ndarray) -> float:
    if len(power_filled) == 0:
        return 0.0
    if len(power_filled) < 30:
        return round(float(np.mean(power_filled)), 1)
    rolling_30 = pd.Series(power_filled).rolling(window=30, min_periods=1).mean().to_numpy(dtype=float)
    return round(float(np.mean(np.power(rolling_30, 4)) ** 0.25), 1)


def _compute_mmp(power_filled: np.ndarray, window_sec: int) -> Optional[float]:
    if len(power_filled) < window_sec:
        return None
    val = pd.Series(power_filled).rolling(window=window_sec, min_periods=window_sec).mean().max()
    return round(float(val), 1) if pd.notna(val) else None


def _compute_decoupling(power_arr: np.ndarray, hr_arr: np.ndarray) -> Optional[float]:
    mask = np.isfinite(power_arr) & np.isfinite(hr_arr) & (power_arr > 0) & (hr_arr > 0)
    if int(np.sum(mask)) < 600:
        return None
    p_valid, hr_valid = power_arr[mask], hr_arr[mask]
    mid = len(p_valid) // 2
    ef1 = float(np.mean(p_valid[:mid])) / float(np.mean(hr_valid[:mid]))
    ef2 = float(np.mean(p_valid[mid:])) / float(np.mean(hr_valid[mid:]))
    return round(((ef1 - ef2) / ef1) * 100.0, 2) if ef1 > 0 else None


def analyze_single_fit(fit_path: Path, ftp: float = FTP_WATTS, lthr: float = LTHR_BPM) -> Optional[Dict[str, Any]]:
    fit = FitFile(str(fit_path))
    rows: List[Dict[str, Any]] = []
    start_ts: Optional[float] = None

    for idx, msg in enumerate(fit.get_messages("record")):
        rec: Dict[str, Any] = {}
        ts_val = None
        for d in msg:
            if d.name == "timestamp" and d.value is not None:
                try:
                    ts_val = d.value.timestamp()
                except Exception:
                    pass
            elif d.name in (
                "power",
                "heart_rate",
                "cadence",
                "left_right_balance",
                "left_pedal_smoothness",
                "right_pedal_smoothness",
                "combined_pedal_smoothness",
                "left_torque_effectiveness",
                "right_torque_effectiveness",
            ):
                rec[d.name] = d.value

        if ts_val is not None:
            if start_ts is None:
                start_ts = ts_val
            rec["elapsed_sec"] = max(0, int(round(ts_val - start_ts)))
        else:
            rec["elapsed_sec"] = idx
        rows.append(rec)

    if not rows:
        return None

    df = pd.DataFrame(rows).sort_values("elapsed_sec").drop_duplicates(subset=["elapsed_sec"], keep="last")
    max_sec = int(df["elapsed_sec"].max())
    merged = pd.merge(pd.DataFrame({"elapsed_sec": np.arange(0, max_sec + 1, dtype=int)}), df, on="elapsed_sec", how="left")

    power_raw = pd.to_numeric(merged.get("power"), errors="coerce").to_numpy(dtype=float)
    hr_raw = pd.to_numeric(merged.get("heart_rate"), errors="coerce").to_numpy(dtype=float)
    cad_raw = pd.to_numeric(merged.get("cadence"), errors="coerce").to_numpy(dtype=float)

    n_sec = len(power_raw)
    duration_min = round(n_sec / 60.0, 1)
    p_filled = np.where(np.isfinite(power_raw) & (power_raw >= 0), power_raw, 0.0)

    avg_power = round(float(np.mean(p_filled)), 1)
    np_watts = _compute_np(p_filled)
    max_power = int(round(float(np.max(p_filled))))
    mmp_5s = _compute_mmp(p_filled, 5)
    mmp_1m = _compute_mmp(p_filled, 60)
    mmp_5m = _compute_mmp(p_filled, 300)
    mmp_20m = _compute_mmp(p_filled, 1200)
    est_ftp = round(mmp_20m * 0.95, 1) if mmp_20m is not None else None

    hr_valid = hr_raw[np.isfinite(hr_raw) & (hr_raw > 0)]
    avg_hr = round(float(np.mean(hr_valid)), 1) if len(hr_valid) > 0 else None
    max_hr = int(round(float(np.max(hr_valid)))) if len(hr_valid) > 0 else None

    cad_valid = cad_raw[np.isfinite(cad_raw) & (cad_raw > 0)]
    avg_cad = round(float(np.mean(cad_valid)), 1) if len(cad_valid) > 0 else None

    ef = round(np_watts / avg_hr, 2) if (avg_hr and avg_hr > 0) else None
    decoupling = _compute_decoupling(power_raw, hr_raw)

    # Coggan 파워 7존 (공식 FTP 기준)
    ratio = p_filled / ftp
    pz1 = round(int(np.sum(ratio < 0.56)) / 60.0, 1)
    pz2 = round(int(np.sum((ratio >= 0.56) & (ratio < 0.76))) / 60.0, 1)
    pz3 = round(int(np.sum((ratio >= 0.76) & (ratio < 0.91))) / 60.0, 1)
    pz4 = round(int(np.sum((ratio >= 0.91) & (ratio < 1.06))) / 60.0, 1)
    pz5 = round(int(np.sum((ratio >= 1.06) & (ratio < 1.21))) / 60.0, 1)
    pz6 = round(int(np.sum((ratio >= 1.21) & (ratio < 1.51))) / 60.0, 1)
    pz7 = round(int(np.sum(ratio >= 1.51)) / 60.0, 1)

    # Joe Friel 심박 5존 (공식 LTHR 147 bpm 기준)
    z1_cut, z2_cut, z3_cut, z4_cut = round(lthr * 0.81), round(lthr * 0.90), round(lthr * 0.94), round(lthr * 1.00)
    hrz1 = round(int(np.sum(hr_valid < z1_cut)) / 60.0, 1) if len(hr_valid) > 0 else 0.0
    hrz2 = round(int(np.sum((hr_valid >= z1_cut) & (hr_valid < z2_cut))) / 60.0, 1) if len(hr_valid) > 0 else 0.0
    hrz3 = round(int(np.sum((hr_valid >= z2_cut) & (hr_valid < z3_cut))) / 60.0, 1) if len(hr_valid) > 0 else 0.0
    hrz4 = round(int(np.sum((hr_valid >= z3_cut) & (hr_valid < z4_cut))) / 60.0, 1) if len(hr_valid) > 0 else 0.0
    hrz5 = round(int(np.sum(hr_valid >= z4_cut)) / 60.0, 1) if len(hr_valid) > 0 else 0.0

    def _mean_col(col: str) -> Optional[float]:
        if col not in merged.columns:
            return None
        s = pd.to_numeric(merged[col], errors="coerce").dropna()
        s = s[s > 0]
        return round(float(s.mean()), 1) if len(s) > 0 else None

    left_bal: Optional[float] = None
    right_bal: Optional[float] = None
    if "left_right_balance" in merged.columns:
        bal_s = pd.to_numeric(merged["left_right_balance"], errors="coerce").dropna()
        if len(bal_s) > 0:
            # Garmin FIT bit7(128): 1이면 오른발 비율, 0이면 왼발 비율
            vals = bal_s.to_numpy(dtype=float)
            is_right = vals >= 128
            pct = np.where(is_right, vals - 128.0, vals)
            pct = pct[(pct > 20.0) & (pct < 80.0)]
            if len(pct) > 0:
                right_pct = float(np.mean(pct)) if np.mean(is_right) >= 0.5 else 100.0 - float(np.mean(pct))
                right_bal = round(right_pct, 1)
                left_bal = round(100.0 - right_bal, 1)

    return {
        "file": fit_path.name,
        "duration_min": duration_min,
        "avg_power": avg_power,
        "NP_watts": np_watts,
        "max_power": max_power,
        "MMP_5s": mmp_5s,
        "MMP_1m": mmp_1m,
        "MMP_5m": mmp_5m,
        "MMP_20m": mmp_20m,
        "est_FTP_from_20m": est_ftp,
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "avg_cadence": avg_cad,
        "efficiency_factor_EF": ef,
        "aerobic_decoupling_pct": decoupling,
        "left_balance_pct": left_bal,
        "right_balance_pct": right_bal,
        "left_pedal_smoothness_pct": _mean_col("left_pedal_smoothness"),
        "right_pedal_smoothness_pct": _mean_col("right_pedal_smoothness"),
        "combined_pedal_smoothness_pct": _mean_col("combined_pedal_smoothness"),
        "left_torque_effectiveness_pct": _mean_col("left_torque_effectiveness"),
        "right_torque_effectiveness_pct": _mean_col("right_torque_effectiveness"),
        "Z1_recovery_min": pz1,
        "Z2_endurance_min": pz2,
        "Z3_tempo_min": pz3,
        "Z4_threshold_min": pz4,
        "Z5_vo2max_min": pz5,
        "Z6_anaerobic_min": pz6,
        "Z7_neuromuscular_min": pz7,
        "HRZ1_recovery_min": hrz1,
        "HRZ2_aerobic_min": hrz2,
        "HRZ3_tempo_min": hrz3,
        "HRZ4_threshold_min": hrz4,
        "HRZ5_vo2max_min": hrz5,
    }


def main() -> None:
    fit_files = sorted(FIT_DIR.glob("*.fit"), key=lambda p: p.name, reverse=True)
    records = [r for p in fit_files if (r := analyze_single_fit(p)) is not None]
    df = pd.DataFrame(records)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] 총 {len(df)}개 FIT 파일 7존·5존 분석 완료 -> {OUT_CSV}")


if __name__ == "__main__":
    main()
