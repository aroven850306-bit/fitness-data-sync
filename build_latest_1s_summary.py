#!/usr/bin/env python3
"""
직전 라이딩 1초 FIT 시계열 기반 워크아웃 블록 자동 감지 및 요약 생성기
- ① 자동 블록 감지(30초 롤링 파워/케이던스 변화점 + 지속 시간 기반 Warmup / Work / Recovery / Cooldown 분할)
- 공식 FTP 기준값(기본 194W) 및 LTHR 기준값(기본 147 bpm, HRmax 173 bpm) 기반 파워·심박 5존(Z1~Z5) 통일 산출
- 스프레드시트 첫 줄 #ERROR! 예방을 위한 순수 텍스트 summary_line 정화 및 quoting=csv.QUOTE_ALL CSV 저장
- latest_ride_1s_summary.txt 및 latest_ride_1s_summary.csv 생성
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from fitparse import FitFile
except ImportError:  # pragma: no cover
    FitFile = None  # type: ignore

ROOT_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = ROOT_DIR / "fit_files"
OUT_TXT = ROOT_DIR / "latest_ride_1s_summary.txt"
OUT_CSV = ROOT_DIR / "latest_ride_1s_summary.csv"


def _load_env_baseline() -> Tuple[float, float, float]:
    """
    환경변수(FTP_WATTS, LTHR_BPM, MAX_HR)에서 기준값을 읽고,
    없으면 공식 확정 기준값(194W, LTHR 147 bpm, HRmax 173 bpm)을 반환한다.
    """
    default_ftp = 194.0
    default_lthr = 147.0
    default_max_hr = 173.0

    env_ftp = os.getenv("FTP_WATTS", "").strip()
    env_lthr = os.getenv("LTHR_BPM", "").strip()
    env_max_hr = os.getenv("MAX_HR", "").strip()

    try:
        resolved_ftp = float(env_ftp) if env_ftp else default_ftp
    except Exception:
        resolved_ftp = default_ftp

    try:
        resolved_lthr = float(env_lthr) if env_lthr else default_lthr
    except Exception:
        resolved_lthr = default_lthr

    try:
        resolved_max_hr = float(env_max_hr) if env_max_hr else default_max_hr
    except Exception:
        resolved_max_hr = default_max_hr

    if resolved_ftp <= 0:
        resolved_ftp = default_ftp
    if resolved_lthr <= 0:
        resolved_lthr = default_lthr
    if resolved_max_hr <= 0:
        resolved_max_hr = default_max_hr

    return resolved_ftp, resolved_lthr, resolved_max_hr


FTP_WATTS, LTHR_BPM, MAX_HR = _load_env_baseline()

SEMICIRCLE_TO_DEG = 180.0 / (2**31)


@dataclass
class DetectedBlock:
    block_type: str  # WARMUP | WORK | RECOVERY | COOLDOWN
    label: str
    start_sec: int
    end_sec: int
    duration_sec: int
    avg_power: float
    max_power: float
    avg_hr: Optional[float]
    max_hr: Optional[float]
    avg_cadence: Optional[float]
    ef: Optional[float]
    hr_delta_prev: Optional[float]


def _to_float_array(values: List[Any]) -> np.ndarray:
    arr = np.full(len(values), np.nan, dtype=float)
    for i, v in enumerate(values):
        if v is None:
            continue
        try:
            fv = float(v)
            if np.isfinite(fv):
                arr[i] = fv
        except Exception:
            continue
    return arr


def _format_mmss(sec: int) -> str:
    sec_clamped = max(0, int(sec))
    mm = sec_clamped // 60
    ss = sec_clamped % 60
    return f"{mm:02d}:{ss:02d}"


def _sanitize_summary_line(raw_text: str) -> str:
    """
    구글 스프레드시트 셀에서 수식 또는 파싱 오류(#ERROR!)가 발생하지 않도록
    줄바꿈·제어문자·#ERROR!·선행 기호 및 수식 유발 문자를 제거한 순수 문자열을 반환한다.
    """
    cleaned = raw_text.replace("#ERROR!", "ERR").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    cleaned = re.sub(r"[=+\-@#|<>(){}\[\]]", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        return "SUMMARY_1S empty"
    return f"SUMMARY_1S {cleaned}"


def _compute_np(power_filled: np.ndarray) -> float:
    if len(power_filled) == 0:
        return 0.0
    if len(power_filled) < 30:
        return round(float(np.mean(power_filled)), 1)
    rolling_30 = pd.Series(power_filled).rolling(window=30, min_periods=1).mean().to_numpy(dtype=float)
    return round(float(np.mean(np.power(rolling_30, 4)) ** 0.25), 1)


def _compute_mmp(power_filled: np.ndarray, window_sec: int) -> Optional[float]:
    if len(power_filled) < window_sec or window_sec <= 0:
        return None
    val = pd.Series(power_filled).rolling(window=window_sec, min_periods=window_sec).mean().max()
    if pd.isna(val):
        return None
    return round(float(val), 1)


def _compute_decoupling(power_arr: np.ndarray, hr_arr: np.ndarray) -> Optional[float]:
    mask = np.isfinite(power_arr) & np.isfinite(hr_arr) & (power_arr > 0) & (hr_arr > 0)
    if int(np.sum(mask)) < 600:
        return None
    p_valid = power_arr[mask]
    hr_valid = hr_arr[mask]
    mid = len(p_valid) // 2
    ef1 = float(np.mean(p_valid[:mid])) / float(np.mean(hr_valid[:mid]))
    ef2 = float(np.mean(p_valid[mid:])) / float(np.mean(hr_valid[mid:]))
    if ef1 <= 0:
        return None
    return round(((ef1 - ef2) / ef1) * 100.0, 2)


def _zone_times_minutes(power_filled: np.ndarray, ftp: float) -> Dict[str, float]:
    """공식 FTP 기준 Coggan 파워 5존 체류 시간(분)"""
    if len(power_filled) == 0 or ftp <= 0:
        return {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}
    ratio = power_filled / ftp
    z1 = int(np.sum(ratio < 0.56))
    z2 = int(np.sum((ratio >= 0.56) & (ratio < 0.76)))
    z3 = int(np.sum((ratio >= 0.76) & (ratio < 0.91)))
    z4 = int(np.sum((ratio >= 0.91) & (ratio < 1.06)))
    z5 = int(np.sum(ratio >= 1.06))
    return {
        "Z1": round(z1 / 60.0, 1),
        "Z2": round(z2 / 60.0, 1),
        "Z3": round(z3 / 60.0, 1),
        "Z4": round(z4 / 60.0, 1),
        "Z5": round(z5 / 60.0, 1),
    }


def _hr_zone_times_minutes(hr_arr: np.ndarray, lthr: float) -> Dict[str, float]:
    """
    젖산역치 심박(LTHR, 기본 147 bpm) 기반 Joe Friel 5존 심박 체류 시간(분)
    - Z1: < 81% LTHR (< 119 bpm)
    - Z2: 81% ~ < 90% LTHR (119 ~ 131 bpm)
    - Z3: 90% ~ < 94% LTHR (132 ~ 137 bpm)
    - Z4: 94% ~ < 100% LTHR (138 ~ 146 bpm)
    - Z5: >= 100% LTHR (147 ~ 173 bpm)
    """
    valid_hr = hr_arr[np.isfinite(hr_arr) & (hr_arr > 0)]
    if len(valid_hr) == 0 or lthr <= 0:
        return {"Z1": 0.0, "Z2": 0.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0}

    z1_cut = round(lthr * 0.81)  # 119 bpm @ LTHR 147
    z2_cut = round(lthr * 0.90)  # 132 bpm @ LTHR 147
    z3_cut = round(lthr * 0.94)  # 138 bpm @ LTHR 147
    z4_cut = round(lthr * 1.00)  # 147 bpm @ LTHR 147

    z1 = int(np.sum(valid_hr < z1_cut))
    z2 = int(np.sum((valid_hr >= z1_cut) & (valid_hr < z2_cut)))
    z3 = int(np.sum((valid_hr >= z2_cut) & (valid_hr < z3_cut)))
    z4 = int(np.sum((valid_hr >= z3_cut) & (valid_hr < z4_cut)))
    z5 = int(np.sum(valid_hr >= z4_cut))
    return {
        "Z1": round(z1 / 60.0, 1),
        "Z2": round(z2 / 60.0, 1),
        "Z3": round(z3 / 60.0, 1),
        "Z4": round(z4 / 60.0, 1),
        "Z5": round(z5 / 60.0, 1),
    }


def _parse_fit_1s_series(fit_path: Path) -> Tuple[Optional[pd.DataFrame], Dict[str, Any]]:
    """원본 .fit 파일에서 1초 시계열(elapsed_sec, power, cadence, heart_rate) 및 세션 요약을 추출한다."""
    if FitFile is None:
        raise RuntimeError("fitparse 라이브러리가 설치되어 있지 않습니다.")

    fit = FitFile(str(fit_path))
    rows: List[Dict[str, Any]] = []
    session_meta: Dict[str, Any] = {}

    for msg in fit.get_messages("session"):
        for d in msg:
            if d.name and d.value is not None:
                session_meta[d.name] = d.value

    start_ts: Optional[float] = None
    for idx, msg in enumerate(fit.get_messages("record")):
        rec: Dict[str, Any] = {"idx": idx}
        ts_val = None
        for d in msg:
            if d.name == "timestamp" and d.value is not None:
                try:
                    ts_val = d.value.timestamp()
                    rec["timestamp"] = d.value.isoformat()
                except Exception:
                    pass
            elif d.name in ("power", "cadence", "heart_rate", "speed", "distance", "altitude"):
                rec[d.name] = d.value
            elif d.name == "position_lat" and d.value is not None:
                rec["lat"] = float(d.value) * SEMICIRCLE_TO_DEG
            elif d.name == "position_long" and d.value is not None:
                rec["lon"] = float(d.value) * SEMICIRCLE_TO_DEG

        if ts_val is not None:
            if start_ts is None:
                start_ts = ts_val
            rec["elapsed_sec"] = max(0, int(round(ts_val - start_ts)))
        else:
            rec["elapsed_sec"] = idx
        rows.append(rec)

    if not rows:
        return None, session_meta

    df = pd.DataFrame(rows).sort_values("elapsed_sec").drop_duplicates(subset=["elapsed_sec"], keep="last")
    max_sec = int(df["elapsed_sec"].max())
    full_grid = pd.DataFrame({"elapsed_sec": np.arange(0, max_sec + 1, dtype=int)})
    merged = pd.merge(full_grid, df, on="elapsed_sec", how="left")

    for col in ("power", "cadence", "heart_rate", "speed", "distance", "altitude", "lat", "lon"):
        if col not in merged.columns:
            merged[col] = np.nan
        merged[col] = pd.to_numeric(merged[col], errors="coerce")

    return merged, session_meta


def _load_latest_stream(fit_path: Optional[Path]) -> Tuple[Optional[pd.DataFrame], Optional[Path], Dict[str, Any]]:
    """가장 최근 .fit 파일을 찾아 1초 시계열을 로드한다. (백업용 streams_1s_gz/latest_stream_1s.json.gz 지원)"""
    if fit_path is not None and fit_path.exists():
        df, meta = _parse_fit_1s_series(fit_path)
        if df is not None and len(df) > 0:
            return df, fit_path, meta

    fit_candidates = sorted(FIT_DIR.glob("*.fit"), key=lambda p: p.name, reverse=True) if FIT_DIR.exists() else []
    if fit_candidates:
        latest_fit = fit_candidates[0]
        df, meta = _parse_fit_1s_series(latest_fit)
        if df is not None and len(df) > 0:
            return df, latest_fit, meta

    gz_path = ROOT_DIR / "streams_1s_gz" / "latest_stream_1s.json.gz"
    if gz_path.exists():
        with gzip.open(gz_path, "rt", encoding="utf-8") as gf:
            payload = json.load(gf)
        n = int(payload.get("samples_1s") or len(payload.get("power_1s", [])))
        df = pd.DataFrame({
            "elapsed_sec": np.arange(0, n, dtype=int),
            "power": _to_float_array(payload.get("power_1s", [])),
            "cadence": _to_float_array(payload.get("cadence_1s", [])),
            "heart_rate": _to_float_array(payload.get("hr_1s", [])),
        })
        src_name = str(payload.get("source_fit_file") or gz_path.name)
        return df, Path(src_name), {}

    return None, None, {}


def _detect_workout_blocks(
    power_arr: np.ndarray,
    hr_arr: np.ndarray,
    cad_arr: np.ndarray,
    ftp: float,
) -> List[DetectedBlock]:
    """
    ① 자동 블록 감지 알고리즘
    - 30초 롤링 파워 및 케이던스 변화율을 결합해 워밍업 / 메인 인터벌(WORK) / 회복(RECOVERY) / 쿨다운 구분
    - 메인 인터벌 판정 기준: 공식 FTP의 76% 이상(Z3 이상) 또는 저케이던스 토크 부하가 90초 이상 지속되는 구간
    """
    n = len(power_arr)
    if n == 0:
        return []

    p_filled = np.where(np.isfinite(power_arr) & (power_arr >= 0), power_arr, 0.0)
    if n < 180:
        avg_p = round(float(np.mean(p_filled)), 1)
        max_p = round(float(np.max(p_filled)), 1)
        avg_h = round(float(np.nanmean(hr_arr)), 1) if np.any(np.isfinite(hr_arr)) else None
        max_h = round(float(np.nanmax(hr_arr)), 1) if np.any(np.isfinite(hr_arr)) else None
        avg_c = round(float(np.nanmean(cad_arr)), 1) if np.any(np.isfinite(cad_arr)) else None
        ef = round(avg_p / avg_h, 2) if (avg_h and avg_h > 0) else None
        return [
            DetectedBlock(
                block_type="WORK",
                label="전체 세션",
                start_sec=0,
                end_sec=n,
                duration_sec=n,
                avg_power=avg_p,
                max_power=max_p,
                avg_hr=avg_h,
                max_hr=max_h,
                avg_cadence=avg_c,
                ef=ef,
                hr_delta_prev=None,
            )
        ]

    roll_30 = pd.Series(p_filled).rolling(window=30, min_periods=1).mean().to_numpy(dtype=float)
    session_p75 = float(np.percentile(roll_30[roll_30 > 0], 75)) if np.any(roll_30 > 0) else ftp * 0.76
    session_med = float(np.median(roll_30[roll_30 > 0])) if np.any(roll_30 > 0) else ftp * 0.55

    work_threshold = max(min(ftp * 0.76, max(session_p75 * 0.93, session_med + 12.0)), ftp * 0.62)
    cad_roll_20 = pd.Series(cad_arr).rolling(window=20, min_periods=1).mean().to_numpy(dtype=float)
    torque_mask = np.isfinite(cad_roll_20) & (cad_roll_20 > 0) & (cad_roll_20 <= 72.0) & (roll_30 >= session_med * 1.05)

    raw_work = (roll_30 >= work_threshold) | torque_mask

    s_work = pd.Series(raw_work.astype(int))
    closed = s_work.rolling(window=25, min_periods=1, center=True).max()
    cleaned_work = (closed.rolling(window=25, min_periods=1, center=True).min() > 0).to_numpy(dtype=bool)

    segments: List[Tuple[bool, int, int]] = []
    cur_state = bool(cleaned_work[0])
    start_idx = 0
    for i in range(1, n):
        st = bool(cleaned_work[i])
        if st != cur_state:
            segments.append((cur_state, start_idx, i))
            cur_state = st
            start_idx = i
    segments.append((cur_state, start_idx, n))

    filtered_segs: List[Tuple[bool, int, int]] = []
    for st, s_idx, e_idx in segments:
        dur = e_idx - s_idx
        if st and dur < 90:
            if filtered_segs and not filtered_segs[-1][0]:
                prev_st, prev_s, _ = filtered_segs[-1]
                filtered_segs[-1] = (prev_st, prev_s, e_idx)
            else:
                filtered_segs.append((False, s_idx, e_idx))
        elif filtered_segs and filtered_segs[-1][0] == st:
            prev_st, prev_s, _ = filtered_segs[-1]
            filtered_segs[-1] = (prev_st, prev_s, e_idx)
        else:
            filtered_segs.append((st, s_idx, e_idx))

    work_indices = [i for i, (st, _, _) in enumerate(filtered_segs) if st]
    first_work_i = work_indices[0] if work_indices else None
    last_work_i = work_indices[-1] if work_indices else None

    blocks: List[DetectedBlock] = []
    work_num = 0
    rec_num = 0
    prev_end_hr: Optional[float] = None

    for i, (is_work, s_idx, e_idx) in enumerate(filtered_segs):
        if e_idx <= s_idx:
            continue
        if is_work:
            b_type = "WORK"
            work_num += 1
            label = f"메인 #{work_num}"
        else:
            if first_work_i is not None and i < first_work_i:
                b_type = "WARMUP"
                label = "워밍업"
            elif last_work_i is not None and i > last_work_i:
                b_type = "COOLDOWN"
                label = "쿨다운"
            else:
                b_type = "RECOVERY"
                rec_num += 1
                label = f"회복 #{rec_num}"

        p_slice = p_filled[s_idx:e_idx]
        hr_slice = hr_arr[s_idx:e_idx]
        cad_slice = cad_arr[s_idx:e_idx]

        avg_p = round(float(np.mean(p_slice)), 1)
        max_p = round(float(np.max(p_slice)), 1)

        hr_valid = hr_slice[np.isfinite(hr_slice) & (hr_slice > 0)]
        avg_h = round(float(np.mean(hr_valid)), 1) if len(hr_valid) > 0 else None
        max_h = round(float(np.max(hr_valid)), 1) if len(hr_valid) > 0 else None

        tail_hr = hr_slice[max(0, len(hr_slice) - 15) :]
        tail_hr_valid = tail_hr[np.isfinite(tail_hr) & (tail_hr > 0)]
        cur_end_hr = float(np.mean(tail_hr_valid)) if len(tail_hr_valid) > 0 else (float(avg_h) if avg_h else None)

        hr_delta: Optional[float] = None
        if prev_end_hr is not None and cur_end_hr is not None:
            hr_delta = round(cur_end_hr - prev_end_hr, 1)
        if cur_end_hr is not None:
            prev_end_hr = cur_end_hr

        cad_valid = cad_slice[np.isfinite(cad_slice) & (cad_slice > 0)]
        avg_c = round(float(np.mean(cad_valid)), 1) if len(cad_valid) > 0 else None

        ef = round(avg_p / avg_h, 2) if (avg_h and avg_h > 0) else None

        blocks.append(
            DetectedBlock(
                block_type=b_type,
                label=label,
                start_sec=s_idx,
                end_sec=e_idx,
                duration_sec=e_idx - s_idx,
                avg_power=avg_p,
                max_power=max_p,
                avg_hr=avg_h,
                max_hr=max_h,
                avg_cadence=avg_c,
                ef=ef,
                hr_delta_prev=hr_delta,
            )
        )

    return blocks


def build_summary(
    fit_path: Optional[Path] = None,
    ftp: float = FTP_WATTS,
    lthr: float = LTHR_BPM,
) -> Tuple[str, Dict[str, Any]]:
    df, resolved_path, meta = _load_latest_stream(fit_path)
    if df is None or len(df) == 0 or resolved_path is None:
        msg = "WARN 분석할 1초 FIT 시계열 데이터가 없습니다."
        return msg, {}

    power_raw = df["power"].to_numpy(dtype=float)
    hr_raw = df["heart_rate"].to_numpy(dtype=float)
    cad_raw = df["cadence"].to_numpy(dtype=float)

    n_sec = len(power_raw)
    duration_min = round(n_sec / 60.0, 1)

    p_filled = np.where(np.isfinite(power_raw) & (power_raw >= 0), power_raw, 0.0)
    avg_power = round(float(np.mean(p_filled)), 1)
    np_power = _compute_np(p_filled)
    vi = round(np_power / avg_power, 2) if avg_power > 0 else 0.0
    intensity_factor = round(np_power / ftp, 2) if ftp > 0 else 0.0
    tss = round(((n_sec * np_power * intensity_factor) / (ftp * 3600.0)) * 100.0, 1) if ftp > 0 else 0.0
    work_kj = round(float(np.sum(p_filled)) / 1000.0, 1)
    zero_pct = round(float(np.mean(p_filled <= 0)) * 100.0, 1)

    mmp_5s = _compute_mmp(p_filled, 5)
    mmp_30s = _compute_mmp(p_filled, 30)
    mmp_1m = _compute_mmp(p_filled, 60)
    mmp_5m = _compute_mmp(p_filled, 300)
    mmp_20m = _compute_mmp(p_filled, 1200)
    session_eftp = round(mmp_20m * 0.95, 1) if mmp_20m is not None else None

    hr_valid = hr_raw[np.isfinite(hr_raw) & (hr_raw > 0)]
    avg_hr = round(float(np.mean(hr_valid)), 1) if len(hr_valid) > 0 else None
    max_hr_val = round(float(np.max(hr_valid)), 1) if len(hr_valid) > 0 else None

    cad_valid = cad_raw[np.isfinite(cad_raw) & (cad_raw > 0)]
    avg_cad = round(float(np.mean(cad_valid)), 1) if len(cad_valid) > 0 else None

    decoupling = _compute_decoupling(power_raw, hr_raw)
    zones = _zone_times_minutes(p_filled, ftp)
    hr_zones = _hr_zone_times_minutes(hr_raw, lthr)

    blocks = _detect_workout_blocks(power_raw, hr_raw, cad_raw, ftp)
    work_blocks = [b for b in blocks if b.block_type == "WORK"]
    rec_blocks = [b for b in blocks if b.block_type == "RECOVERY"]

    lines: List[str] = []
    lines.append(f"직전 라이딩 워크아웃 블록 정밀 요약 {resolved_path.name}")
    lines.append(
        f"1. 전체 요약 시간 {duration_min}분 {n_sec}초 / 평균파워 {avg_power}W / NP {np_power}W / "
        f"VI {vi} / IF {intensity_factor} / TSS {int(ftp)}W 기준 {tss} / 일량 {work_kj}kJ / 0W비율 {zero_pct}%"
    )
    lines.append(
        f"2. 최대출력 MMP 5s {mmp_5s if mmp_5s is not None else 'NA'}W / "
        f"30s {mmp_30s if mmp_30s is not None else 'NA'}W / "
        f"1m {mmp_1m if mmp_1m is not None else 'NA'}W / "
        f"5m {mmp_5m if mmp_5m is not None else 'NA'}W / "
        f"20m {mmp_20m if mmp_20m is not None else 'NA'}W"
        + (f" / 세션 eFTP 참고 {session_eftp}W" if session_eftp is not None else "")
    )
    lines.append(
        f"3. 파워존체류 분 Z1 {zones['Z1']} / Z2 {zones['Z2']} / Z3 {zones['Z3']} / Z4 {zones['Z4']} / Z5 {zones['Z5']} / "
        f"심박존체류 LTHR {int(lthr)}bpm 기준 분 HRZ1 {hr_zones['Z1']} / HRZ2 {hr_zones['Z2']} / "
        f"HRZ3 {hr_zones['Z3']} / HRZ4 {hr_zones['Z4']} / HRZ5 {hr_zones['Z5']} / "
        f"평균심박 {avg_hr if avg_hr is not None else 'NA'}bpm 최대 {max_hr_val if max_hr_val is not None else 'NA'}bpm / "
        f"평균케이던스 {avg_cad if avg_cad is not None else 'NA'}rpm / 디커플링 {decoupling if decoupling is not None else 'NA'}%"
    )
    lines.append(f"4. 워크아웃 블록별 수행 분석 1 자동 블록 감지 {len(blocks)}개 블록")

    for b in blocks:
        b_min = round(b.duration_sec / 60.0, 1)
        ftp_pct = round((b.avg_power / ftp) * 100.0, 0) if ftp > 0 else 0.0
        hr_part = f"심박 {b.avg_hr}bpm 최대 {b.max_hr}" if (b.avg_hr is not None and b.max_hr is not None) else (f"심박 {b.avg_hr}bpm" if b.avg_hr is not None else "심박 NA")
        cad_part = f"케이던스 {b.avg_cadence}rpm" if b.avg_cadence is not None else "케이던스 NA"
        ef_part = f"EF {b.ef}" if b.ef is not None else ""
        delta_part = ""
        if b.hr_delta_prev is not None:
            sign = "증가 " if b.hr_delta_prev > 0 else ("감소 " if b.hr_delta_prev < 0 else "")
            delta_part = f" / 전구간대비심박 {sign}{abs(b.hr_delta_prev)}bpm"

        extra_bits = [x for x in (hr_part, cad_part, ef_part) if x]
        lines.append(
            f"{b.label} {_format_mmss(b.start_sec)}~{_format_mmss(b.end_sec)} {b_min}분 / "
            f"평균 {b.avg_power}W {int(ftp_pct)}% FTP 최대 {b.max_power}W / "
            + " / ".join(extra_bits)
            + delta_part
        )

    lines.append("5. 인터벌 핵심 진단")
    if len(work_blocks) >= 2:
        first_w = work_blocks[0]
        last_w = work_blocks[-1]
        p_diff_pct = round(((last_w.avg_power - first_w.avg_power) / first_w.avg_power) * 100.0, 1) if first_w.avg_power > 0 else 0.0
        p_dir = "상승 " if p_diff_pct > 0 else ("하락 " if p_diff_pct < 0 else "")
        lines.append(
            f"메인 파워 추이 {first_w.label} {first_w.avg_power}W 에서 {last_w.label} {last_w.avg_power}W 로 {p_dir}{abs(p_diff_pct)}%"
        )
        if first_w.avg_hr is not None and last_w.avg_hr is not None:
            hr_diff = round(last_w.avg_hr - first_w.avg_hr, 1)
            h_dir = "상승 " if hr_diff > 0 else ("하락 " if hr_diff < 0 else "")
            lines.append(
                f"메인 심박 추이 {first_w.label} {first_w.avg_hr}bpm 에서 {last_w.label} {last_w.avg_hr}bpm 로 {h_dir}{abs(hr_diff)}bpm"
            )
    elif len(work_blocks) == 1:
        w0 = work_blocks[0]
        lines.append(
            f"단일 메인 구간 {w0.label} {round(w0.duration_sec / 60.0, 1)}분 / 평균 {w0.avg_power}W / 케이던스 {w0.avg_cadence or 'NA'}rpm"
        )
    else:
        lines.append("뚜렷한 고부하 인터벌 블록 미탐지 기초 유산소 연속 주행 성격")

    rec_drops = [b.hr_delta_prev for b in rec_blocks if b.hr_delta_prev is not None]
    if rec_drops:
        avg_drop = round(float(np.mean(rec_drops)), 1)
        lines.append(f"회복 구간 심박 변화 평균 {avg_drop}bpm")

    summary_text = "\n".join(lines) + "\n"

    summary_row: Dict[str, Any] = {
        "fit_file": resolved_path.name,
        "duration_min": duration_min,
        "avg_power": avg_power,
        "norm_power_NP": np_power,
        "vi": vi,
        "intensity_factor": intensity_factor,
        "tss_194w": tss,
        "work_kj": work_kj,
        "zero_power_pct": zero_pct,
        "mmp_5s": mmp_5s,
        "mmp_1m": mmp_1m,
        "mmp_5m": mmp_5m,
        "mmp_20m": mmp_20m,
        "session_eftp_ref": session_eftp,
        "avg_hr": avg_hr,
        "max_hr": max_hr_val,
        "hr_z1_min": hr_zones["Z1"],
        "hr_z2_min": hr_zones["Z2"],
        "hr_z3_min": hr_zones["Z3"],
        "hr_z4_min": hr_zones["Z4"],
        "hr_z5_min": hr_zones["Z5"],
        "avg_cadence": avg_cad,
        "decoupling_pct": decoupling,
        "block_count": len(blocks),
        "work_block_count": len(work_blocks),
        "summary_line": _sanitize_summary_line(summary_text),
    }
    return summary_text, summary_row


def main() -> None:
    summary_text, summary_row = build_summary(ftp=FTP_WATTS, lthr=LTHR_BPM)
    OUT_TXT.write_text(summary_text, encoding="utf-8")
    if summary_row:
        pd.DataFrame([summary_row]).to_csv(
            OUT_CSV,
            index=False,
            encoding="utf-8-sig",
            quoting=csv.QUOTE_ALL,
        )
    print(summary_text)
    print(f"[OK] 저장 완료: {OUT_TXT} / {OUT_CSV}")


if __name__ == "__main__":
    main()
