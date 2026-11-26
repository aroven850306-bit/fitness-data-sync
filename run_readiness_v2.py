      #!/usr/bin/env python3
"""훈련 준비도 v2 자동 실행 및 저장 스크립트."""

from __future__ import annotations

import csv
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
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


def main() -> None:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
    health_rows = _load_rows(ROOT_DIR / "garmin_health.csv", "date")
    fit_rows = _load_rows(ROOT_DIR / "fit_analysis_report.csv", "date")

    today_health = health_rows[0] if health_rows else {}
    recent_3d_health = health_rows[:3]
    recent_7d_health = health_rows[:7]
    last_ride = fit_rows[0] if fit_rows else None

    tsb = _optional_float(today_health.get("tsb"))
    acwr = _optional_float(today_health.get("acwr"))
    rem_rec_h = _optional_float(today_health.get("remaining_recovery_hours"))

    ftp = _optional_float(os.getenv("FTP_WATTS")) or DEFAULT_FTP
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
