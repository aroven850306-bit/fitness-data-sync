#!/usr/bin/env python3
"""
후보 eFTP / eLTHR / HRmax / 체중 검증 확정 시 performance_baseline.json을 갱신하고
함께 확인해야 할 설정·리포트·워크아웃 반영 대상을 안내하는 스크립트입니다.

사용 예시:
  python update_performance_baseline.py --ftp 202.0 --lthr 149.0
  python update_performance_baseline.py --max-hr 174 --weight 76.8
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
BASELINE_PATH = Path(
    os.getenv("PERFORMANCE_BASELINE_FILE", str(DATA_DIR / "performance_baseline.json"))
)


def default_baseline() -> Dict[str, Any]:
    """초기 기준선 파일이 없을 때 사용할 기본 기준선 딕셔너리를 반환합니다."""
    return {
        "schema_version": "1.0",
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
        "official_ftp_watts": 194.0,
        "official_lthr_bpm": 147.0,
        "max_hr_bpm": 173,
        "target_ftp_watts": 240.0,
        "current_weight_kg": 77.4,
        "target_weight_kg": 72.0,
        "vo2_max_baseline": 45,
        "mmp_5m_baseline_watts": 255.0,
        "z2_efficiency_power_watts": 118.0,
        "ftp_test_date": "2026-12-06",
        "event_date": "2027-04-03",
        "event_name": "고성공룡나라그란폰도 B코스",
        "notes": "후보 eFTP/eLTHR 감지 시 즉시 덮어쓰지 않고 검증 확정 후 갱신",
    }


def load_baseline(path: Path = BASELINE_PATH) -> Dict[str, Any]:
    """기존 performance_baseline.json을 로드하고, 누락된 기본 키는 안전하게 보완합니다."""
    base = default_baseline()
    if not path.exists():
        return base

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            base.update(loaded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"기준선 JSON 파싱 실패 ({path}): {exc}") from exc

    return base


def _validate_positive(value: Optional[float], label: str) -> Optional[float]:
    """양수 검증 헬퍼입니다."""
    if value is None:
        return None
    if value <= 0:
        raise ValueError(f"{label}은(는) 0보다 커야 합니다. (입력값: {value})")
    return float(value)


def update_baseline(
    ftp_watts: Optional[float] = None,
    lthr_bpm: Optional[float] = None,
    max_hr_bpm: Optional[int] = None,
    weight_kg: Optional[float] = None,
    vo2_max: Optional[int] = None,
    mmp_5m_watts: Optional[float] = None,
    z2_power_watts: Optional[float] = None,
    path: Path = BASELINE_PATH,
) -> Dict[str, Any]:
    """
    검증 확정된 기준 성능값을 performance_baseline.json에 반영하고
    변경 내역과 후속 반영 체크리스트를 출력합니다.
    """
    data = load_baseline(path)

    prev_ftp = data.get("official_ftp_watts")
    prev_lthr = data.get("official_lthr_bpm")
    prev_max_hr = data.get("max_hr_bpm")
    prev_weight = data.get("current_weight_kg")

    v_ftp = _validate_positive(ftp_watts, "FTP")
    v_lthr = _validate_positive(lthr_bpm, "LTHR")
    v_max_hr = _validate_positive(float(max_hr_bpm) if max_hr_bpm is not None else None, "HRmax")
    v_weight = _validate_positive(weight_kg, "체중")
    v_vo2 = _validate_positive(float(vo2_max) if vo2_max is not None else None, "VO2max")
    v_mmp5 = _validate_positive(mmp_5m_watts, "5분 MMP")
    v_z2 = _validate_positive(z2_power_watts, "순수 Z2 기준 파워")

    if v_ftp is not None:
        data["official_ftp_watts"] = round(v_ftp, 1)
    if v_lthr is not None:
        data["official_lthr_bpm"] = round(v_lthr, 1)
    if v_max_hr is not None:
        data["max_hr_bpm"] = int(round(v_max_hr))
    if v_weight is not None:
        data["current_weight_kg"] = round(v_weight, 1)
    if v_vo2 is not None:
        data["vo2_max_baseline"] = int(round(v_vo2))
    if v_mmp5 is not None:
        data["mmp_5m_baseline_watts"] = round(v_mmp5, 1)
    if v_z2 is not None:
        data["z2_efficiency_power_watts"] = round(v_z2, 1)

    data["updated_at"] = datetime.now().strftime("%Y-%m-%d")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[OK] {path} 갱신 완료 ({data['updated_at']})")
    print(f"  - FTP:     {prev_ftp}W -> {data.get('official_ftp_watts')}W")
    print(f"  - LTHR:    {prev_lthr} bpm -> {data.get('official_lthr_bpm')} bpm")
    print(f"  - HRmax:   {prev_max_hr} bpm -> {data.get('max_hr_bpm')} bpm")
    print(f"  - 체중:    {prev_weight} kg -> {data.get('current_weight_kg')} kg")
    print(
        f"  - 참고기준: VO2max {data.get('vo2_max_baseline')} · "
        f"MMP5m {data.get('mmp_5m_baseline_watts')}W · "
        f"Z2기준 {data.get('z2_efficiency_power_watts')}W"
    )

    print("\n[후속 확인 체크리스트]")
    print("  1) GitHub Actions Variables: FTP_WATTS / LTHR_BPM / MAX_HR 값 동기화 확인")
    print("  2) performance_baseline.json: official_ftp_watts / official_lthr_bpm / updated_at 반영")
    print("  3) cycling_coach_master_instructions.md: 공식 기준값 표 및 지침 업데이트")
    print("  4) cycling_daily_report_11_23.html: 기준 FTP/LTHR/HRmax, 심박 존 범위, W/kg 기준 반영")
    print("  5) 신규 Zwift 워크아웃: 이후 생성되는 zwift_YYYY-MM-DD_요일_종류.zwo 목표 파워 및 <description> 반영")
    return data


def main() -> None:
    """CLI 인자를 파싱하고 기준선 갱신 함수를 실행합니다."""
    parser = argparse.ArgumentParser(
        description="공식 성능 기준선(performance_baseline.json) 갱신"
    )
    parser.add_argument("--ftp", type=float, default=None, help="새 공식 FTP (W)")
    parser.add_argument("--lthr", type=float, default=None, help="새 공식 LTHR (bpm)")
    parser.add_argument("--max-hr", type=int, default=None, help="새 최대심박 HRmax (bpm)")
    parser.add_argument("--weight", type=float, default=None, help="현재 체중 (kg)")
    parser.add_argument("--vo2-max", type=int, default=None, help="기준 VO2max")
    parser.add_argument("--mmp-5m", type=float, default=None, help="5분 피크 기준 파워 (W)")
    parser.add_argument("--z2-power", type=float, default=None, help="순수 Z2 효율 기준 파워 (W)")
    args = parser.parse_args()

    update_baseline(
        ftp_watts=args.ftp,
        lthr_bpm=args.lthr,
        max_hr_bpm=args.max_hr,
        weight_kg=args.weight,
        vo2_max=args.vo2_max,
        mmp_5m_watts=args.mmp_5m,
        z2_power_watts=args.z2_power,
    )


if __name__ == "__main__":
    main()
