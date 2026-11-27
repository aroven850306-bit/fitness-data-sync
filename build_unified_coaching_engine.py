#!/usr/bin/env python3
"""
통합 사이클링 코칭 분석 엔진 (일·주·월·대회 전 4계층 판단)
- 7종 데이터 자산(fit_analysis_report.csv, garmin_activities.csv, strava_activities.csv,
  strava_segments.csv, latest_ride_1s_summary.txt, garmin_health.csv, readiness_v2_summary.csv) 통합
- 단일 원천 정규화(1초 FIT 우선, 공식 FTP 194W 통일 기준 TSS 및 CTL/ATL/TSB/ACWR 시계열 산출)
- 일·주·월·대회 전 4계층 코칭 지표 산출 후 산출물 저장:
  1) unified_daily_metrics.csv
  2) unified_coaching_state.json
  3) unified_coaching_summary.txt
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


KST = ZoneInfo("Asia/Seoul")
DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))

# 고정 기준치
DEFAULT_FTP = 194.0
DEFAULT_LTHR = 147.0
DEFAULT_MAX_HR = 173.0
TARGET_FTP = 240.0
CURRENT_WEIGHT_KG = 77.4
TARGET_WEIGHT_KG = 72.0
EVENT_DATE = date(2027, 4, 3)

# EWMA 시정수
CTL_TAU = 42.0
ATL_TAU = 7.0


def _to_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if math.isfinite(f) else None
    s = str(val).strip()
    if not s or s.upper() in ("NAN", "NONE", "NULL", "N/A", "확인 못 함", "PARTIAL"):
        return None
    s_clean = re.sub(r"[^\d.\-+]", "", s)
    if not s_clean or s_clean in (".", "-", "+", "-.", "+."):
        return None
    try:
        f = float(s_clean)
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _parse_date_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m2 = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m2:
        return f"{int(m2.group(1)):04d}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    return None


def _read_csv_safe(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        df.columns = [str(c).strip() for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame()


def _get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    val = _to_float(raw)
    return val if (val is not None and val > 0) else default


def _compute_tss(duration_sec: float, np_watts: Optional[float], ftp: float, fallback_tss: Optional[float] = None) -> float:
    if ftp <= 0:
        ftp = DEFAULT_FTP
    if np_watts is not None and np_watts > 0 and duration_sec > 0:
        intensity_factor = np_watts / ftp
        tss = (duration_sec * np_watts * intensity_factor) / (ftp * 3600.0) * 100.0
        return round(max(0.0, tss), 1)
    if fallback_tss is not None and fallback_tss >= 0:
        return round(fallback_tss, 1)
    return 0.0


def _get_phase_targets(as_of: date) -> Dict[str, Any]:
    if as_of <= date(2026, 12, 6):
        return {
            "phase_name": "기초·역치 빌드업 & 1차 테스트",
            "low_aerobic_pct": (55.0, 65.0),
            "high_aerobic_pct": (25.0, 35.0),
            "anaerobic_pct": (10.0, 15.0),
        }
    if as_of <= date(2027, 1, 17):
        return {
            "phase_name": "FTP 240W 1차 도약 블록",
            "low_aerobic_pct": (50.0, 60.0),
            "high_aerobic_pct": (30.0, 40.0),
            "anaerobic_pct": (10.0, 15.0),
        }
    if as_of <= date(2027, 2, 28):
        return {
            "phase_name": "역치 지속력 심화",
            "low_aerobic_pct": (50.0, 60.0),
            "high_aerobic_pct": (30.0, 40.0),
            "anaerobic_pct": (10.0, 15.0),
        }
    if as_of <= date(2027, 3, 21):
        return {
            "phase_name": "그란폰도 특화 (9.4% 업힐 대비)",
            "low_aerobic_pct": (45.0, 55.0),
            "high_aerobic_pct": (30.0, 40.0),
            "anaerobic_pct": (12.0, 20.0),
        }
    return {
        "phase_name": "테이퍼",
        "low_aerobic_pct": (60.0, 75.0),
        "high_aerobic_pct": (15.0, 25.0),
        "anaerobic_pct": (5.0, 10.0),
    }


def _pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def build_unified_sessions(ftp: float) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    fit_df = _read_csv_safe(DATA_DIR / "fit_analysis_report.csv")
    garmin_act_df = _read_csv_safe(DATA_DIR / "garmin_activities.csv")
    strava_act_df = _read_csv_safe(DATA_DIR / "strava_activities.csv")
    strava_seg_df = _read_csv_safe(DATA_DIR / "strava_segments.csv")

    records: List[Dict[str, Any]] = []

    # 1순위: fit_analysis_report.csv
    if not fit_df.empty:
        c_date = _pick_col(fit_df, ["date", "start_date", "activity_date", "timestamp"])
        c_dur = _pick_col(fit_df, ["duration_sec", "moving_time_sec", "elapsed_sec", "duration"])
        c_np = _pick_col(fit_df, ["normalized_power", "np", "np_watts", "avg_power", "power"])
        c_tss = _pick_col(fit_df, ["tss", "training_stress_score"])
        c_hr = _pick_col(fit_df, ["avg_hr", "average_heart_rate", "hr"])
        c_dec = _pick_col(fit_df, ["aerobic_decoupling_pct", "decoupling_pct", "pw_hr"])
        for idx, row in fit_df.iterrows():
            d_str = _parse_date_str(row.get(c_date)) if c_date else None
            if not d_str:
                continue
            dur = _to_float(row.get(c_dur)) if c_dur else None
            if dur is not None and 0 < dur < 120:
                dur = dur * 60.0
            np_val = _to_float(row.get(c_np)) if c_np else None
            raw_tss = _to_float(row.get(c_tss)) if c_tss else None
            tss = _compute_tss(dur or 0.0, np_val, ftp, raw_tss)
            records.append({
                "date": d_str,
                "source": "fit",
                "duration_sec": dur or 0.0,
                "np_watts": np_val,
                "tss": tss,
                "avg_hr": _to_float(row.get(c_hr)) if c_hr else None,
                "decoupling_pct": _to_float(row.get(c_dec)) if c_dec else None,
                "rpe": None,
                "calories": None,
                "segment_count": 0,
            })

    # 2순위: garmin_activities.csv (동일 날짜 FIT가 없을 때만 보완)
    fit_dates = {r["date"] for r in records}
    if not garmin_act_df.empty:
        c_date = _pick_col(garmin_act_df, ["date", "start_time_local", "start_date", "activity_date"])
        c_dur = _pick_col(garmin_act_df, ["duration_sec", "duration", "elapsed_time_sec", "moving_time_sec"])
        c_np = _pick_col(garmin_act_df, ["norm_power", "normalized_power", "np", "avg_power", "average_power"])
        c_tss = _pick_col(garmin_act_df, ["training_stress_score", "tss", "activity_training_load"])
        c_hr = _pick_col(garmin_act_df, ["avg_hr", "average_hr", "average_heart_rate"])
        for _, row in garmin_act_df.iterrows():
            d_str = _parse_date_str(row.get(c_date)) if c_date else None
            if not d_str or d_str in fit_dates:
                continue
            dur = _to_float(row.get(c_dur)) if c_dur else None
            if dur is not None and 0 < dur < 120:
                dur = dur * 60.0
            np_val = _to_float(row.get(c_np)) if c_np else None
            raw_tss = _to_float(row.get(c_tss)) if c_tss else None
            tss = _compute_tss(dur or 0.0, np_val, ftp, raw_tss)
            records.append({
                "date": d_str,
                "source": "garmin",
                "duration_sec": dur or 0.0,
                "np_watts": np_val,
                "tss": tss,
                "avg_hr": _to_float(row.get(c_hr)) if c_hr else None,
                "decoupling_pct": None,
                "rpe": None,
                "calories": None,
                "segment_count": 0,
            })

    # Strava 보강 정보(RPE, 칼로리, 3순위 백업 세션)
    strava_by_date: Dict[str, Dict[str, Any]] = {}
    if not strava_act_df.empty:
        c_date = _pick_col(strava_act_df, ["date", "start_date_local", "start_date", "activity_date"])
        c_dur = _pick_col(strava_act_df, ["moving_time_sec", "elapsed_time_sec", "duration_sec", "moving_time"])
        c_np = _pick_col(strava_act_df, ["weighted_average_watts", "np", "normalized_power", "average_watts"])
        c_
