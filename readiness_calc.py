from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math
from typing import Any, Dict, List, Optional, Tuple


ENGINE_VERSION = "v2"
DEFAULT_FTP = 194.0


@dataclass
class ReadinessResult:
    engine_version: str
    as_of_kst: str
    score: Optional[float]
    score_range: Tuple[float, float]
    confidence: str
    completeness_pct: float
    permission_grade: str
    permission_label: str
    gate_reasons: List[str]
    breakdown: Dict[str, Dict[str, Any]]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, str) and val.strip().upper() in ("", "NONE", "NAN", "NULL", "확인 못 함"):
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (ValueError, TypeError):
        return None


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.replace(tzinfo=None) if val.tzinfo else val
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[: len(fmt)], fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        return None


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _mean_valid(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    vals = [_to_float(r.get(key)) for r in rows]
    clean = [v for v in vals if v is not None]
    return (sum(clean) / len(clean)) if clean else None


def compute_3d_sleep_debt(
    recent_3d_health: List[Dict[str, Any]],
    target_3d_hours: float = 21.0,
) -> Tuple[Optional[float], float]:
    vals = [_to_float(r.get("sleep_hours")) for r in recent_3d_health]
    clean = [v for v in vals if v is not None]
    if not clean:
        return None, 0.0
    total_3d = round(sum(clean), 2)
    debt_3d = round(max(0.0, target_3d_hours - total_3d), 2)
    return debt_3d, total_3d


def compute_session_recovery_context(
    as_of_kst: str,
    last_ride: Optional[Dict[str, Any]],
    remaining_recovery_hours: Optional[float],
    ftp: float = DEFAULT_FTP,
) -> Dict[str, Optional[float]]:
    elapsed_h: Optional[float] = None
    recommended_h: Optional[float] = None
    load_product: Optional[float] = None

    if last_ride:
        np_val = _to_float(last_ride.get("norm_power_NP"))
        if_val = _to_float(last_ride.get("intensity_factor"))
        if if_val is None and np_val is not None and ftp > 0:
            if_val = round(np_val / ftp, 3)

        tss_val = _to_float(last_ride.get("tss_194w"))
        if tss_val is None:
            tss_val = _to_float(last_ride.get("tss"))

        if if_val is not None and tss_val is not None:
            load_product = round(if_val * tss_val, 1)
            recommended_h = round(_clamp(8.0 + load_product * 0.45, 8.0, 36.0), 1)

        now_dt = _parse_dt(as_of_kst)
        end_dt = _parse_dt(last_ride.get("end_time")) or _parse_dt(last_ride.get("date"))
        if end_dt is None and now_dt is not None:
            dur_min = _to_float(last_ride.get("duration_min"))
            start_dt = _parse_dt(last_ride.get("start_time")) or _parse_dt(last_ride.get("date"))
            if start_dt is not None and dur_min is not None:
                end_dt = start_dt + timedelta(minutes=dur_min)

        if now_dt is not None and end_dt is not None:
            elapsed_h = round(max(0.0, (now_dt - end_dt).total_seconds() / 3600.0), 1)

    rem_h = remaining_recovery_hours
    if rem_h is None and recommended_h is not None and elapsed_h is not None:
        rem_h = round(max(0.0, recommended_h - elapsed_h), 1)

    return {
        "if_x_tss": load_product,
        "recommended_recovery_h": recommended_h,
        "elapsed_recovery_h": elapsed_h,
        "remaining_recovery_h": rem_h,
    }


def score_training_balance(
    tsb: Optional[float],
    acwr: Optional[float],
    last_ride: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], float, str, str]:
    if tsb is None:
        return None, 0.0, "확인 못 함", "TSB 데이터 없음(0점 미적용)"

    if -30.0 <= tsb <= -10.0:
        pts = 23.0 if tsb >= -20.0 else 20.5
        status = "적정 훈련 구간"
    elif -10.0 < tsb <= 5.0:
        pts = 22.0 - abs(tsb + 2.0) * 0.25
        status = "균형/회복 전환"
    elif tsb > 5.0:
        pts = 19.5
        status = "회복 여유(부하 점검)"
    else:
        pts = 14.0
        status = "과부하 누적 주의"

    if acwr is not None:
        if acwr >= 1.5:
            pts -= 3.5
            status += f" · ACWR {acwr:.2f}"
        elif acwr > 1.3:
            pts -= 1.5

    if last_ride:
        decouple = _to_float(last_ride.get("aerobic_decoupling_pct"))
        if decouple is not None and decouple > 15.0:
            pts -= 1.0

    pts = round(_clamp(pts, 0.0, 25.0), 1)
    detail = f"TSB {tsb:+.1f}" + (f", ACWR {acwr:.2f}" if acwr is not None else "")
    return pts, 1.0, status, detail


def score_sleep_recovery(
    today_health: Dict[str, Any],
    recent_3d_health: List[Dict[str, Any]],
    recent_7d_health: List[Dict[str, Any]],
) -> Tuple[Optional[float], float, str, str]:
    sleep_h = _to_float(today_health.get("sleep_hours"))
    sleep_score = _to_float(today_health.get("sleep_score"))
    deep_min = _to_float(today_health.get("deep_sleep_min"))
    rem_min = _to_float(today_health.get("rem_sleep_min"))

    avg_7d_h = _mean_valid(recent_7d_health, "sleep_hours")
    is_estimated = False
    if sleep_h is None and avg_7d_h is not None:
        sleep_h = avg_7d_h
        is_estimated = True

    if sleep_h is None and sleep_score is None:
        return None, 0.0, "확인 못 함", "수면 기록 없음(0점 미적용)"

    h_pts = _clamp((sleep_h / 7.5) * 10.0, 0.0, 10.0) if sleep_h is not None else 5.0
    s_pts = _clamp((sleep_score / 100.0) * 5.0, 0.0, 5.0) if sleep_score is not None else 2.5

    debt_3d, total_3d = compute_3d_sleep_debt(recent_3d_health)
    q_pts = 5.0
    if debt_3d is not None and debt_3d > 3.0:
        q_pts -= min(2.5, (debt_3d - 3.0) * 0.8)
    if deep_min is not None and deep_min < 60.0:
        q_pts -= 1.0
    if rem_min is not None and rem_min < 60.0:
        q_pts -= 1.0
    q_pts = _clamp(q_pts, 0.0, 5.0)

    total = round(_clamp(h_pts + s_pts + q_pts, 0.0, 20.0), 1)
    comp = 0.6 if is_estimated else 1.0
    label = ("추정 " if is_estimated else "") + (f"{sleep_h:.2f}h" if sleep_h is not None else "확인 못 함")
    debt_txt = f"3일부채 {debt_3d:.1f}h(3일합 {total_3d:.1f}h)" if debt_3d is not None else "3일부채 확인 못 함"
    detail = f"수면점수 {int(sleep_score) if sleep_score is not None else '-'}점 · {debt_txt}"
    if deep_min is not None or rem_min is not None:
        detail += f" · Deep {int(deep_min or 0)}m/REM {int(rem_min or 0)}m"
    return total, comp, label, detail


def score_hrv_trend(
    today_health: Dict[str, Any],
    recent_7d_health: List[Dict[str, Any]],
) -> Tuple[Optional[float], float, str, str]:
    hrv_val = _to_float(today_health.get("hrv_last_night_avg"))
    status_raw = str(today_health.get("hrv_status") or "").strip().upper()
    low_b = _to_float(today_health.get("hrv_baseline_low"))
    high_b = _to_float(today_health.get("hrv_baseline_high"))
    avg_7d = _mean_valid(recent_7d_health, "hrv_last_night_avg")

    has_baseline = (low_b is not None and high_b is not None) or (status_raw not in ("", "NONE", "UNKNOWN"))
    if hrv_val is None or not has_baseline:
        return None, 0.0, "확인 못 함", "HRV 기준선 미형성 또는 값 없음(0점 미적용)"

    if low_b is not None and high_b is not None:
        if low_b <= hrv_val <= high_b:
            pts, state = 15.0, "기준 범위 내 안정"
        elif hrv_val > high_b:
            pts, state = 14.5, "기준 범위 상회"
        elif hrv_val >= low_b * 0.9:
            pts, state = 12.0, "기준 범위 하단 근접"
        elif hrv_val >= low_b * 0.8:
            pts, state = 8.0, "기준 범위보다 낮음"
        else:
            pts, state = 4.0, "기준 범위보다 크게 낮음"
    else:
        pts, state = (14.0, "BALANCED") if status_raw == "BALANCED" else (8.0, status_raw)

    diff_txt = ""
    if avg_7d and avg_7d > 0:
        pct = ((hrv_val - avg_7d) / avg_7d) * 100.0
        diff_txt = f" (7일 평균 대비 {pct:+.1f}%)"
        if pct <= -15.0:
            pts = max(4.0, pts - 2.5)

    return round(_clamp(pts, 0.0, 15.0), 1), 1.0, f"{int(hrv_val)}ms · {state}", f"기준 {low_b or '-'}-{high_b or '-'}ms{diff_txt}"


def score_recovery_time(
    recovery_ctx: Dict[str, Optional[float]],
) -> Tuple[Optional[float], float, str, str]:
    rem = recovery_ctx.get("remaining_recovery_h")
    elapsed = recovery_ctx.get("elapsed_recovery_h")
    rec_h = recovery_ctx.get("recommended_recovery_h")
    if_x_tss = recovery_ctx.get("if_x_tss")

    if rem is None:
        return None, 0.0, "확인 못 함", "남은 회복시간 미확인(0점 미적용)"

    pts = round(max(0.0, 15.0 - min(15.0, max(0.0, rem)) * 1.0), 1)
    parts = ["시간당 -1점 감점(최대 15점)"]
    if elapsed is not None:
        parts.append(f"경과 {elapsed:.1f}h")
    if if_x_tss is not None and rec_h is not None:
        parts.append(f"IF×TSS {if_x_tss:.1f}(권장 {rec_h:.1f}h)")
    return pts, 1.0, f"남은 {rem:.1f}h", " · ".join(parts)


def score_resting_hr(
    today_health: Dict[str, Any],
    recent_7d_health: List[Dict[str, Any]],
) -> Tuple[Optional[float], float, str, str]:
    rhr = _to_float(today_health.get("resting_hr"))
    avg_7d = _mean_valid(recent_7d_health, "resting_hr")
    if rhr is None:
        return None, 0.0, "확인 못 함", "안정시 심박 없음(0점 미적용)"
    if avg_7d is None:
        return 12.0, 0.7, f"{int(rhr)} bpm", "7일 평균 기준 부재"

    delta = rhr - avg_7d
    if delta <= 1.0:
        pts = 15.0
    elif delta <= 4.0:
        pts = 12.5
    elif delta < 7.0:
        pts = 9.0
    else:
        pts = 5.0
    return round(pts, 1), 1.0, f"{int(rhr)} bpm", f"7일 평균 {avg_7d:.1f}bpm 대비 {delta:+.1f}bpm"


def score_stress_and_activity(
    today_health: Dict[str, Any],
    recent_7d_health: List[Dict[str, Any]],
) -> Tuple[Optional[float], float, str, str]:
    stress = _to_float(today_health.get("avg_stress"))
    if stress is None:
        return None, 0.0, "확인 못 함", "스트레스 데이터 없음(0점 미적용)"

    avg_7d = _mean_valid(recent_7d_health, "avg_stress") or stress
    if stress <= 25:
        pts = 10.0
    elif stress <= 35:
        pts = 8.0
    elif stress <= 45:
        pts = 5.5
    else:
        pts = 3.0

    if stress - avg_7d >= 8.0:
        pts -= 1.5

    max_stress = _to_float(today_health.get("max_stress"))
    rest_stress_sec = _to_float(today_health.get("rest_stress_sec"))
    steps = _to_float(today_health.get("steps"))
    active_kcal = _to_float(today_health.get("active_kcal"))

    extra = []
    if max_stress is not None and max_stress >= 95:
        pts -= 0.5
        extra.append(f"최대스트레스 {int(max_stress)}")
    if rest_stress_sec is not None and rest_stress_sec < 14400:
        pts -= 0.5
        extra.append(f"휴식스트레스 {rest_stress_sec/3600:.1f}h")
    if steps is not None and steps >= 13000:
        pts -= 1.0
        extra.append(f"걸음 {int(steps):,}")
    if active_kcal is not None and active_kcal >= 900:
        pts -= 0.5
        extra.append(f"활동 {int(active_kcal)}kcal")

    pts = round(_clamp(pts, 0.0, 10.0), 1)
    detail = f"7일 평균 {avg_7d:.1f}" + (f" · {', '.join(extra)}" if extra else "")
    return pts, 1.0, f"스트레스 {int(stress)}", detail


def evaluate_readiness_v2(
    as_of_kst: str,
    today_health: Dict[str, Any],
    recent_3d_health: List[Dict[str, Any]],
    recent_7d_health: List[Dict[str, Any]],
    tsb: Optional[float] = None,
    acwr: Optional[float] = None,
    remaining_recovery_hours: Optional[float] = None,
    last_ride: Optional[Dict[str, Any]] = None,
    hrv_below_baseline_2d: bool = False,
    illness_or_pain_flag: bool = False,
    ftp: float = DEFAULT_FTP,
) -> ReadinessResult:
    recovery_ctx = compute_session_recovery_context(
        as_of_kst=as_of_kst,
        last_ride=last_ride,
        remaining_recovery_hours=remaining_recovery_hours,
        ftp=ftp,
    )

    items = [
        ("training_balance", 25.0, *score_training_balance(tsb, acwr, last_ride)),
        ("sleep_recovery", 20.0, *score_sleep_recovery(today_health, recent_3d_health, recent_7d_health)),
        ("hrv_trend", 15.0, *score_hrv_trend(today_health, recent_7d_health)),
        ("recovery_time", 15.0, *score_recovery_time(recovery_ctx)),
        ("resting_hr", 15.0, *score_resting_hr(today_health, recent_7d_health)),
        ("stress_activity", 10.0, *score_stress_and_activity(today_health, recent_7d_health)),
    ]

    earned = 0.0
    available_max = 0.0
    comp_sum = 0.0
    breakdown: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []

    for key, max_p, pts, comp, label, detail in items:
        comp_sum += comp
        if pts is not None:
            earned += pts
            available_max += max_p
        else:
            notes.append(f"{key} 미확정({detail})")
        breakdown[key] = {"score": pts, "max": max_p, "label": label, "detail": detail}

    completeness_pct = round((comp_sum / len(items)) * 100.0, 1)
    confidence = "높음" if completeness_pct >= 80.0 else ("보통" if completeness_pct >= 50.0 else "낮음")

    missing_pool = max(0.0, 100.0 - available_max)
    min_possible = round(earned, 1)
    max_possible = round(earned + missing_pool, 1)
    scaled_score = round((earned / available_max) * 100.0, 1) if available_max >= 70.0 else None
    score_range = (round(max(0.0, min_possible), 1), round(min(100.0, max_possible), 1))

    rank_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    grade_labels = {
        "A": "A · 계획대로 진행",
        "B": "B · 강도 조정 (파워 5~10% 하향 또는 세트 축소)",
        "C": "C · 존2 전환 (고강도 제외·Z1~Z2 대체)",
        "D": "D · 휴식 (회복 우선·훈련 중단/휴식)",
    }
    current_rank = 0
    b_gate_count = 0
    gate_reasons: List[str] = []

    def apply_gate(grade: str, reason: str, count_as_b_warning: bool = False) -> None:
        nonlocal current_rank, b_gate_count
        gate_reasons.append(reason)
        if count_as_b_warning:
            b_gate_count += 1
        if rank_map[grade] > current_rank:
            current_rank = rank_map[grade]

    ref_score = scaled_score if scaled_score is not None else max_possible
    if ref_score < 40.0:
        apply_gate("D", f"준비도 {ref_score:.1f}점 (40 미만)")
    elif ref_score < 60.0:
        apply_gate("C", f"준비도 {ref_score:.1f}점 (40~59)")
    elif ref_score < 80.0:
        apply_gate("B", f"준비도 {ref_score:.1f}점 (60~79)")

    sleep_h = _to_float(today_health.get("sleep_hours"))
    if sleep_h is not None and sleep_h < 5.0:
        apply_gate("B", f"수면 {sleep_h:.2f}h (5시간 미만)", count_as_b_warning=True)

    debt_3d, _ = compute_3d_sleep_debt(recent_3d_health)
    if debt_3d is not None and debt_3d > 3.0:
        apply_gate("B", f"3일 누적 수면 부채 {debt_3d:.1f}h (3시간 초과)", count_as_b_warning=True)

    bb_curr = _to_float(today_health.get("body_battery_current"))
    if bb_curr is not None and bb_curr <= 30.0:
        apply_gate("B", f"Body Battery {int(bb_curr)} (30 이하)", count_as_b_warning=True)

    if acwr is not None and acwr >= 1.5:
        apply_gate("B", f"ACWR {acwr:.2f} (1.5 이상)", count_as_b_warning=True)

    rhr = _to_float(today_health.get("resting_hr"))
    avg_7d_rhr = _mean_valid(recent_7d_health, "resting_hr")
    if rhr is not None and avg_7d_rhr is not None and (rhr - avg_7d_rhr) >= 7.0:
        apply_gate("C", "안정시 심박 7일 평균 대비 +7bpm 이상 (고강도 제한·존2 전환)")

    if hrv_below_baseline_2d:
        apply_gate("C", "HRV 기준선 하단 미만 2일 이상 지속 (고강도 제한·존2 전환)")

    if b_gate_count >= 2 and current_rank < rank_map["C"]:
        apply_gate("C", f"복합 피로 경고 {b_gate_count}건 동시 발생 (존2 전환)")

    resp = _to_float(today_health.get("avg_respiration"))
    spo2 = _to_float(today_health.get("avg_spo2"))
    if (resp is not None and resp >= 20.0) or (spo2 is not None and spo2 < 92.0):
        apply_gate("D", f"호흡/SpO2 이상 징후(호흡 {resp}, SpO2 {spo2})")

    if illness_or_pain_flag:
        apply_gate("D", "몸살·발열 또는 이상 증상 신호")

    final_grade = {v: k for k, v in rank_map.items()}[current_rank]
    return ReadinessResult(
        engine_version=ENGINE_VERSION,
        as_of_kst=as_of_kst,
        score=scaled_score,
        score_range=score_range,
        confidence=confidence,
        completeness_pct=completeness_pct,
        permission_grade=final_grade,
        permission_label=grade_labels[final_grade],
        gate_reasons=gate_reasons,
        breakdown=breakdown,
        notes=notes,
    )
