#!/usr/bin/env python3
"""훈련 준비도 v2 자동 실행 및 저장 스크립트 (TSB/ACWR 및 직전 세션 회복 경과 보완 포함)."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from readiness_calc import DEFAULT_FTP, evaluate_readiness_v2


ROOT_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
OUT_JSON = ROOT_DIR / "readiness_v2.json"
OUT_CSV = ROOT_DIR / "readiness_v2_summary.csv"


def _load_rows(csv_path: Path, date_col: str = "date") -> List[Dict[str, Any]]:
    if not csv_path.exists():
        return []
    df = pd.read_csv(csv_path)
    if df.empty:
        return []
    if date_col in df.columns:
        df = df.sort_values(date_col, ascending=False)
    return df.to_dict(orient="records")


def _optional_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        num = float(val)
        return num if pd.notna(num) else None
    except (ValueError, TypeError):
        return None


def _extract_day_str(val: Any) -> Optional[str]:
    if val is None or val == "":
        return None
    s = str(val).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _estimate_tss_from_fit_row(row: Dict[str, Any], ftp: float) -> Optional[float]:
    if ftp <= 0:
        return None
    dur_min = _optional_float(row.get("duration_min"))
    np_w = _optional_float(row.get("NP_watts"))
    if dur_min is None or np_w is None or dur_min <= 0 or np_w <= 0:
        return None
    if_val = np_w / ftp
    tss = ((dur_min * 60.0 * np_w * if_val) / (ftp * 3600.0)) * 100.0
    return round(tss, 1)


def _compute_ewma_load_metrics(
    fit_rows: List[Dict[str, Any]],
    as_of_day_str: str,
    ftp: float,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """fit_analysis_report 이력 기반 일별 TSS 집계 후 42일 CTL / 7일 ATL / TSB / ACWR 산출."""
    if not fit_rows or ftp <= 0:
        return None, None, None, None

    daily_tss: Dict[str, float] = {}
    for r in fit_rows:
        d_str = _extract_day_str(r.get("date")) or _extract_day_str(r.get("file"))
        tss_val = _estimate_tss_from_fit_row(r, ftp)
        if d_str and tss_val is not None:
            daily_tss[d_str] = round(daily_tss.get(d_str, 0.0) + tss_val, 1)

    if not daily_tss:
        return None, None, None, None

    try:
        end_date = datetime.strptime(as_of_day_str, "%Y-%m-%d").date()
    except ValueError:
        end_date = max(datetime.strptime(k, "%Y-%m-%d").date() for k in daily_tss.keys())

    start_date = min(datetime.strptime(k, "%Y-%m-%d").date() for k in daily_tss.keys())
    if (end_date - start_date).days > 180:
        start_date = end_date - timedelta(days=180)

    ctl = 0.0
    atl = 0.0
    cur = start_date
    while cur <= end_date:
        tss_today = daily_tss.get(cur.isoformat(), 0.0)
        ctl = ctl + (tss_today - ctl) / 42.0
        atl = atl + (tss_today - atl) / 7.0
        cur += timedelta(days=1)

    tsb = round(ctl - atl, 1)
    acwr = round(atl / ctl, 2) if ctl > 0 else None
    return round(ctl, 1), round(atl, 1), tsb, acwr


def _enrich_last_ride_for_recovery(last_ride: Optional[Dict[str, Any]], ftp: float) -> Optional[Dict[str, Any]]:
    if not last_ride:
        return None
    enriched = dict(last_ride)
    np_w = _optional_float(enriched.get("NP_watts"))
    if np_w is not None and ftp > 0 and enriched.get("intensity_factor") is None:
        enriched["intensity_factor"] = round(np_w / ftp, 3)
    if enriched.get("tss_194w") is None:
        est_tss = _estimate_tss_from_fit_row(enriched, ftp)
        if est_tss is not None:
            enriched["tss_194w"] = est_tss
    if enriched.get("end_time") is None:
        d_str = _extract_day_str(enriched.get("date")) or _extract_day_str(enriched.get("file"))
        dur_min = _optional_float(enriched.get("duration_min"))
        if d_str:
            # 세션 시각 미기재 시 해당 일자 19:00 KST 기준 종료 시각으로 근사
            base_dt = datetime.strptime(f"{d_str} 19:00:00", "%Y-%m-%d %H:%M:%S")
            if dur_min is not None:
                enriched["end_time"] = base_dt.strftime("%Y-%m-%d %H:%M:%S")
    return enriched


def main() -> None:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    as_of_day = now_kst[:10]

    health_rows = _load_rows(ROOT_DIR / "garmin_health.csv", "date")
    fit_rows = _load_rows(ROOT_DIR / "fit_analysis_report.csv", "date")

    today_health = health_rows[0] if health_rows else {}
    recent_3d_health = health_rows[:3]
    recent_7d_health = health_rows[:7]

    ftp = _optional_float(os.getenv("FTP_WATTS")) or DEFAULT_FTP
    last_ride = _enrich_last_ride_for_recovery(fit_rows[0] if fit_rows else None, ftp)

    ctl_calc, atl_calc, tsb_calc, acwr_calc = _compute_ewma_load_metrics(fit_rows, as_of_day, ftp)

    tsb = _optional_float(today_health.get("tsb"))
    if tsb is None:
        tsb = tsb_calc

    acwr = _optional_float(today_health.get("acwr"))
    if acwr is None:
        acwr = acwr_calc

    rem_rec_h = _optional_float(today_health.get("remaining_recovery_hours"))
    illness_flag = os.getenv("ILLNESS_OR_PAIN_FLAG", "").strip().lower() in ("1", "true", "yes")

    result = evaluate_readiness_v2(
        as_of_kst=now_kst,
        today_health=today_health,
        recent_3d_health=recent_3d_health,
        recent_7d_health=recent_7d_health,
        tsb=tsb,
        acwr=acwr,
        remaining_recovery_hours=rem_rec_h,
        last_ride=last_ride,
        illness_or_pain_flag=illness_flag,
        ftp=ftp,
    )

    payload = result.to_dict()
    payload["derived_load_context"] = {
        "ctl_42d": ctl_calc,
        "atl_7d": atl_calc,
        "tsb": tsb,
        "acwr": acwr,
        "source": "garmin_health" if _optional_float(today_health.get("tsb")) is not None else "fit_analysis_report_ewma",
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    flat_row = {
        "engine_version": result.engine_version,
        "as_of_kst": result.as_of_kst,
        "score": result.score,
        "score_range_min": result.score_range[0],
        "score_range_max": result.score_range[1],
        "confidence": result.confidence,
        "completeness_pct": result.completeness_pct,
        "permission_grade": result.permission_grade,
        "permission_label": result.permission_label,
        "ctl_42d": ctl_calc,
        "atl_7d": atl_calc,
        "tsb_used": tsb,
        "acwr_used": acwr,
        "gate_reasons": " | ".join(result.gate_reasons),
        "notes": " | ".join(result.notes),
    }
    pd.DataFrame([flat_row]).to_csv(
        OUT_CSV,
        index=False,
        encoding="utf-8-sig",
        quoting=csv.QUOTE_ALL,
    )
    print(f"[OK] 훈련 준비도 v2 저장 완료: {OUT_JSON} / {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
