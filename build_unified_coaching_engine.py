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
        c_rpe = _pick_col(strava_act_df, ["perceived_exertion", "rpe"])
        c_cal = _pick_col(strava_act_df, ["calories", "kilojoules"])
        for _, row in strava_act_df.iterrows():
            d_str = _parse_date_str(row.get(c_date)) if c_date else None
            if not d_str:
                continue
            rpe_val = _to_float(row.get(c_rpe)) if c_rpe else None
            cal_val = _to_float(row.get(c_cal)) if c_cal else None
            if d_str not in strava_by_date:
                strava_by_date[d_str] = {"rpe": rpe_val, "calories": cal_val or 0.0}
            else:
                if rpe_val is not None:
                    strava_by_date[d_str]["rpe"] = rpe_val
                if cal_val is not None:
                    strava_by_date[d_str]["calories"] = (strava_by_date[d_str].get("calories") or 0.0) + cal_val

            existing_dates = {r["date"] for r in records}
            if d_str not in existing_dates:
                dur = _to_float(row.get(c_dur)) if c_dur else None
                if dur is not None and 0 < dur < 120:
                    dur = dur * 60.0
                np_val = _to_float(row.get(c_np)) if c_np else None
                tss = _compute_tss(dur or 0.0, np_val, ftp, None)
                records.append({
                    "date": d_str,
                    "source": "strava",
                    "duration_sec": dur or 0.0,
                    "np_watts": np_val,
                    "tss": tss,
                    "avg_hr": None,
                    "decoupling_pct": None,
                    "rpe": rpe_val,
                    "calories": cal_val,
                    "segment_count": 0,
                })

    seg_by_date: Dict[str, int] = {}
    if not strava_seg_df.empty:
        c_sdate = _pick_col(strava_seg_df, ["date", "start_date_local", "start_date", "activity_date"])
        if c_sdate:
            for _, row in strava_seg_df.iterrows():
                d_str = _parse_date_str(row.get(c_sdate))
                if d_str:
                    seg_by_date[d_str] = seg_by_date.get(d_str, 0) + 1

    for r in records:
        d_str = r["date"]
        if d_str in strava_by_date:
            if r["rpe"] is None:
                r["rpe"] = strava_by_date[d_str].get("rpe")
            if r["calories"] is None and strava_by_date[d_str].get("calories"):
                r["calories"] = strava_by_date[d_str].get("calories")
        r["segment_count"] = seg_by_date.get(d_str, 0)

    sessions_df = pd.DataFrame(records)
    meta = {
        "fit_rows": len(fit_df),
        "garmin_activity_rows": len(garmin_act_df),
        "strava_activity_rows": len(strava_act_df),
        "strava_segment_rows": len(strava_seg_df),
        "unified_session_rows": len(sessions_df),
    }
    return sessions_df, meta


def build_daily_timeseries(sessions_df: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
    health_df = _read_csv_safe(DATA_DIR / "garmin_health.csv")
    c_hdate = _pick_col(health_df, ["date", "calendar_date", "day"]) if not health_df.empty else None

    health_map: Dict[str, Dict[str, Any]] = {}
    health_dates: List[date] = []
    if not health_df.empty and c_hdate:
        for _, row in health_df.iterrows():
            d_str = _parse_date_str(row.get(c_hdate))
            if d_str:
                health_map[d_str] = row.to_dict()
                health_dates.append(date.fromisoformat(d_str))

    session_dates: List[date] = []
    daily_tss_map: Dict[str, float] = {}
    daily_dur_map: Dict[str, float] = {}
    daily_np_vals: Dict[str, List[float]] = {}
    daily_hr_vals: Dict[str, List[float]] = {}
    daily_dec_vals: Dict[str, List[float]] = {}
    daily_rpe_vals: Dict[str, List[float]] = {}
    daily_cal_map: Dict[str, float] = {}
    daily_seg_map: Dict[str, int] = {}
    daily_src_map: Dict[str, List[str]] = {}

    if not sessions_df.empty:
        for _, r in sessions_df.iterrows():
            d_str = r["date"]
            session_dates.append(date.fromisoformat(d_str))
            daily_tss_map[d_str] = daily_tss_map.get(d_str, 0.0) + float(r.get("tss") or 0.0)
            daily_dur_map[d_str] = daily_dur_map.get(d_str, 0.0) + float(r.get("duration_sec") or 0.0)
            if r.get("np_watts") is not None and pd.notna(r.get("np_watts")):
                daily_np_vals.setdefault(d_str, []).append(float(r["np_watts"]))
            if r.get("avg_hr") is not None and pd.notna(r.get("avg_hr")):
                daily_hr_vals.setdefault(d_str, []).append(float(r["avg_hr"]))
            if r.get("decoupling_pct") is not None and pd.notna(r.get("decoupling_pct")):
                daily_dec_vals.setdefault(d_str, []).append(float(r["decoupling_pct"]))
            if r.get("rpe") is not None and pd.notna(r.get("rpe")):
                daily_rpe_vals.setdefault(d_str, []).append(float(r["rpe"]))
            if r.get("calories") is not None and pd.notna(r.get("calories")):
                daily_cal_map[d_str] = daily_cal_map.get(d_str, 0.0) + float(r["calories"])
            daily_seg_map[d_str] = daily_seg_map.get(d_str, 0) + int(r.get("segment_count") or 0)
            src = str(r.get("source") or "")
            if src and src not in daily_src_map.setdefault(d_str, []):
                daily_src_map[d_str].append(src)

    all_candidate_dates = session_dates + health_dates
    start_date = min(all_candidate_dates) if all_candidate_dates else (as_of_date - timedelta(days=42))
    if start_date > as_of_date:
        start_date = as_of_date - timedelta(days=42)

    alpha_ctl = 1.0 - math.exp(-1.0 / CTL_TAU)
    alpha_atl = 1.0 - math.exp(-1.0 / ATL_TAU)

    ctl = 0.0
    atl = 0.0
    rows: List[Dict[str, Any]] = []
    tss_history: List[float] = []

    cur = start_date
    while cur <= as_of_date:
        d_str = cur.isoformat()
        day_tss = round(daily_tss_map.get(d_str, 0.0), 1)
        tss_history.append(day_tss)

        ctl = ctl + alpha_ctl * (day_tss - ctl)
        atl = atl + alpha_atl * (day_tss - atl)
        tsb = ctl - atl

        # 7일 합산 / (28일 합산 / 4)
        last_7_sum = sum(tss_history[-7:])
        last_28_sum = sum(tss_history[-28:])
        weekly_avg_28 = (last_28_sum / 4.0) if last_28_sum > 0 else 0.0
        acwr = round(last_7_sum / weekly_avg_28, 2) if weekly_avg_28 > 0 else None

        h_row = health_map.get(d_str, {})
        c_low = _pick_col(health_df, ["training_load_low_aerobic", "low_aerobic_load", "low_aerobic"]) if not health_df.empty else None
        c_high = _pick_col(health_df, ["training_load_high_aerobic", "high_aerobic_load", "high_aerobic"]) if not health_df.empty else None
        c_ana = _pick_col(health_df, ["training_load_anaerobic", "anaerobic_load", "anaerobic"]) if not health_df.empty else None
        c_sleep_h = _pick_col(health_df, ["sleep_hours", "sleep_duration_hours", "total_sleep_hours"]) if not health_df.empty else None
        c_sleep_s = _pick_col(health_df, ["sleep_score"]) if not health_df.empty else None
        c_hrv = _pick_col(health_df, ["hrv_last_night_avg", "hrv_weekly_avg", "hrv_avg", "hrv"]) if not health_df.empty else None
        c_rhr = _pick_col(health_df, ["resting_hr", "rhr", "resting_heart_rate"]) if not health_df.empty else None
        c_stress = _pick_col(health_df, ["avg_stress", "average_stress", "stress_avg"]) if not health_df.empty else None
        c_bb = _pick_col(health_df, ["body_battery_current", "body_battery_morning", "body_battery"]) if not health_df.empty else None

        low_aero = _to_float(h_row.get(c_low)) if c_low else None
        high_aero = _to_float(h_row.get(c_high)) if c_high else None
        anaer = _to_float(h_row.get(c_ana)) if c_ana else None

        np_avg = round(float(np.mean(daily_np_vals[d_str])), 1) if d_str in daily_np_vals else None
        hr_avg = round(float(np.mean(daily_hr_vals[d_str])), 1) if d_str in daily_hr_vals else None
        dec_avg = round(float(np.mean(daily_dec_vals[d_str])), 2) if d_str in daily_dec_vals else None
        rpe_avg = round(float(np.mean(daily_rpe_vals[d_str])), 1) if d_str in daily_rpe_vals else None
        ef_val = round(np_avg / hr_avg, 2) if (np_avg is not None and hr_avg is not None and hr_avg > 0) else None

        rows.append({
            "date": d_str,
            "data_sources": "+".join(daily_src_map.get(d_str, [])) or "none",
            "duration_min": round(daily_dur_map.get(d_str, 0.0) / 60.0, 1),
            "tss": day_tss,
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
            "acwr": acwr,
            "np_watts": np_avg,
            "avg_hr": hr_avg,
            "ef": ef_val,
            "aerobic_decoupling_pct": dec_avg,
            "rpe": rpe_avg,
            "calories": round(daily_cal_map[d_str], 0) if d_str in daily_cal_map else None,
            "segment_count": daily_seg_map.get(d_str, 0),
            "low_aerobic_load": low_aero,
            "high_aerobic_load": high_aero,
            "anaerobic_load": anaer,
            "sleep_hours": _to_float(h_row.get(c_sleep_h)) if c_sleep_h else None,
            "sleep_score": _to_float(h_row.get(c_sleep_s)) if c_sleep_s else None,
            "hrv_last_night_avg": _to_float(h_row.get(c_hrv)) if c_hrv else None,
            "resting_hr": _to_float(h_row.get(c_rhr)) if c_rhr else None,
            "avg_stress": _to_float(h_row.get(c_stress)) if c_stress else None,
            "body_battery_current": _to_float(h_row.get(c_bb)) if c_bb else None,
        })
        cur += timedelta(days=1)

    return pd.DataFrame(rows)


def _read_latest_1s_summary_text() -> str:
    txt_path = DATA_DIR / "latest_ride_1s_summary.txt"
    if not txt_path.exists():
        return ""
    try:
        return txt_path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def compute_coaching_state(daily_df: pd.DataFrame, now_kst: datetime, ftp: float, lthr: float, max_hr: float) -> Dict[str, Any]:
    as_of_date = now_kst.date()
    today_str = as_of_date.isoformat()

    readiness_df = _read_csv_safe(DATA_DIR / "readiness_v2_summary.csv")
    readiness_row: Dict[str, Any] = {}
    if not readiness_df.empty:
        readiness_row = readiness_df.iloc[-1].to_dict()

    today_row = daily_df[daily_df["date"] == today_str].iloc[-1].to_dict() if (not daily_df.empty and (daily_df["date"] == today_str).any()) else {}
    recent_7_df = daily_df.tail(7)
    recent_28_df = daily_df.tail(28)

    # 당일 훈련 완료 후 익일 기상 동기화 전 '보류' 전환 규칙
    today_tss = _to_float(today_row.get("tss")) or 0.0
    today_dur_min = _to_float(today_row.get("duration_min")) or 0.0
    has_today_health = any(
        _to_float(today_row.get(col)) is not None
        for col in ("sleep_hours", "hrv_last_night_avg", "resting_hr", "body_battery_current")
    )
    workout_completed_today = (today_tss >= 15.0 or today_dur_min >= 20.0) and (now_kst.hour >= 10)
    is_hold_state = workout_completed_today and not has_today_health

    raw_score = _to_float(readiness_row.get("score"))
    raw_grade = str(readiness_row.get("permission_grade") or "").strip().upper()
    raw_label = str(readiness_row.get("permission_label") or "").strip()

    if is_hold_state:
        readiness_status = "⏸ 보류 (내일 기상 후 동기화 대기)"
        effective_grade = "HOLD"
        effective_score = None
    else:
        readiness_status = raw_label or ("확인 못 함" if raw_score is None else f"{raw_score:.1f}점")
        effective_grade = raw_grade if raw_grade in ("A", "B", "C", "D") else "확인 못 함"
        effective_score = raw_score

    # 3일 누적 수면 부채
    last_3_sleep = [
        _to_float(v)
        for v in daily_df.tail(3)["sleep_hours"].tolist()
        if _to_float(v) is not None
    ] if not daily_df.empty else []
    sleep_debt_3d = round(max(0.0, 21.0 - sum(last_3_sleep)), 1) if last_3_sleep else None

    # HRV / RHR 7일 편차
    hrv_today = _to_float(today_row.get("hrv_last_night_avg"))
    hrv_7d_vals = [_to_float(v) for v in recent_7_df["hrv_last_night_avg"].tolist() if _to_float(v) is not None] if not recent_7_df.empty else []
    hrv_7d_avg = round(float(np.mean(hrv_7d_vals)), 1) if hrv_7d_vals else None
    hrv_delta_pct = round(((hrv_today - hrv_7d_avg) / hrv_7d_avg) * 100.0, 1) if (hrv_today is not None and hrv_7d_avg and hrv_7d_avg > 0) else None

    rhr_today = _to_float(today_row.get("resting_hr"))
    rhr_7d_vals = [_to_float(v) for v in recent_7_df["resting_hr"].tolist() if _to_float(v) is not None] if not recent_7_df.empty else []
    rhr_7d_avg = round(float(np.mean(rhr_7d_vals)), 1) if rhr_7d_vals else None
    rhr_delta_bpm = round(rhr_today - rhr_7d_avg, 1) if (rhr_today is not None and rhr_7d_avg is not None) else None

    # 부하 포커스 3종 (빈 행 폴백)
    latest_low = _to_float(today_row.get("low_aerobic_load"))
    latest_high = _to_float(today_row.get("high_aerobic_load"))
    latest_ana = _to_float(today_row.get("anaerobic_load"))
    load_focus_fallback_date = today_str
    if latest_low is None and latest_high is None and latest_ana is None and not daily_df.empty:
        valid_lf = daily_df[
            daily_df["low_aerobic_load"].apply(lambda x: _to_float(x) is not None)
            | daily_df["high_aerobic_load"].apply(lambda x: _to_float(x) is not None)
            | daily_df["anaerobic_load"].apply(lambda x: _to_float(x) is not None)
        ]
        if not valid_lf.empty:
            fb_row = valid_lf.iloc[-1].to_dict()
            latest_low = _to_float(fb_row.get("low_aerobic_load"))
            latest_high = _to_float(fb_row.get("high_aerobic_load"))
            latest_ana = _to_float(fb_row.get("anaerobic_load"))
            load_focus_fallback_date = str(fb_row.get("date"))

    lf_sum = (latest_low or 0.0) + (latest_high or 0.0) + (latest_ana or 0.0)
    lf_ratios = {
        "low_aerobic_pct": round((latest_low or 0.0) / lf_sum * 100.0, 1) if lf_sum > 0 else None,
        "high_aerobic_pct": round((latest_high or 0.0) / lf_sum * 100.0, 1) if lf_sum > 0 else None,
        "anaerobic_pct": round((latest_ana or 0.0) / lf_sum * 100.0, 1) if lf_sum > 0 else None,
    }

    # 계층 2: 주간 지표
    tss_7d = round(sum(_to_float(v) or 0.0 for v in recent_7_df["tss"].tolist()), 1) if not recent_7_df.empty else 0.0
    weekly_target_tss = 260.0
    weekly_tss_achievement_pct = round((tss_7d / weekly_target_tss) * 100.0, 1) if weekly_target_tss > 0 else None

    tss_list_7d = [_to_float(v) or 0.0 for v in recent_7_df["tss"].tolist()] if not recent_7_df.empty else []
    tss_mean_7d = float(np.mean(tss_list_7d)) if tss_list_7d else 0.0
    tss_std_7d = float(np.std(tss_list_7d)) if tss_list_7d else 0.0
    monotony_7d = round(tss_mean_7d / tss_std_7d, 2) if tss_std_7d > 0 else None
    strain_7d = round(tss_7d * monotony_7d, 1) if (monotony_7d is not None) else None

    ctl_now = _to_float(today_row.get("ctl"))
    ctl_7d_ago = _to_float(daily_df.iloc[-8]["ctl"]) if len(daily_df) >= 8 else (_to_float(daily_df.iloc[0]["ctl"]) if not daily_df.empty else None)
    ctl_ramp_rate = round(ctl_now - ctl_7d_ago, 1) if (ctl_now is not None and ctl_7d_ago is not None) else None

    # 계층 3: 월간 지표 (28일)
    ef_vals_28d = [_to_float(v) for v in recent_28_df["ef"].tolist() if _to_float(v) is not None] if not recent_28_df.empty else []
    ef_28d_avg = round(float(np.mean(ef_vals_28d)), 2) if ef_vals_28d else None
    np_vals_28d = [_to_float(v) for v in recent_28_df["np_watts"].tolist() if _to_float(v) is not None] if not recent_28_df.empty else []
    peak_np_28d = round(max(np_vals_28d), 1) if np_vals_28d else None
    candidate_eftp = round(peak_np_28d * 0.95, 1) if (peak_np_28d is not None and peak_np_28d * 0.95 > ftp) else None

    # 계층 4: 대회 전 지표
    days_to_event = (EVENT_DATE - as_of_date).days
    current_wkg = round(ftp / CURRENT_WEIGHT_KG, 2)
    target_wkg = round(TARGET_FTP / TARGET_WEIGHT_KG, 2)
    ftp_progress_pct = round((ftp / TARGET_FTP) * 100.0, 1)

    phase = _get_phase_targets(as_of_date)
    latest_1s_txt = _read_latest_1s_summary_text()

    return {
        "engine_version": "v2.6",
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S KST"),
        "as_of_date": today_str,
        "baselines": {
            "official_ftp_watts": ftp,
            "official_lthr_bpm": lthr,
            "max_hr_bpm": max_hr,
            "target_ftp_watts": TARGET_FTP,
            "current_weight_kg": CURRENT_WEIGHT_KG,
            "target_weight_kg": TARGET_WEIGHT_KG,
            "event_date": EVENT_DATE.isoformat(),
            "days_to_event": days_to_event,
        },
        "periodization_phase": phase,
        "layer1_daily": {
            "readiness_status": readiness_status,
            "effective_grade": effective_grade,
            "effective_score": effective_score,
            "is_hold_after_workout": is_hold_state,
            "sleep_debt_3d_hours": sleep_debt_3d,
            "hrv_today": hrv_today,
            "hrv_7d_avg": hrv_7d_avg,
            "hrv_delta_pct": hrv_delta_pct,
            "rhr_today": rhr_today,
            "rhr_7d_avg": rhr_7d_avg,
            "rhr_delta_bpm": rhr_delta_bpm,
            "load_focus_date": load_focus_fallback_date,
            "load_focus_3": {
                "low_aerobic": latest_low,
                "high_aerobic": latest_high,
                "anaerobic": latest_ana,
                "ratios_pct": lf_ratios,
            },
            "latest_1s_summary_present": bool(latest_1s_txt),
        },
        "layer2_weekly": {
            "tss_7d": tss_7d,
            "weekly_target_tss": weekly_target_tss,
            "tss_achievement_pct": weekly_tss_achievement_pct,
            "monotony_7d": monotony_7d,
            "strain_7d": strain_7d,
            "ctl_ramp_rate_7d": ctl_ramp_rate,
        },
        "layer3_monthly": {
            "ef_28d_avg": ef_28d_avg,
            "peak_np_28d": peak_np_28d,
            "candidate_eftp_watts": candidate_eftp,
            "candidate_eftp_rule": "후보 자동 덮어쓰기 금지 · 표준 테스트/검증 확정 시 일괄 갱신",
        },
        "layer4_event_prep": {
            "current_wkg": current_wkg,
            "target_wkg": target_wkg,
            "ftp_progress_pct": ftp_progress_pct,
            "course_summary": "고성공룡나라 그란폰도 B코스 (약 118.8km / 상승 약 1,252m / 핵심 업힐 2.49km · 평균 9.4%)",
        },
        "training_balance_latest": {
            "ctl": _to_float(today_row.get("ctl")),
            "atl": _to_float(today_row.get("atl")),
            "tsb": _to_float(today_row.get("tsb")),
            "acwr": _to_float(today_row.get("acwr")),
        },
    }


def _fmt_num(val: Any, suffix: str = "", digits: int = 1) -> str:
    f = _to_float(val)
    if f is None:
        return "확인 못 함"
    if digits == 0:
        return f"{int(round(f))}{suffix}"
    return f"{f:.{digits}f}{suffix}"


def build_summary_text(state: Dict[str, Any]) -> str:
    b = state["baselines"]
    tb = state["training_balance_latest"]
    l1 = state["layer1_daily"]
    l2 = state["layer2_weekly"]
    l3 = state["layer3_monthly"]
    l4 = state["layer4_event_prep"]
    phase = state["periodization_phase"]
    lf = l1["load_focus_3"]
    ratios = lf["ratios_pct"]

    lines = [
        f"[통합 코칭 분석 요약 | {state['generated_at_kst']} | 기준 FTP {int(b['official_ftp_watts'])}W · LTHR {int(b['official_lthr_bpm'])}bpm · HRmax {int(b['max_hr_bpm'])}bpm]",
        f"- 주기화 단계: {phase['phase_name']} (D-{b['days_to_event']}, 대회일 {b['event_date']})",
        f"- 트레이닝 밸런스(단일 기준): CTL {_fmt_num(tb['ctl'])} | ATL {_fmt_num(tb['atl'])} | TSB {_fmt_num(tb['tsb'])} | ACWR {_fmt_num(tb['acwr'], '', 2)}",
        f"- [1계층·일간] 준비도: {l1['readiness_status']} | 3일 수면부채: {_fmt_num(l1['sleep_debt_3d_hours'], 'h')} | HRV: {_fmt_num(l1['hrv_today'], 'ms', 0)} (7일대비 {_fmt_num(l1['hrv_delta_pct'], '%')}) | RHR: {_fmt_num(l1['rhr_today'], 'bpm', 0)} (7일대비 {_fmt_num(l1['rhr_delta_bpm'], 'bpm')})",
        f"- [부하 포커스 3종({l1['load_focus_date']})]: 저유산소 {_fmt_num(lf['low_aerobic'], '', 0)} ({_fmt_num(ratios['low_aerobic_pct'], '%')}) | 고유산소 {_fmt_num(lf['high_aerobic'], '', 0)} ({_fmt_num(ratios['high_aerobic_pct'], '%')}) | 무산소 {_fmt_num(lf['anaerobic'], '', 0)} ({_fmt_num(ratios['anaerobic_pct'], '%')})",
        f"- [2계층·주간] 7일 TSS: {_fmt_num(l2['tss_7d'])} / {_fmt_num(l2['weekly_target_tss'])} ({_fmt_num(l2['tss_achievement_pct'], '%')}) | Monotony: {_fmt_num(l2['monotony_7d'], '', 2)} | Strain: {_fmt_num(l2['strain_7d'])} | CTL 7일변화: {_fmt_num(l2['ctl_ramp_rate_7d'])}",
        f"- [3계층·월간] 28일 평균 EF: {_fmt_num(l3['ef_28d_avg'], '', 2)} | 28일 최고 NP: {_fmt_num(l3['peak_np_28d'], 'W')} | 후보 eFTP: {_fmt_num(l3['candidate_eftp_watts'], 'W')}",
        f"- [4계층·대회전] 현재 {_fmt_num(l4['current_wkg'], 'W/kg', 2)} (FTP 달성률 {_fmt_num(l4['ftp_progress_pct'], '%')}) -> 목표 {_fmt_num(l4['target_wkg'], 'W/kg', 2)} | {l4['course_summary']}",
    ]
    return "\n".join(lines)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_kst = datetime.now(KST)

    ftp = _get_env_float("FTP_WATTS", DEFAULT_FTP)
    lthr = _get_env_float("LTHR_BPM", DEFAULT_LTHR)
    max_hr = _get_env_float("MAX_HR", DEFAULT_MAX_HR)

    sessions_df, session_meta = build_unified_sessions(ftp=ftp)
    daily_df = build_daily_timeseries(sessions_df=sessions_df, as_of_date=now_kst.date())
    state = compute_coaching_state(daily_df=daily_df, now_kst=now_kst, ftp=ftp, lthr=lthr, max_hr=max_hr)
    state["ingestion_meta"] = session_meta
    summary_text = build_summary_text(state=state)

    out_csv = DATA_DIR / "unified_daily_metrics.csv"
    out_json = DATA_DIR / "unified_coaching_state.json"
    out_txt = DATA_DIR / "unified_coaching_summary.txt"

    daily_df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    out_json.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    out_txt.write_text(summary_text, encoding="utf-8")

    print(f"[OK] Saved {out_csv} ({len(daily_df)} rows)")
    print(f"[OK] Saved {out_json}")
    print(f"[OK] Saved {out_txt}")


if __name__ == "__main__":
    main()
