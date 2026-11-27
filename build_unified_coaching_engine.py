```python:통합 분석 엔진 파이썬 스크립트 (일·주·월·대회 전 4계층 판단):build_unified_coaching_engine.py
#!/usr/bin/env python3
"""
사이클링 통합 분석 엔진 (build_unified_coaching_engine.py)
- 2단계 통합 분석 지표 정의서(stage2_integrated_metrics_design.md) 기반 4단계 파이프라인 구현
- 입력(7종 데이터 자산):
    1) fit_analysis_report.csv   (1순위 세션 정밀 출력·존·EF·디커플링)
    2) garmin_activities.csv     (2순위 세션 백업·TE·VO2max·활동ID)
    3) strava_activities.csv     (3순위 백업 + RPE·칼로리·경사 스트림)
    4) strava_segments.csv       (야외/가상 세그먼트 PR·업힐 통과 기록)
    5) latest_ride_1s_summary.txt(직전 1회 블록별 수행률·eFTP/eLTHR)
    6) garmin_health.csv         (수면·HRV·RHR·BB·스트레스·부하포커스 3종)
    7) readiness_v2_summary.csv  (준비도 점수·신뢰도·A/B/C/D 게이트)
- 출력:
    1) unified_daily_metrics.csv   (일별 정규화 시계열 테이블: TSS_194, CTL, ATL, TSB, ACWR, 웰니스)
    2) unified_coaching_state.json (4계층 상위 판단 지표 16종 표준 JSON)
    3) unified_coaching_summary.txt(스프레드시트/리포트 연동용 단일 요약 텍스트)
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ==============================================================================
# 0. 전역 경로 및 공식 생리·대회 기준 상수
# ==============================================================================
KST = timezone(timedelta(hours=9))
DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
BASELINE_FILE = Path(os.getenv("PERFORMANCE_BASELINE_FILE", str(DATA_DIR / "performance_baseline.json")))

OUT_DAILY_CSV = DATA_DIR / "unified_daily_metrics.csv"
OUT_STATE_JSON = DATA_DIR / "unified_coaching_state.json"
OUT_SUMMARY_TXT = DATA_DIR / "unified_coaching_summary.txt"


def _to_float(val: Any, ndigits: Optional[int] = 2) -> Optional[float]:
    """결측치/문자열을 안전하게 float로 변환하고 반올림합니다."""
    if val is None or (isinstance(val, float) and not np.isfinite(val)):
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "null", "na", "n/a", "#error!"):
        return None
    try:
        num = float(s)
        if not np.isfinite(num):
            return None
        if ndigits == 0:
            return float(int(round(num)))
        return round(num, ndigits) if ndigits is not None else num
    except (ValueError, TypeError):
        return None


def _to_int(val: Any) -> Optional[int]:
    """결측치를 안전하게 int로 변환합니다."""
    f = _to_float(val, ndigits=0)
    return int(f) if f is not None else None


def _parse_datetime(val: Any) -> Optional[datetime]:
    """다양한 날짜/시간 문자열을 naive KST 기준 datetime으로 파싱합니다."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return None
    s_clean = re.sub(r"([+-]\d{2}:\d{2}|Z)$", "", s).replace("T", " ").strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s_clean[: len(fmt) + 4], fmt)
        except ValueError:
            continue
    try:
        ts = pd.to_datetime(s_clean, errors="coerce")
        if pd.notna(ts):
            return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    return None


def _load_baseline() -> Dict[str, Any]:
    """performance_baseline.json 및 환경변수에서 공식 확정 기준선을 로드합니다."""
    baseline: Dict[str, Any] = {
        "official_ftp_w": 194.0,
        "target_ftp_w": 240.0,
        "official_lthr_bpm": 147,
        "max_hr_bpm": 173,
        "current_weight_kg": 77.4,
        "target_weight_kg": 72.0,
        "vo2_max_baseline": 45,
        "mmp_5m_baseline_w": 255.0,
        "mmp_20m_baseline_w": 203.8,
        "z2_efficiency_power_w": 118.0,
        "ftp_test_date": "2026-12-06",
        "event_date": "2027-04-03",
        "event_name": "고성공룡나라그란폰도 B코스",
    }

    if BASELINE_FILE.exists():
        try:
            raw = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if _to_float(raw.get("official_ftp_watts")) is not None:
                    baseline["official_ftp_w"] = _to_float(raw["official_ftp_watts"], 1)
                if _to_float(raw.get("target_ftp_watts")) is not None:
                    baseline["target_ftp_w"] = _to_float(raw["target_ftp_watts"], 1)
                if _to_int(raw.get("official_lthr_bpm")) is not None:
                    baseline["official_lthr_bpm"] = _to_int(raw["official_lthr_bpm"])
                if _to_int(raw.get("max_hr_bpm")) is not None:
                    baseline["max_hr_bpm"] = _to_int(raw["max_hr_bpm"])
                if _to_float(raw.get("current_weight_kg")) is not None:
                    baseline["current_weight_kg"] = _to_float(raw["current_weight_kg"], 1)
                if _to_float(raw.get("target_weight_kg")) is not None:
                    baseline["target_weight_kg"] = _to_float(raw["target_weight_kg"], 1)
                if _to_int(raw.get("vo2_max_baseline")) is not None:
                    baseline["vo2_max_baseline"] = _to_int(raw["vo2_max_baseline"])
                if _to_float(raw.get("mmp_5m_baseline_watts")) is not None:
                    baseline["mmp_5m_baseline_w"] = _to_float(raw["mmp_5m_baseline_watts"], 1)
                if _to_float(raw.get("z2_efficiency_power_watts")) is not None:
                    baseline["z2_efficiency_power_w"] = _to_float(raw["z2_efficiency_power_watts"], 1)
                if raw.get("ftp_test_date"):
                    baseline["ftp_test_date"] = str(raw["ftp_test_date"]).strip()
                if raw.get("event_date"):
                    baseline["event_date"] = str(raw["event_date"]).strip()
                if raw.get("event_name"):
                    baseline["event_name"] = str(raw["event_name"]).strip()
        except Exception as e:
            print(f"[WARN] 기준선 JSON 로드 실패 ({BASELINE_FILE}): {e}", flush=True)

    if os.getenv("FTP_WATTS", "").strip():
        baseline["official_ftp_w"] = float(os.getenv("FTP_WATTS").strip())
    if os.getenv("LTHR_BPM", "").strip():
        baseline["official_lthr_bpm"] = int(round(float(os.getenv("LTHR_BPM").strip())))
    if os.getenv("MAX_HR", "").strip():
        baseline["max_hr_bpm"] = int(round(float(os.getenv("MAX_HR").strip())))

    w_kg = float(baseline["current_weight_kg"] or 77.4)
    ftp_w = float(baseline["official_ftp_w"] or 194.0)
    baseline["current_w_per_kg"] = round(ftp_w / w_kg, 2) if w_kg > 0 else 0.0
    return baseline


def _read_csv_safe(filename: str) -> pd.DataFrame:
    """로컬 CSV 파일을 안전하게 읽고 결측 시 빈 DataFrame을 반환합니다."""
    path = DATA_DIR / filename
    if not path.exists():
        print(f"[WARN] 입력 파일 미존재 (건너뜀): {path}", flush=True)
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="utf-8")
    except Exception as e:
        print(f"[WARN] CSV 읽기 실패 ({path}): {e}", flush=True)
        return pd.DataFrame()


def _read_latest_1s_summary() -> Dict[str, Any]:
    """latest_ride_1s_summary.txt에서 직전 1회 블록 수행률 및 후보 eFTP/eLTHR 정보를 파싱합니다."""
    out: Dict[str, Any] = {
        "raw_text": "",
        "detected_blocks": [],
        "candidate_eftp_w": None,
        "candidate_elthr_bpm": None,
        "adherence_note": None,
    }
    txt_path = DATA_DIR / "latest_ride_1s_summary.txt"
    if not txt_path.exists():
        return out

    try:
        text = txt_path.read_text(encoding="utf-8", errors="ignore").strip()
        out["raw_text"] = text

        eftp_match = re.search(r"후보\s*eFTP\s*([0-9]+(?:\.[0-9]+)?)\s*W", text)
        if eftp_match:
            out["candidate_eftp_w"] = _to_float(eftp_match.group(1), 1)

        elthr_match = re.search(r"후보\s*eLTHR\s*([0-9]+(?:\.[0-9]+)?)\s*bpm", text)
        if elthr_match:
            out["candidate_elthr_bpm"] = _to_int(elthr_match.group(1))

        block_matches = re.findall(
            r"(WARMUP|WORK|RECOVERY|COOLDOWN)\s+([^\d]+?)\s+(\d{2}:\d{2})\s+(\d{2}:\d{2})\s+([0-9]+(?:\.[0-9]+)?)W",
            text,
        )
        for b_type, b_lbl, s_t, e_t, avg_w in block_matches:
            out["detected_blocks"].append(
                {
                    "type": b_type,
                    "label": b_lbl.strip(),
                    "start": s_t,
                    "end": e_t,
                    "avg_power_w": _to_float(avg_w, 1),
                }
            )
    except Exception as e:
        print(f"[WARN] latest_ride_1s_summary.txt 파싱 실패: {e}", flush=True)

    return out


# ==============================================================================
# Step A: 7종 활동 소스 통합, 중복 제거 및 공식 FTP(194W) 표준 TSS 정규화
# ==============================================================================
def compute_standard_tss(duration_min: Optional[float], np_watts: Optional[float], official_ftp: float) -> Optional[float]:
    """
    공식 FTP 기준 표준 TSS 산출 공식:
    TSS_194 = (duration_sec * NP * (NP / FTP)) / (FTP * 3600) * 100
    """
    if duration_min is None or np_watts is None or official_ftp <= 0:
        return None
    if duration_min <= 0 or np_watts <= 0:
        return 0.0
    duration_sec = duration_min * 60.0
    intensity_factor = np_watts / official_ftp
    tss = (duration_sec * np_watts * intensity_factor) / (official_ftp * 3600.0) * 100.0
    return round(tss, 2)


def build_unified_sessions(
    fit_df: pd.DataFrame,
    garmin_act_df: pd.DataFrame,
    strava_act_df: pd.DataFrame,
    strava_seg_df: pd.DataFrame,
    official_ftp: float,
) -> pd.DataFrame:
    """
    Garmin 활동을 1순위 마스터로 삼고, FIT 1초 정밀 분석(1순위 출력)과
    Strava 확장 데이터(RPE, 칼로리, 경사 스트림, 세그먼트 PR/업힐)를 중복 없이 병합합니다.
    """
    fit_by_id: Dict[str, Dict[str, Any]] = {}
    fit_by_date: Dict[str, List[Dict[str, Any]]] = {}
    if not fit_df.empty:
        for _, r in fit_df.iterrows():
            row_dict = r.to_dict()
            fname = str(row_dict.get("file", ""))
            id_m = re.search(r"_(\d+)\.fit$", fname)
            date_m = re.search(r"^(\d{4}-\d{2}-\d{2})", fname)
            if id_m:
                fit_by_id[id_m.group(1)] = row_dict
            if date_m:
                fit_by_date.setdefault(date_m.group(1), []).append(row_dict)

    seg_by_strava_id: Dict[str, Dict[str, Any]] = {}
    if not strava_seg_df.empty and "activity_id" in strava_seg_df.columns:
        for act_id_val, grp in strava_seg_df.groupby("activity_id"):
            aid_str = str(act_id_val).split(".")[0]
            pr_rows = grp[grp["is_pr"].astype(str).str.lower().isin(["true", "1"])] if "is_pr" in grp.columns else pd.DataFrame()
            seg_by_strava_id[aid_str] = {
                "seg_total_efforts": int(len(grp)),
                "seg_pr_count_calc": int(len(pr_rows)),
            }

    strava_records: List[Dict[str, Any]] = []
    if not strava_act_df.empty:
        for _, r in strava_act_df.iterrows():
            d = r.to_dict()
            dt = _parse_datetime(d.get("date"))
            if dt is None:
                continue
            d["_dt"] = dt
            d["_date_str"] = dt.strftime("%Y-%m-%d")
            d["_dur_min"] = _to_float(d.get("moving_time_min"), 1) or 0.0
            d["_used"] = False
            strava_records.append(d)

    unified_rows: List[Dict[str, Any]] = []

    if not garmin_act_df.empty:
        for _, r in garmin_act_df.iterrows():
            g = r.to_dict()
            act_id = str(g.get("activity_id", "")).split(".")[0]
            dt = _parse_datetime(g.get("date"))
            if dt is None:
                continue
            date_str = dt.strftime("%Y-%m-%d")
            dur_garmin = _to_float(g.get("duration_min"), 1) or 0.0

            fit_row = fit_by_id.get(act_id)
            if fit_row is None and date_str in fit_by_date:
                for cand in fit_by_date[date_str]:
                    f_dur = _to_float(cand.get("duration_min"), 1) or 0.0
                    if abs(f_dur - dur_garmin) <= 3.0:
                        fit_row = cand
                        break

            matched_strava: Optional[Dict[str, Any]] = None
            best_diff = 999999.0
            for s in strava_records:
                if s["_used"]:
                    continue
                time_diff_sec = abs((s["_dt"] - dt).total_seconds())
                dur_diff_min = abs(s["_dur_min"] - dur_garmin)
                if time_diff_sec <= 900 and dur_diff_min <= 5.0:
                    if time_diff_sec < best_diff:
                        best_diff = time_diff_sec
                        matched_strava = s
                elif s["_date_str"] == date_str and dur_diff_min <= 2.5 and best_diff > 900:
                    best_diff = 1000.0 + dur_diff_min
                    matched_strava = s

            if matched_strava is not None:
                matched_strava["_used"] = True

            dur_final = (
                _to_float(fit_row.get("duration_min"), 1) if fit_row else None
            ) or dur_garmin or (matched_strava["_dur_min"] if matched_strava else 0.0)

            avg_power = (
                _to_float(fit_row.get("avg_power"), 1) if fit_row else None
            ) or _to_float(g.get("avg_power"), 1) or (
                _to_float(matched_strava.get("avg_watts"), 1) if matched_strava else None
            )

            np_watts = (
                _to_float(fit_row.get("NP_watts"), 1) if fit_row else None
            ) or _to_float(g.get("norm_power_NP"), 1) or (
                _to_float(matched_strava.get("weighted_avg_watts_NP"), 1) if matched_strava else None
            )

            tss_194 = compute_standard_tss(dur_final, np_watts, official_ftp)
            if tss_194 is None:
                tss_194 = _to_float(g.get("training_stress_score_TSS"), 2) or 0.0

            if_194 = round(np_watts / official_ftp, 3) if (np_watts and official_ftp > 0) else _to_float(g.get("intensity_factor_IF"), 3)

            avg_hr = (
                _to_float(fit_row.get("avg_hr"), 1) if fit_row else None
            ) or _to_float(g.get("avg_hr"), 1) or (
                _to_float(matched_strava.get("avg_hr"), 1) if matched_strava else None
            )
            max_hr = (
                _to_int(fit_row.get("max_hr")) if fit_row else None
            ) or _to_int(g.get("max_hr")) or (
                _to_int(matched_strava.get("max_hr")) if matched_strava else None
            )

            ef = _to_float(fit_row.get("efficiency_factor_EF"), 2) if fit_row else None
            if ef is None and np_watts and avg_hr and avg_hr > 0:
                ef = round(np_watts / avg_hr, 2)

            decoupling_pct = _to_float(fit_row.get("aerobic_decoupling_pct"), 2) if fit_row else None

            z1_min = _to_float(fit_row.get("Z1_recovery_min"), 1) if fit_row else (
                _to_float(matched_strava.get("strava_pz1_min"), 1) if matched_strava else None
            )
            z2_min = _to_float(fit_row.get("Z2_endurance_min"), 1) if fit_row else (
                _to_float(matched_strava.get("strava_pz2_min"), 1) if matched_strava else None
            )
            z3_min = _to_float(fit_row.get("Z3_tempo_min"), 1) if fit_row else (
                _to_float(matched_strava.get("strava_pz3_min"), 1) if matched_strava else None
            )
            z4_min = _to_float(fit_row.get("Z4_threshold_min"), 1) if fit_row else (
                _to_float(matched_strava.get("strava_pz4_min"), 1) if matched_strava else None
            )
            z5_min = _to_float(fit_row.get("Z5_vo2max_min"), 1) if fit_row else (
                _to_float(matched_strava.get("strava_pz5_plus_min"), 1) if matched_strava else None
            )
            z6_min = _to_float(fit_row.get("Z6_anaerobic_min"), 1) if fit_row else 0.0
            z7_min = _to_float(fit_row.get("Z7_neuromuscular_min"), 1) if fit_row else 0.0

            strava_id = str(matched_strava.get("id", "")).split(".")[0] if matched_strava else None
            seg_extra = seg_by_strava_id.get(strava_id or "", {})

            is_trainer = True
            if matched_strava and matched_strava.get("trainer") is not None:
                is_trainer = str(matched_strava.get("trainer")).strip().lower() in ("true", "1")
            elif "indoor" not in str(g.get("type", "")).lower() and "virtual" not in str(g.get("type", "")).lower():
                is_trainer = False

            unified_rows.append(
                {
                    "source_priority": "FIT+GARMIN" if fit_row else "GARMIN",
                    "garmin_id": act_id,
                    "strava_id": strava_id,
                    "start_dt": dt,
                    "end_dt": dt + timedelta(minutes=dur_final),
                    "date": date_str,
                    "name": g.get("name") or (matched_strava.get("name") if matched_strava else ""),
                    "is_trainer": is_trainer,
                    "distance_km": _to_float(g.get("distance_km"), 2) or (
                        _to_float(matched_strava.get("distance_km"), 2) if matched_strava else 0.0
                    ),
                    "duration_min": dur_final,
                    "elevation_gain_m": _to_float(g.get("elevation_gain_m"), 1) or (
                        _to_float(matched_strava.get("elevation_gain_m"), 1) if matched_strava else 0.0
                    ),
                    "avg_power": avg_power,
                    "np_watts": np_watts,
                    "max_power": _to_int(fit_row.get("max_power")) if fit_row else _to_int(g.get("max_power")),
                    "mmp_1m": _to_float(fit_row.get("MMP_1m"), 1) if fit_row else None,
                    "mmp_5m": _to_float(fit_row.get("MMP_5m"), 1) if fit_row else None,
                    "mmp_20m": _to_float(fit_row.get("MMP_20m"), 1) if fit_row else _to_float(g.get("max_20min_power"), 1),
                    "if_194": if_194,
                    "tss_194": tss_194,
                    "avg_hr": avg_hr,
                    "max_hr": max_hr,
                    "avg_cadence": _to_float(fit_row.get("avg_cadence"), 1) if fit_row else _to_float(g.get("avg_cadence"), 1),
                    "ef": ef,
                    "decoupling_pct": decoupling_pct,
                    "z1_min": z1_min,
                    "z2_min": z2_min,
                    "z3_min": z3_min,
                    "z4_min": z4_min,
                    "z5_plus_min": round((z5_min or 0.0) + (z6_min or 0.0) + (z7_min or 0.0), 1) if z5_min is not None else None,
                    "aerobic_te": _to_float(g.get("aerobic_training_effect"), 1),
                    "anaerobic_te": _to_float(g.get("anaerobic_training_effect"), 1),
                    "vo2_max": _to_int(g.get("vo2_max")),
                    "kilojoules": _to_float(matched_strava.get("kilojoules"), 1) if matched_strava else (
                        round((avg_power * dur_final * 60.0) / 1000.0, 1) if avg_power else None
                    ),
                    "calories_kcal": _to_int(matched_strava.get("calories_kcal")) if matched_strava else None,
                    "suffer_score_re": _to_int(matched_strava.get("suffer_score")) if matched_strava else None,
                    "rpe": _to_float(matched_strava.get("perceived_exertion_rpe"), 1) if matched_strava else None,
                    "max_grade_pct": _to_float(matched_strava.get("max_grade_pct"), 1) if matched_strava else None,
                    "flat_avg_watts": _to_float(matched_strava.get("flat_avg_watts"), 1) if matched_strava else None,
                    "false_flat_avg_watts": _to_float(matched_strava.get("false_flat_avg_watts"), 1) if matched_strava else None,
                    "climb_avg_watts": _to_float(matched_strava.get("climb_avg_watts"), 1) if matched_strava else None,
                    "climb_time_min": _to_float(matched_strava.get("climb_time_min"), 1) if matched_strava else None,
                    "key_climb_segment": matched_strava.get("key_climb_segment") if matched_strava else None,
                    "segment_pr_names": matched_strava.get("segment_pr_names") if matched_strava else None,
                    "segment_efforts": seg_extra.get("seg_total_efforts", _to_int(matched_strava.get("segment_count")) if matched_strava else 0),
                }
            )

    for s in strava_records:
        if s["_used"]:
            continue
        dt = s["_dt"]
        dur_final = s["_dur_min"]
        avg_power = _to_float(s.get("avg_watts"), 1)
        np_watts = _to_float(s.get("weighted_avg_watts_NP"), 1) or avg_power
        tss_194 = compute_standard_tss(dur_final, np_watts, official_ftp) or 0.0
        if_194 = round(np_watts / official_ftp, 3) if (np_watts and official_ftp > 0) else None
        avg_hr = _to_float(s.get("avg_hr"), 1)
        ef = round(np_watts / avg_hr, 2) if (np_watts and avg_hr and avg_hr > 0) else None
        strava_id = str(s.get("id", "")).split(".")[0]

        unified_rows.append(
            {
                "source_priority": "STRAVA_BACKUP",
                "garmin_id": None,
                "strava_id": strava_id,
                "start_dt": dt,
                "end_dt": dt + timedelta(minutes=dur_final),
                "date": dt.strftime("%Y-%m-%d"),
                "name": s.get("name", ""),
                "is_trainer": str(s.get("trainer")).strip().lower() in ("true", "1"),
                "distance_km": _to_float(s.get("distance_km"), 2) or 0.0,
                "duration_min": dur_final,
                "elevation_gain_m": _to_float(s.get("elevation_gain_m"), 1) or 0.0,
                "avg_power": avg_power,
                "np_watts": np_watts,
                "max_power": _to_int(s.get("max_watts")),
                "mmp_1m": None,
                "mmp_5m": None,
                "mmp_20m": None,
                "if_194": if_194,
                "tss_194": tss_194,
                "avg_hr": avg_hr,
                "max_hr": _to_int(s.get("max_hr")),
                "avg_cadence": _to_float(s.get("avg_cadence"), 1),
                "ef": ef,
                "decoupling_pct": None,
                "z1_min": _to_float(s.get("strava_pz1_min"), 1),
                "z2_min": _to_float(s.get("strava_pz2_min"), 1),
                "z3_min": _to_float(s.get("strava_pz3_min"), 1),
                "z4_min": _to_float(s.get("strava_pz4_min"), 1),
                "z5_plus_min": _to_float(s.get("strava_pz5_plus_min"), 1),
                "aerobic_te": None,
                "anaerobic_te": None,
                "vo2_max": None,
                "kilojoules": _to_float(s.get("kilojoules"), 1),
                "calories_kcal": _to_int(s.get("calories_kcal")),
                "suffer_score_re": _to_int(s.get("suffer_score")),
                "rpe": _to_float(s.get("perceived_exertion_rpe"), 1),
                "max_grade_pct": _to_float(s.get("max_grade_pct"), 1),
                "flat_avg_watts": _to_float(s.get("flat_avg_watts"), 1),
                "false_flat_avg_watts": _to_float(s.get("false_flat_avg_watts"), 1),
                "climb_avg_watts": _to_float(s.get("climb_avg_watts"), 1),
                "climb_time_min": _to_float(s.get("climb_time_min"), 1),
                "key_climb_segment": s.get("key_climb_segment"),
                "segment_pr_names": s.get("segment_pr_names"),
                "segment_efforts": _to_int(s.get("segment_count")) or 0,
            }
        )

    if not unified_rows:
        return pd.DataFrame()

    df_out = pd.DataFrame(unified_rows).sort_values("start_dt", ascending=True).reset_index(drop=True)
    return df_out


# ==============================================================================
# Step B: 일별 통합 시계열 병합 및 단일 EWMA(CTL · ATL · TSB · ACWR) 산출
# ==============================================================================
def build_daily_timeseries(
    sessions_df: pd.DataFrame,
    health_df: pd.DataFrame,
    now_kst: datetime,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    첫 활동일부터 현재 시각까지 연속 날짜 시계열을 생성하고
    CTL(42일), ATL(7일), TSB, ACWR 및 실시간 회복 감쇠 수치를 단일 공식으로 계산합니다.
    """
    today_date = now_kst.date()
    start_dates: List[datetime.date] = [today_date]

    if not sessions_df.empty:
        first_act_dt = sessions_df["start_dt"].min()
        if pd.notna(first_act_dt):
            start_dates.append(first_act_dt.date())
    if not health_df.empty and "date" in health_df.columns:
        for d_val in health_df["date"].dropna():
            dt_h = _parse_datetime(d_val)
            if dt_h:
                start_dates.append(dt_h.date())

    min_date = min(start_dates)
    all_dates = pd.date_range(start=min_date, end=today_date, freq="D")

    daily_act_map: Dict[str, Dict[str, Any]] = {}
    if not sessions_df.empty:
        for date_str, grp in sessions_df.groupby("date"):
            daily_act_map[str(date_str)] = {
                "session_count": int(len(grp)),
                "duration_min": round(float(grp["duration_min"].fillna(0).sum()), 1),
                "tss_194": round(float(grp["tss_194"].fillna(0).sum()), 2),
                "kilojoules": round(float(grp["kilojoules"].fillna(0).sum()), 1),
                "z1_min": round(float(grp["z1_min"].fillna(0).sum()), 1),
                "z2_min": round(float(grp["z2_min"].fillna(0).sum()), 1),
                "z3_min": round(float(grp["z3_min"].fillna(0).sum()), 1),
                "z4_min": round(float(grp["z4_min"].fillna(0).sum()), 1),
                "z5_plus_min": round(float(grp["z5_plus_min"].fillna(0).sum()), 1),
            }

    health_map: Dict[str, Dict[str, Any]] = {}
    if not health_df.empty and "date" in health_df.columns:
        for _, r in health_df.iterrows():
            dt_h = _parse_datetime(r.get("date"))
            if dt_h:
                health_map[dt_h.strftime("%Y-%m-%d")] = r.to_dict()

    k_ctl = 1.0 - math.exp(-1.0 / 42.0)
    k_atl = 1.0 - math.exp(-1.0 / 7.0)
    ctl = 0.0
    atl = 0.0

    rows: List[Dict[str, Any]] = []
    for ts_day in all_dates:
        d_str = ts_day.strftime("%Y-%m-%d")
        act = daily_act_map.get(d_str, {})
        h = health_map.get(d_str, {})

        tss_day = float(act.get("tss_194", 0.0))
        ctl = ctl + (tss_day - ctl) * k_ctl
        atl = atl + (tss_day - atl) * k_atl
        tsb = ctl - atl
        acwr = round(atl / ctl, 2) if ctl > 0.1 else 0.0

        rows.append(
            {
                "date": d_str,
                "session_count": act.get("session_count", 0),
                "duration_min": act.get("duration_min", 0.0),
                "tss_194": round(tss_day, 2),
                "ctl_42d": round(ctl, 2),
                "atl_7d": round(atl, 2),
                "tsb": round(tsb, 2),
                "acwr": acwr,
                "z1_min": act.get("z1_min", 0.0),
                "z2_min": act.get("z2_min", 0.0),
                "z3_min": act.get("z3_min", 0.0),
                "z4_min": act.get("z4_min", 0.0),
                "z5_plus_min": act.get("z5_plus_min", 0.0),
                "kilojoules": act.get("kilojoules", 0.0),
                "resting_hr": _to_int(h.get("resting_hr")),
                "hrv_last_night_avg": _to_int(h.get("hrv_last_night_avg")),
                "hrv_weekly_avg": _to_int(h.get("hrv_weekly_avg")),
                "hrv_status": h.get("hrv_status") if pd.notna(h.get("hrv_status")) else None,
                "hrv_baseline_low": _to_int(h.get("hrv_baseline_low")),
                "hrv_baseline_high": _to_int(h.get("hrv_baseline_high")),
                "sleep_hours": _to_float(h.get("sleep_hours"), 2),
                "sleep_score": _to_int(h.get("sleep_score")),
                "deep_sleep_min": _to_int(h.get("deep_sleep_min")),
                "rem_sleep_min": _to_int(h.get("rem_sleep_min")),
                "body_battery_current": _to_int(h.get("body_battery_current")),
                "body_battery_high": _to_int(h.get("body_battery_high")),
                "body_battery_low": _to_int(h.get("body_battery_low")),
                "avg_stress": _to_int(h.get("avg_stress")),
                "max_stress": _to_int(h.get("max_stress")),
                "rest_stress_sec": _to_int(h.get("rest_stress_sec")),
                "avg_respiration": _to_float(h.get("avg_respiration"), 1),
                "avg_spo2": _to_int(h.get("avg_spo2")),
                "steps": _to_int(h.get("steps")),
                "active_kcal": _to_int(h.get("active_kcal")),
                "total_kcal": _to_int(h.get("total_kcal")),
                "load_low_aerobic": _to_float(h.get("training_load_low_aerobic"), 1),
                "load_high_aerobic": _to_float(h.get("training_load_high_aerobic"), 1),
                "load_anaerobic": _to_float(h.get("training_load_anaerobic"), 1),
            }
        )

    daily_df = pd.DataFrame(rows)

    now_naive = now_kst.replace(tzinfo=None)
    latest_row = daily_df.iloc[-1].to_dict()
    live_ctl = float(latest_row["ctl_42d"])
    live_atl = float(latest_row["atl_7d"])
    elapsed_hours: Optional[float] = None
    last_session_info: Optional[Dict[str, Any]] = None

    if not sessions_df.empty:
        last_s = sessions_df.iloc[-1]
        end_dt = last_s["end_dt"]
        if pd.notna(end_dt):
            diff_h = max(0.0, (now_naive - end_dt).total_seconds() / 3600.0)
            elapsed_hours = round(diff_h, 1)
            if float(latest_row["tss_194"]) == 0.0 and diff_h > 0:
                frac_days = min(diff_h / 24.0, 1.0)
                prev_row = daily_df.iloc[-2].to_dict() if len(daily_df) >= 2 else latest_row
                live_ctl = float(prev_row["ctl_42d"]) * math.exp(-frac_days / 42.0)
                live_atl = float(prev_row["atl_7d"]) * math.exp(-frac_days / 7.0)

        last_session_info = {
            "date": str(last_s["date"]),
            "name": str(last_s["name"]),
            "garmin_id": last_s["garmin_id"],
            "strava_id": last_s["strava_id"],
            "duration_min": _to_float(last_s["duration_min"], 1),
            "avg_power": _to_float(last_s["avg_power"], 1),
            "np_watts": _to_float(last_s["np_watts"], 1),
            "if_194": _to_float(last_s["if_194"], 3),
            "tss_194": _to_float(last_s["tss_194"], 1),
            "avg_hr": _to_float(last_s["avg_hr"], 1),
            "max_hr": _to_int(last_s["max_hr"]),
            "ef": _to_float(last_s["ef"], 2),
            "decoupling_pct": _to_float(last_s["decoupling_pct"], 2),
            "rpe": _to_float(last_s["rpe"], 1),
            "calories_kcal": _to_int(last_s["calories_kcal"]),
            "kilojoules": _to_float(last_s["kilojoules"], 1),
            "is_trainer": bool(last_s["is_trainer"]),
            "climb_avg_watts": _to_float(last_s["climb_avg_watts"], 1),
            "key_climb_segment": last_s["key_climb_segment"] if pd.notna(last_s["key_climb_segment"]) else None,
        }

    live_ctl_r = round(live_ctl, 1)
    live_atl_r = round(live_atl, 1)
    live_tsb_r = round(live_ctl - live_atl, 1)
    live_acwr_r = round(live_atl / live_ctl, 2) if live_ctl > 0.1 else 0.0

    daily_df.loc[daily_df.index[-1], "ctl_42d"] = live_ctl_r
    daily_df.loc[daily_df.index[-1], "atl_7d"] = live_atl_r
    daily_df.loc[daily_df.index[-1], "tsb"] = live_tsb_r
    daily_df.loc[daily_df.index[-1], "acwr"] = live_acwr_r

    live_pmc = {
        "ctl_42d": live_ctl_r,
        "atl_7d": live_atl_r,
        "tsb": live_tsb_r,
        "acwr": live_acwr_r,
        "elapsed_hours_since_last_ride": elapsed_hours,
        "last_session": last_session_info,
    }
    return daily_df, live_pmc


# ==============================================================================
# Step C: 4계층 상위 판단 지표 16종 산출 (일 · 주 · 월 · 대회 전)
# ==============================================================================
def compute_layer1_daily(
    daily_df: pd.DataFrame,
    readiness_df: pd.DataFrame,
    live_pmc: Dict[str, Any],
    now_kst: datetime,
) -> Dict[str, Any]:
    """계층 1 [일간 · Daily]: 준비도 게이트, 3일 누적 수면 부채, 자율신경 편차, 부하 포커스 3종"""
    today_str = now_kst.strftime("%Y-%m-%d")
    today_row = daily_df.iloc[-1].to_dict()

    recent_3d = daily_df.tail(3)
    valid_sleeps = [float(x) for x in recent_3d["sleep_hours"].dropna().tolist()]
    sleep_debt_3d = round(sum(s - 7.0 for s in valid_sleeps), 2) if valid_sleeps else None

    recent_7d = daily_df.tail(7)
    hrv_curr = _to_int(today_row.get("hrv_last_night_avg"))
    hrv_7d_vals = [float(x) for x in recent_7d["hrv_last_night_avg"].dropna().tolist()]
    hrv_7d_avg = round(float(np.mean(hrv_7d_vals)), 1) if hrv_7d_vals else None
    hrv_delta_pct = (
        round(((hrv_curr - hrv_7d_avg) / hrv_7d_avg) * 100.0, 1)
        if (hrv_curr is not None and hrv_7d_avg and hrv_7d_avg > 0)
        else None
    )

    rhr_curr = _to_int(today_row.get("resting_hr"))
    rhr_7d_vals = [float(x) for x in recent_7d["resting_hr"].dropna().tolist()]
    rhr_7d_avg = round(float(np.mean(rhr_7d_vals)), 1) if rhr_7d_vals else None
    rhr_delta_bpm = round(rhr_curr - rhr_7d_avg, 1) if (rhr_curr is not None and rhr_7d_avg is not None) else None

    r_score = None
    r_grade = None
    r_conf = None
    r_comp = None
    r_reasons = None
    if not readiness_df.empty:
        r_latest = readiness_df.iloc[0].to_dict()
        r_score = _to_float(r_latest.get("score"), 1)
        raw_grade = str(r_latest.get("permission_grade") or "").strip()
        r_grade = raw_grade[0] if raw_grade and raw_grade[0] in ("A", "B", "C", "D") else None
        r_conf = r_latest.get("confidence") if pd.notna(r_latest.get("confidence")) else None
        r_comp = _to_int(r_latest.get("completeness_pct"))
        r_reasons = r_latest.get("gate_reasons") if pd.notna(r_latest.get("gate_reasons")) else None

    trained_today = int(today_row.get("session_count") or 0) > 0
    gate_overrides: List[str] = []

    resp_curr = _to_float(today_row.get("avg_respiration"), 1)
    spo2_curr = _to_int(today_row.get("avg_spo2"))
    bb_curr = _to_int(today_row.get("body_battery_current"))
    hrv_status = str(today_row.get("hrv_status") or "").upper()

    if (spo2_curr is not None and spo2_curr < 90) or (resp_curr is not None and resp_curr >= 20.0):
        r_grade = "D"
        gate_overrides.append("호흡/SpO2 안전 게이트 경고(D 등급)")
    elif rhr_delta_bpm is not None and rhr_delta_bpm >= 7.0:
        if r_grade in (None, "A", "B"):
            r_grade = "C"
        gate_overrides.append(f"RHR +{rhr_delta_bpm}bpm 상승(C 등급 이상 제한)")
    elif (sleep_debt_3d is not None and sleep_debt_3d < -3.0) or (bb_curr is not None and bb_curr <= 30) or ("UNBALANCED" in hrv_status):
        if r_grade in (None, "A"):
            r_grade = "B"
        gate_overrides.append("수면 부채/BB/HRV 주의(B 등급 강도 조정)")

    if trained_today:
        effective_status = "⏸ 보류 (당일 훈련 완료 · 익일 기상 후 동기화 대기)"
    else:
        effective_status = f"{r_grade or '확인 못 함'}등급 활성"

    lf_valid = daily_df.dropna(subset=["load_low_aerobic", "load_high_aerobic", "load_anaerobic"])
    load_focus_latest: Optional[Dict[str, Any]] = None
    if not lf_valid.empty:
        lf_row = lf_valid.iloc[-1].to_dict()
        low_v = float(lf_row["load_low_aerobic"])
        high_v = float(lf_row["load_high_aerobic"])
        ana_v = float(lf_row["load_anaerobic"])
        tot_v = low_v + high_v + ana_v
        ratios = [round((v / tot_v) * 100.0, 1) for v in (low_v, high_v, ana_v)] if tot_v > 0 else [0.0, 0.0, 0.0]
        load_focus_latest = {
            "ref_date": str(lf_row["date"]),
            "low_aerobic": round(low_v, 1),
            "high_aerobic": round(high_v, 1),
            "anaerobic": round(ana_v, 1),
            "ratios_pct": ratios,
            "high_aerobic_needs_boost": high_v < 350.0,
        }

    return {
        "as_of_date": today_str,
        "ctl_42d": live_pmc["ctl_42d"],
        "atl_7d": live_pmc["atl_7d"],
        "tsb": live_pmc["tsb"],
        "acwr": live_pmc["acwr"],
        "elapsed_hours_since_last_ride": live_pmc["elapsed_hours_since_last_ride"],
        "trained_today": trained_today,
        "readiness_status": effective_status,
        "readiness_score": r_score,
        "permission_grade": r_grade,
        "readiness_confidence": r_conf,
        "readiness_completeness_pct": r_comp,
        "gate_reasons": r_reasons,
        "gate_overrides": gate_overrides,
        "sleep_hours_last_night": _to_float(today_row.get("sleep_hours"), 2),
        "sleep_score_last_night": _to_int(today_row.get("sleep_score")),
        "sleep_debt_3d_hours": sleep_debt_3d,
        "hrv_last_night_ms": hrv_curr,
        "hrv_status": today_row.get("hrv_status"),
        "hrv_7d_delta_pct": hrv_delta_pct,
        "rhr_bpm": rhr_curr,
        "rhr_7d_delta_bpm": rhr_delta_bpm,
        "body_battery_current": bb_curr,
        "avg_stress": _to_int(today_row.get("avg_stress")),
        "load_focus_latest": load_focus_latest,
    }


def compute_layer2_weekly(
    daily_df: pd.DataFrame,
    now_kst: datetime,
    weekly_planned_tss: float = 263.6,
) -> Dict[str, Any]:
    """계층 2 [주간 · Weekly]: 주간 TSS 달성률, 폴라라이즈드 저강도 비중, Monotony/Strain, CTL 램프율"""
    today_date = now_kst.date()
    monday_date = today_date - timedelta(days=today_date.weekday())
    sunday_date = monday_date + timedelta(days=6)

    week_mask = (daily_df["date"] >= monday_date.strftime("%Y-%m-%d")) & (
        daily_df["date"] <= sunday_date.strftime("%Y-%m-%d")
    )
    week_df = daily_df[week_mask].copy()

    completed_tss = round(float(week_df["tss_194"].fillna(0).sum()), 1)
    completion_pct = round((completed_tss / weekly_planned_tss) * 100.0, 1) if weekly_planned_tss > 0 else 0.0

    tot_dur = float(week_df["duration_min"].fillna(0).sum())
    low_dur = float(week_df["z1_min"].fillna(0).sum() + week_df["z2_min"].fillna(0).sum())
    polarized_low_pct = round((low_dur / tot_dur) * 100.0, 1) if tot_dur > 0 else None
    junk_miles_warning = bool(polarized_low_pct is not None and polarized_low_pct < 65.0 and tot_dur >= 90.0)

    rolling_7d = daily_df.tail(7)["tss_194"].fillna(0).to_numpy(dtype=float)
    tss_7d_sum = round(float(np.sum(rolling_7d)), 1)
    mean_7d = float(np.mean(rolling_7d)) if len(rolling_7d) > 0 else 0.0
    std_7d = float(np.std(rolling_7d, ddof=0)) if len(rolling_7d) > 0 else 0.0
    monotony = round(mean_7d / std_7d, 2) if std_7d > 0.5 else None
    strain = round(tss_7d_sum * monotony, 1) if monotony is not None else None
    monotony_warning = bool(monotony is not None and monotony > 2.0)

    ctl_now = float(daily_df.iloc[-1]["ctl_42d"])
    ctl_7d_ago = float(daily_df.iloc[-8]["ctl_42d"]) if len(daily_df) >= 8 else float(daily_df.iloc[0]["ctl_42d"])
    ctl_ramp_7d = round(ctl_now - ctl_7d_ago, 1)

    return {
        "week_label": f"{monday_date.strftime('%Y-%m-%d')} ~ {sunday_date.strftime('%Y-%m-%d')}",
        "completed_sessions": int(week_df["session_count"].fillna(0).sum()),
        "completed_duration_min": round(tot_dur, 1),
        "low_intensity_min": round(low_dur, 1),
        "completed_tss_194w": completed_tss,
        "planned_tss_194w": weekly_planned_tss,
        "completion_pct": completion_pct,
        "polarized_low_pct": polarized_low_pct,
        "junk_miles_warning": junk_miles_warning,
        "rolling_7d_tss": tss_7d_sum,
        "monotony": monotony,
        "strain": strain,
        "monotony_warning": monotony_warning,
        "ctl_ramp_7d": ctl_ramp_7d,
    }


def compute_layer3_monthly_28d(
    sessions_df: pd.DataFrame,
    daily_df: pd.DataFrame,
    baseline: Dict[str, Any],
    now_kst: datetime,
) -> Dict[str, Any]:
    """계층 3 [월간 · 28일 롤링]: 순수 Z2 EF 추이, 28일 MMP 곡선, 동일 출력 심박 비용, 정체·과훈련 경보"""
    cutoff_28d = (now_kst - timedelta(days=28)).replace(tzinfo=None)
    cutoff_56d = (now_kst - timedelta(days=56)).replace(tzinfo=None)
    official_ftp = float(baseline["official_ftp_w"])

    z2_avg_ef: Optional[float] = None
    z2_prev_ef: Optional[float] = None
    z2_ef_delta_pct: Optional[float] = None
    z2_avg_decoupling: Optional[float] = None

    mmp_1m_28d: Optional[float] = None
    mmp_5m_28d: Optional[float] = None
    mmp_20m_28d: Optional[float] = None
    candidate_eftp_w: Optional[float] = None

    tempo_hr_28d: Optional[float] = None
    threshold_hr_28d: Optional[float] = None

    if not sessions_df.empty:
        s_28d = sessions_df[sessions_df["start_dt"] >= cutoff_28d]
        s_prev_28d = sessions_df[(sessions_df["start_dt"] >= cutoff_56d) & (sessions_df["start_dt"] < cutoff_28d)]

        z2_curr = s_28d[(s_28d["duration_min"] >= 45.0) & (s_28d["if_194"] <= 0.68) & (s_28d["ef"].notna())]
        if not z2_curr.empty:
            z2_avg_ef = round(float(z2_curr["ef"].mean()), 2)
            dec_vals = z2_curr["decoupling_pct"].dropna()
            if not dec_vals.empty:
                z2_avg_decoupling = round(float(dec_vals.mean()), 2)

        z2_prev = s_prev_28d[(s_prev_28d["duration_min"] >= 45.0) & (s_prev_28d["if_194"] <= 0.68) & (s_prev_28d["ef"].notna())]
        if not z2_prev.empty:
            z2_prev_ef = round(float(z2_prev["ef"].mean()), 2)

        if z2_avg_ef is not None and z2_prev_ef is not None and z2_prev_ef > 0:
            z2_ef_delta_pct = round(((z2_avg_ef - z2_prev_ef) / z2_prev_ef) * 100.0, 1)

        if not s_28d.empty:
            if s_28d["mmp_1m"].notna().any():
                mmp_1m_28d = round(float(s_28d["mmp_1m"].max()), 1)
            if s_28d["mmp_5m"].notna().any():
                mmp_5m_28d = round(float(s_28d["mmp_5m"].max()), 1)
            if s_28d["mmp_20m"].notna().any():
                mmp_20m_28d = round(float(s_28d["mmp_20m"].max()), 1)
                est_20m_ftp = round(mmp_20m_28d * 0.95, 1)
                if est_20m_ftp > official_ftp:
                    candidate_eftp_w = est_20m_ftp

        tempo_sub = s_28d[(s_28d["np_watts"] >= 140.0) & (s_28d["np_watts"] <= 160.0) & (s_28d["avg_hr"].notna())]
        if not tempo_sub.empty:
            tempo_hr_28d = round(float(tempo_sub["avg_hr"].mean()), 1)

        thresh_sub = s_28d[(s_28d["np_watts"] >= 185.0) & (s_28d["np_watts"] <= 200.0) & (s_28d["avg_hr"].notna())]
        if not thresh_sub.empty:
            threshold_hr_28d = round(float(thresh_sub["avg_hr"].mean()), 1)

    ctl_stagnant = False
    if len(daily_df) >= 22:
        ctl_now = float(daily_df.iloc[-1]["ctl_42d"])
        ctl_21d_ago = float(daily_df.iloc[-22]["ctl_42d"])
        ctl_stagnant = (ctl_now - ctl_21d_ago) < 0.5

    hrv_dropped = False
    if len(daily_df) >= 21:
        hrv_recent_7 = daily_df.tail(7)["hrv_last_night_avg"].dropna()
        hrv_prev_14 = daily_df.iloc[-21:-7]["hrv_last_night_avg"].dropna()
        if not hrv_recent_7.empty and not hrv_prev_14.empty and float(hrv_prev_14.mean()) > 0:
            hrv_diff_pct = ((float(hrv_recent_7.mean()) - float(hrv_prev_14.mean())) / float(hrv_prev_14.mean())) * 100.0
            hrv_dropped = hrv_diff_pct <= -10.0

    ef_dropped = bool(z2_ef_delta_pct is not None and z2_ef_delta_pct <= -2.0)
    stagnation_warning = bool(ctl_stagnant and ef_dropped and hrv_dropped)

    return {
        "z2_avg_ef": z2_avg_ef,
        "z2_prev_28d_ef": z2_prev_ef,
        "z2_ef_delta_pct": z2_ef_delta_pct,
        "z2_avg_decoupling_pct": z2_avg_decoupling,
        "mmp_1m_best_28d": mmp_1m_28d,
        "mmp_5m_best_28d": mmp_5m_28d,
        "mmp_20m_best_28d": mmp_20m_28d,
        "candidate_eftp_w": candidate_eftp_w,
        "cardiac_cost_140_160w_hr": tempo_hr_28d,
        "cardiac_cost_185_200w_hr": threshold_hr_28d,
        "stagnation_warning": stagnation_warning,
    }


def compute_layer4_race_readiness(
    sessions_df: pd.DataFrame,
    baseline: Dict[str, Any],
    now_kst: datetime,
) -> Dict[str, Any]:
    """계층 4 [대회 전 · 그란폰도 완주 검증]: W/kg 진척도, 9.4% 업힐 지속력, 90분+ 장거리 내구력, 연료 보급 적응"""
    today_date = now_kst.date()
    event_dt = _parse_datetime(baseline.get("event_date"))
    test_dt = _parse_datetime(baseline.get("ftp_test_date"))

    days_to_event = (event_dt.date() - today_date).days if event_dt else None
    days_to_test = (test_dt.date() - today_date).days if test_dt else None

    curr_ftp = float(baseline["official_ftp_w"])
    goal_ftp = float(baseline["target_ftp_w"])
    curr_wt = float(baseline["current_weight_kg"])
    goal_wt = float(baseline["target_weight_kg"])

    curr_wkg = round(curr_ftp / curr_wt, 2) if curr_wt > 0 else 0.0
    goal_wkg = round(goal_ftp / goal_wt, 2) if goal_wt > 0 else 3.33
    wkg_gap = round(goal_wkg - curr_wkg, 2)

    cutoff_28d = (now_kst - timedelta(days=28)).replace(tzinfo=None)
    longest_ride_28d = 0.0
    long_rides_90m_count = 0
    best_climb_power: Optional[float] = None
    max_kj_single_ride: Optional[float] = None

    if not sessions_df.empty:
        s_28d = sessions_df[sessions_df["start_dt"] >= cutoff_28d]
        if not s_28d.empty:
            longest_ride_28d = round(float(s_28d["duration_min"].fillna(0).max()), 1)

        long_90 = sessions_df[sessions_df["duration_min"] >= 90.0]
        long_rides_90m_count = int(len(long_90))

        if sessions_df["climb_avg_watts"].notna().any():
            best_climb_power = round(float(sessions_df["climb_avg_watts"].max()), 1)
        if sessions_df["kilojoules"].notna().any():
            max_kj_single_ride = round(float(sessions_df["kilojoules"].max()), 1)

    if longest_ride_28d >= 120.0:
        durability_status = f"장거리 120분+ 적응 완료 (최근 28일 최장 {longest_ride_28d}분)"
    elif longest_ride_28d >= 90.0:
        durability_status = f"90분+ 장거리 진입 완료 (최근 28일 최장 {longest_ride_28d}분 · 120분 확장 단계)"
    else:
        durability_status = f"빌드업 진행 중 (최근 28일 최장 {longest_ride_28d}분 · 주말 90~110분 장거리 확장 단계)"

    return {
        "event_name": baseline.get("event_name", "고성공룡나라그란폰도 B코스"),
        "event_date": baseline.get("event_date", "2027-04-03"),
        "days_to_event": days_to_event,
        "next_ftp_test_date": baseline.get("ftp_test_date", "2026-12-06"),
        "days_to_next_ftp_test": days_to_test,
        "current_w_per_kg": curr_wkg,
        "goal_w_per_kg": goal_wkg,
        "wkg_gap_to_goal": wkg_gap,
        "best_outdoor_climb_watts": best_climb_power,
        "longest_ride_min_28d": longest_ride_28d,
        "long_rides_90m_total_count": long_rides_90m_count,
        "max_single_ride_kj": max_kj_single_ride,
        "climb_durability_status": durability_status,
    }


# ==============================================================================
# 4. 메인 실행 및 산출물 저장
# ==============================================================================
def run_unified_engine(now_kst: Optional[datetime] = None) -> Dict[str, Any]:
    """전체 4단계 파이프라인을 실행해 CSV, JSON, TXT 산출물을 생성합니다."""
    if now_kst is None:
        now_kst = datetime.now(KST)

    baseline = _load_baseline()
    official_ftp = float(baseline["official_ftp_w"])

    fit_df = _read_csv_safe("fit_analysis_report.csv")
    garmin_act_df = _read_csv_safe("garmin_activities.csv")
    strava_act_df = _read_csv_safe("strava_activities.csv")
    strava_seg_df = _read_csv_safe("strava_segments.csv")
    health_df = _read_csv_safe("garmin_health.csv")
    readiness_df = _read_csv_safe("readiness_v2_summary.csv")
    latest_1s_info = _read_latest_1s_summary()

    sessions_df = build_unified_sessions(
        fit_df=fit_df,
        garmin_act_df=garmin_act_df,
        strava_act_df=strava_act_df,
        strava_seg_df=strava_seg_df,
        official_ftp=official_ftp,
    )

    daily_df, live_pmc = build_daily_timeseries(
        sessions_df=sessions_df,
        health_df=health_df,
        now_kst=now_kst,
    )

    layer1 = compute_layer1_daily(daily_df, readiness_df, live_pmc, now_kst)
    layer2 = compute_layer2_weekly(daily_df, now_kst)
    layer3 = compute_layer3_monthly_28d(sessions_df, daily_df, baseline, now_kst)
    layer4 = compute_layer4_race_readiness(sessions_df, baseline, now_kst)

    if layer3["candidate_eftp_w"] is None and latest_1s_info.get("candidate_eftp_w") is not None:
        if float(latest_1s_info["candidate_eftp_w"]) > official_ftp:
            layer3["candidate_eftp_w"] = latest_1s_info["candidate_eftp_w"]

    unified_state: Dict[str, Any] = {
        "generated_at_kst": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": {
            "official_ftp_w": baseline["official_ftp_w"],
            "target_ftp_w": baseline["target_ftp_w"],
            "official_lthr_bpm": baseline["official_lthr_bpm"],
            "max_hr_bpm": baseline["max_hr_bpm"],
            "current_weight_kg": baseline["current_weight_kg"],
            "target_weight_kg": baseline["target_weight_kg"],
            "current_w_per_kg": baseline["current_w_per_kg"],
        },
        "latest_ride": live_pmc.get("last_session"),
        "layer1_daily": layer1,
        "layer2_weekly": layer2,
        "layer3_monthly_28d": layer3,
        "layer4_race_readiness": layer4,
    }

    daily_df.to_csv(OUT_DAILY_CSV, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)

    OUT_STATE_JSON.write_text(
        json.dumps(unified_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lf = layer1.get("load_focus_latest") or {}
    lf_str = (
        f"LoadFocus({lf.get('ref_date')}): Low {lf.get('low_aerobic')} / High {lf.get('high_aerobic')} / Ana {lf.get('anaerobic')}"
        if lf
        else "LoadFocus: N/A"
    )
    summary_line = (
        f"UNIFIED_COACHING_STATE {unified_state['generated_at_kst']} KST | "
        f"CTL {layer1['ctl_42d']} ATL {layer1['atl_7d']} TSB {layer1['tsb']} ACWR {layer1['acwr']} | "
        f"Readiness {layer1['readiness_score']} ({layer1['readiness_status']}) | "
        f"SleepDebt3d {layer1['sleep_debt_3d_hours']}h | {lf_str} | "
        f"WeekTSS {layer2['completed_tss_194w']}/{layer2['planned_tss_194w']} ({layer2['completion_pct']}%) | "
        f"Event D-{layer4['days_to_event']} ({layer4['current_w_per_kg']} W/kg)"
    )
    OUT_SUMMARY_TXT.write_text(summary_line + "\n", encoding="utf-8")

    return unified_state


def main() -> None:
    """통합 분석 엔진 진입점"""
    print("=== 사이클링 통합 분석 엔진 실행 시작 ===", flush=True)
    state = run_unified_engine()
    l1 = state["layer1_daily"]
    l2 = state["layer2_weekly"]
    l3 = state["layer3_monthly_28d"]
    l4 = state["layer4_race_readiness"]

    print(
        f"[OK] 일간 지표: CTL {l1['ctl_42d']} · ATL {l1['atl_7d']} · TSB {l1['tsb']} · "
        f"ACWR {l1['acwr']} · 준비도 {l1['readiness_score']} ({l1['readiness_status']})",
        flush=True,
    )
    print(
        f"[OK] 주간 지표: {l2['completed_tss_194w']}/{l2['planned_tss_194w']} TSS ({l2['completion_pct']}%) · "
        f"저강도 비중 {l2['polarized_low_pct']}% · 램프율 {l2['ctl_ramp_7d']} CTL/주",
        flush=True,
    )
    print(
        f"[OK] 월간(28d): 순수 Z2 EF {l3['z2_avg_ef']} · MMP20m 최고 {l3['mmp_20m_best_28d']}W · "
        f"정체 경보 {l3['stagnation_warning']}",
        flush=True,
    )
    print(
        f"[OK] 대회(D-{l4['days_to_event']}): 현재 {l4['current_w_per_kg']} W/kg → 목표 {l4['goal_w_per_kg']} W/kg · "
        f"{l4['climb_durability_status']}",
        flush=True,
    )
    print(f"[OK] 저장 완료: {OUT_DAILY_CSV.name}, {OUT_STATE_JSON.name}, {OUT_SUMMARY_TXT.name}", flush=True)


if __name__ == "__main__":
    main()
