#!/usr/bin/env python3
"""
후보 eFTP / eLTHR / HRmax / 체중 검증 확정 시 performance_baseline.json을 갱신하고
검증 이력 기록, 관련 파일 반영 상태 점검, 후속 체크리스트 안내를 수행하는 스크립트입니다.

사용 예시:
  python update_performance_baseline.py --ftp 202.0 --lthr 149.0 --reason "12/06 1차 FTP 테스트 확정"
  python update_performance_baseline.py --max-hr 174 --weight 76.8 --dry-run
  python update_performance_baseline.py --inspect-only
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
BASELINE_PATH = Path(
    os.getenv("PERFORMANCE_BASELINE_FILE", str(DATA_DIR / "performance_baseline.json"))
)
HISTORY_PATH = Path(
    os.getenv(
        "PERFORMANCE_BASELINE_HISTORY_FILE",
        str(DATA_DIR / "performance_baseline_history.jsonl"),
    )
)


def default_baseline() -> Dict[str, Any]:
    """초기 기준선 파일이 없을 때 사용할 기본 기준선 딕셔너리를 반환합니다."""
    return {
        "schema_version": "1.1",
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
        "last_update_reason": "초기 기준선 설정",
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
    """양수 및 합리적 범위 검증 헬퍼입니다."""
    if value is None:
        return None
    if value <= 0:
        raise ValueError(f"{label}은(는) 0보다 커야 합니다. (입력값: {value})")
    return float(value)


def _validate_ranges(
    ftp_watts: Optional[float],
    lthr_bpm: Optional[float],
    max_hr_bpm: Optional[int],
    weight_kg: Optional[float],
) -> None:
    """사이클링 생리 지표의 비정상 입력(오타 등)을 사전에 경고/차단합니다."""
    if ftp_watts is not None and not (60.0 <= ftp_watts <= 500.0):
        raise ValueError(f"FTP 값이 비정상 범위입니다(60~500W 권장): {ftp_watts}")
    if lthr_bpm is not None and not (90.0 <= lthr_bpm <= 220.0):
        raise ValueError(f"LTHR 값이 비정상 범위입니다(90~220 bpm 권장): {lthr_bpm}")
    if max_hr_bpm is not None and not (110 <= max_hr_bpm <= 235):
        raise ValueError(f"HRmax 값이 비정상 범위입니다(110~235 bpm 권장): {max_hr_bpm}")
    if lthr_bpm is not None and max_hr_bpm is not None and lthr_bpm > max_hr_bpm:
        raise ValueError(f"LTHR({lthr_bpm} bpm)이 HRmax({max_hr_bpm} bpm)보다 클 수 없습니다.")
    if weight_kg is not None and not (35.0 <= weight_kg <= 180.0):
        raise ValueError(f"체중 값이 비정상 범위입니다(35~180 kg 권장): {weight_kg}")


def _append_history(
    record: Dict[str, Any], history_path: Path = HISTORY_PATH
) -> None:
    """기준선 변경 이력을 JSONL 파일에 순차 기록합니다."""
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[WARN] 변경 이력 기록 실패 ({history_path}): {exc}")


def inspect_related_files(
    data: Dict[str, Any], root_dir: Path = DATA_DIR
) -> List[str]:
    """
    저장소 내 주요 리포트/지침/워크플로우 파일에서 현재 기준 FTP/LTHR/HRmax/체중
    반영 여부를 점검해 요약 라인 리스트를 반환합니다.
    """
    ftp = data.get("official_ftp_watts")
    lthr = data.get("official_lthr_bpm")
    max_hr = data.get("max_hr_bpm")
    weight = data.get("current_weight_kg")

    checks: List[str] = []

    report_html = root_dir / "cycling_daily_report_11_23.html"
    if report_html.exists():
        html_text = report_html.read_text(encoding="utf-8", errors="ignore")
        ftp_matches = re.findall(r"OFFICIAL_FTP\s*=\s*([0-9]+(?:\.[0-9]+)?)", html_text)
        wt_matches = re.findall(r"WEIGHT_KG\s*=\s*([0-9]+(?:\.[0-9]+)?)", html_text)
        has_lthr = str(int(round(float(lthr)))) in html_text if lthr is not None else False
        has_max_hr = str(max_hr) in html_text if max_hr is not None else False
        checks.append(
            f"cycling_daily_report_11_23.html: 스크립트 FTP={ftp_matches or ['미발견']}, "
            f"체중={wt_matches or ['미발견']}, LTHR({lthr})언급={has_lthr}, HRmax({max_hr})언급={has_max_hr}"
        )
    else:
        checks.append("cycling_daily_report_11_23.html: 파일 미존재(현재 작업 디렉토리 기준)")

    instructions_md = root_dir / "cycling_coach_master_instructions.md"
    if instructions_md.exists():
        md_text = instructions_md.read_text(encoding="utf-8", errors="ignore")
        checks.append(
            f"cycling_coach_master_instructions.md: FTP({ftp})포함={str(int(round(float(ftp)))) in md_text}, "
            f"LTHR({lthr})포함={str(int(round(float(lthr)))) in md_text}, "
            f"체중({weight})포함={str(weight) in md_text}"
        )
    else:
        checks.append("cycling_coach_master_instructions.md: 파일 미존재(현재 작업 디렉토리 기준)")

    workflow_yml = root_dir / ".github" / "workflows" / "sync_data.yml"
    if workflow_yml.exists():
        yml_text = workflow_yml.read_text(encoding="utf-8", errors="ignore")
        checks.append(
            f".github/workflows/sync_data.yml: FTP_WATTS변수참조={'FTP_WATTS' in yml_text}, "
            f"MAX_HR변수참조={'MAX_HR' in yml_text}"
        )
    else:
        checks.append(".github/workflows/sync_data.yml: 파일 미존재(현재 작업 디렉토리 기준)")

    return checks


def update_baseline(
    ftp_watts: Optional[float] = None,
    lthr_bpm: Optional[float] = None,
    max_hr_bpm: Optional[int] = None,
    weight_kg: Optional[float] = None,
    vo2_max: Optional[int] = None,
    mmp_5m_watts: Optional[float] = None,
    z2_power_watts: Optional[float] = None,
    reason: Optional[str] = None,
    dry_run: bool = False,
    path: Path = BASELINE_PATH,
) -> Dict[str, Any]:
    """
    검증 확정된 기준 성능값을 performance_baseline.json에 반영하고
    변경 내역, 파일 점검 결과, 후속 반영 체크리스트를 출력합니다.
    """
    data = load_baseline(path)

    prev_ftp = data.get("official_ftp_watts")
    prev_lthr = data.get("official_lthr_bpm")
    prev_max_hr = data.get("max_hr_bpm")
    prev_weight = data.get("current_weight_kg")

    v_ftp = _validate_positive(ftp_watts, "FTP")
    v_lthr = _validate_positive(lthr_bpm, "LTHR")
    v_max_hr = _validate_positive(
        float(max_hr_bpm) if max_hr_bpm is not None else None, "HRmax"
    )
    v_weight = _validate_positive(weight_kg, "체중")
    v_vo2 = _validate_positive(
        float(vo2_max) if vo2_max is not None else None, "VO2max"
    )
    v_mmp5 = _validate_positive(mmp_5m_watts, "5분 MMP")
    v_z2 = _validate_positive(z2_power_watts, "순수 Z2 기준 파워")

    _validate_ranges(
        ftp_watts=v_ftp,
        lthr_bpm=v_lthr,
        max_hr_bpm=int(round(v_max_hr)) if v_max_hr is not None else None,
        weight_kg=v_weight,
    )

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

    now_dt = datetime.now()
    data["schema_version"] = "1.1"
    data["updated_at"] = now_dt.strftime("%Y-%m-%d")
    if reason and reason.strip():
        data["last_update_reason"] = reason.strip()

    cur_ftp = float(data.get("official_ftp_watts") or 1.0)
    cur_wt = float(data.get("current_weight_kg") or 1.0)
    w_per_kg = round(cur_ftp / cur_wt, 2) if cur_wt > 0 else 0.0

    mode_label = "[DRY-RUN 미리보기]" if dry_run else "[OK] 저장 완료"
    print(f"{mode_label} {path} ({data['updated_at']}) · 현재 W/kg: {w_per_kg}")
    print(f"  - FTP:     {prev_ftp}W -> {data.get('official_ftp_watts')}W")
    print(f"  - LTHR:    {prev_lthr} bpm -> {data.get('official_lthr_bpm')} bpm")
    print(f"  - HRmax:   {prev_max_hr} bpm -> {data.get('max_hr_bpm')} bpm")
    print(f"  - 체중:    {prev_weight} kg -> {data.get('current_weight_kg')} kg")
    print(
        f"  - 참고기준: VO2max {data.get('vo2_max_baseline')} · "
        f"MMP5m {data.get('mmp_5m_baseline_watts')}W · "
        f"Z2기준 {data.get('z2_efficiency_power_watts')}W"
    )
    print(f"  - 갱신사유: {data.get('last_update_reason')}")

    if not dry_run:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise RuntimeError(f"기준선 파일 저장 실패 ({path}): {exc}") from exc

        _append_history(
            {
                "timestamp": now_dt.isoformat(timespec="seconds"),
                "reason": data.get("last_update_reason"),
                "ftp": {"prev": prev_ftp, "new": data.get("official_ftp_watts")},
                "lthr": {"prev": prev_lthr, "new": data.get("official_lthr_bpm")},
                "max_hr": {"prev": prev_max_hr, "new": data.get("max_hr_bpm")},
                "weight_kg": {"prev": prev_weight, "new": data.get("current_weight_kg")},
                "w_per_kg": w_per_kg,
            }
        )

    print("\n[관련 파일 기준값 점검]")
    for item in inspect_related_files(data, path.parent):
        print(f"  * {item}")

    print("\n[후속 확인 체크리스트]")
    print("  1) GitHub Actions Variables: FTP_WATTS / LTHR_BPM / MAX_HR 값 동기화 확인")
    print("  2) performance_baseline.json: official_ftp_watts / official_lthr_bpm / updated_at 반영")
    print("  3) cycling_coach_master_instructions.md: 공식 기준값 표 및 지침 업데이트")
    print("  4) cycling_daily_report_11_23.html: 기준 FTP/LTHR/HRmax, 심박 존 범위, W/kg 기준 반영")
    print("  5) 신규 Zwift 워크아웃: 이후 생성되는 zwift_YYYY-MM-DD_요일_종류.zwo 목표 파워 및 <description> 반영")
    return data


def main() -> None:
    """CLI 인자를 파싱하고 기준선 점검 또는 갱신 함수를 실행합니다."""
    parser = argparse.ArgumentParser(
        description="공식 성능 기준선(performance_baseline.json) 갱신 및 연계 점검"
    )
    parser.add_argument("--ftp", type=float, default=None, help="새 공식 FTP (W)")
    parser.add_argument("--lthr", type=float, default=None, help="새 공식 LTHR (bpm)")
    parser.add_argument("--max-hr", type=int, default=None, help="새 최대심박 HRmax (bpm)")
    parser.add_argument("--weight", type=float, default=None, help="현재 체중 (kg)")
    parser.add_argument("--vo2-max", type=int, default=None, help="기준 VO2max")
    parser.add_argument("--mmp-5m", type=float, default=None, help="5분 피크 기준 파워 (W)")
    parser.add_argument("--z2-power", type=float, default=None, help="순수 Z2 효율 기준 파워 (W)")
    parser.add_argument("--reason", type=str, default=None, help="갱신 사유 메모")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일 저장 없이 변경 결과와 연계 점검만 미리 확인",
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="값 변경 없이 현재 기준선과 연계 파일 상태만 점검",
    )
    args = parser.parse_args()

    if args.inspect_only:
        current = load_baseline(BASELINE_PATH)
        print(f"[INFO] 현재 기준선 ({BASELINE_PATH}):")
        print(json.dumps(current, ensure_ascii=False, indent=2))
        print("\n[관련 파일 기준값 점검]")
        for item in inspect_related_files(current, BASELINE_PATH.parent):
            print(f"  * {item}")
        return

    update_baseline(
        ftp_watts=args.ftp,
        lthr_bpm=args.lthr,
        max_hr_bpm=args.max_hr,
        weight_kg=args.weight,
        vo2_max=args.vo2_max,
        mmp_5m_watts=args.mmp_5m,
        z2_power_watts=args.z2_power,
        reason=args.reason,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
