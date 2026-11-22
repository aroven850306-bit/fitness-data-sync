#!/usr/bin/env python3
"""
누적 .fit 파일 전체 정밀 분석 스크립트 (전체 업그레이드 버전)
- 디렉토리 내 과거 .fit 파일 전체(22개 이상) 일괄 분석 및 누적 CSV 생성
- MMP(5초/1분/5분/20분), eFTP, NP, IF, TSS, 파워 존(Z1~Z5) 분포
- 에어로빅 디커플링(Pw:HR Drift), 효율 지수(EF = NP / Avg HR)
- 좌우 밸런스(%), 좌우 토크 효율(TE %), 좌우 페달 부드러움(Pedal Smoothness %) 추출
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fitparse import FitFile

load_dotenv()

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "./fitness_data"))
FIT_DIR = DATA_DIR / "fit_files"
OUTPUT_CSV = DATA_DIR / "fit_analysis_summary.csv"
FTP_WATTS = float(os.getenv("FTP_WATTS", "195"))


def compute_normalized_power(power_series: pd.Series) -> float:
    """30초 롤링 평균의 4제곱 평균의 4제곱근으로 Normalized Power(NP) 계산"""
    p = power_series.fillna(0).astype(float)
    if len(p) < 30:
        return round(float(p.mean()), 1)
    rolling_30s = p.rolling(window=30, min_periods=1).mean()
    np_val = float(np.mean(np.power(rolling_30s, 4)) ** 0.25)
    return round(np_val, 1)


def compute_mmp(power_series: pd.Series, window_sec: int) -> Optional[float]:
    """지정된 초(window_sec) 구간의 최대 평균 파워(MMP) 계산"""
    p = power_series.fillna(0).astype(float)
    if len(p) < window_sec:
        return None
    max_val = float(p.rolling(window=window_sec).mean().max())
    return round(max_val, 1)


def compute_aerobic_decoupling(df: pd.DataFrame) -> Optional[float]:
    """전반기 대비 후반기 Pw:HR 효율 비율 변화율(Aerobic Decoupling %) 계산"""
    valid = df[(df["power"] > 0) & (df["heart_rate"] > 0)].copy()
    if len(valid) < 600:  # 최소 10분 이상 유효 데이터 필요
        return None

    mid = len(valid) // 2
    first_half = valid.iloc[:mid]
    second_half = valid.iloc[mid:]

    ef1 = first_half["power"].mean() / first_half["heart_rate"].mean()
    ef2 = second_half["power"].mean() / second_half["heart_rate"].mean()
    if ef1 <= 0:
        return None

    decoupling = ((ef1 - ef2) / ef1) * 100.0
    return round(float(decoupling), 2)


def parse_left_right_balance(series: pd.Series) -> Optional[float]:
    """
    FIT left_right_balance 필드에서 좌측 비율(%) 추출
    - bit 7(0x80=128)이 세팅된 경우 하위 7비트가 우측 비율이므로 100 - right 로 환산
    """
    s = series.dropna()
    if s.empty:
        return None

    left_vals: List[float] = []
    for raw in s:
        try:
            val = float(raw)
        except Exception:
            continue
        if val <= 0:
            continue
        int_val = int(round(val))
        if int_val >= 128:
            right_pct = int_val & 0x7F
            if 0 < right_pct < 100:
                left_vals.append(100.0 - right_pct)
        elif 0 < val < 100:
            left_vals.append(val)

    if not left_vals:
        return None
    return round(float(np.mean(left_vals)), 1)


def safe_mean(df: pd.DataFrame, col: str) -> Optional[float]:
    """컬럼 존재 여부 확인 후 양수 유효값 평균 반환"""
    if col not in df.columns:
        return None
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    s = s[s > 0]
    if s.empty:
        return None
    return round(float(s.mean()), 1)


def analyze_single_fit(fit_path: Path, ftp: float = FTP_WATTS) -> Optional[Dict[str, Any]]:
    """단일 .fit 파일을 파싱하여 모든 파워·심박·EF·사이클링 역학 지표 산출"""
    try:
        fit = FitFile(str(fit_path))
        records: List[Dict[str, Any]] = []
        session_info: Dict[str, Any] = {}

        for msg in fit.get_messages("session"):
            for field in msg:
                session_info[field.name] = field.value

        for msg in fit.get_messages("record"):
            row = {field.name: field.value for field in msg}
            records.append(row)

        if not records:
            return None

        df = pd.DataFrame(records)
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)

        total_sec = len(df)
        duration_min = round(total_sec / 60.0, 1)

        power = pd.to_numeric(df.get("power", pd.Series(dtype=float)), errors="coerce")
        hr = pd.to_numeric(df.get("heart_rate", pd.Series(dtype=float)), errors="coerce")
        cadence = pd.to_numeric(df.get("cadence", pd.Series(dtype=float)), errors="coerce")

        df["power"] = power
        df["heart_rate"] = hr

        valid_power = power.dropna()
        valid_hr = hr[hr > 0].dropna()
        valid_cad = cadence[cadence > 0].dropna()

        avg_power = round(float(valid_power.mean()), 1) if not valid_power.empty else None
        max_power = round(float(valid_power.max()), 1) if not valid_power.empty else None
        np_watts = compute_normalized_power(power) if not valid_power.empty else None

        avg_hr = round(float(valid_hr.mean()), 1) if not valid_hr.empty else None
        max_hr = round(float(valid_hr.max()), 1) if not valid_hr.empty else None
        avg_cadence = round(float(valid_cad.mean()), 1) if not valid_cad.empty else None

        # EF (Efficiency Factor = NP / Avg HR)
        efficiency_factor = (
            round(float(np_watts) / float(avg_hr), 2)
            if (np_watts and avg_hr and avg_hr > 0)
            else None
        )

        # MMP 및 20분 파워 기반 eFTP
        mmp_5s = compute_mmp(power, 5)
        mmp_1m = compute_mmp(power, 60)
        mmp_5m = compute_mmp(power, 300)
        mmp_20m = compute_mmp(power, 1200)
        est_ftp = round(mmp_20m * 0.95, 1) if mmp_20m else None

        # 디커플링 및 좌우 밸런스
        decoupling_pct = compute_aerobic_decoupling(df)
        left_bal = (
            parse_left_right_balance(df["left_right_balance"])
            if "left_right_balance" in df.columns
            else None
        )
        right_bal = round(100.0 - left_bal, 1) if left_bal is not None else None

        # 페달 부드러움(Pedal Smoothness) 및 토크 효율(Torque Effectiveness)
        left_ps = safe_mean(df, "left_pedal_smoothness") or session_info.get("avg_left_pedal_smoothness")
        right_ps = safe_mean(df, "right_pedal_smoothness") or session_info.get("avg_right_pedal_smoothness")
        if left_ps is None and right_ps is None:
            combined_ps = safe_mean(df, "combined_pedal_smoothness") or session_info.get("avg_combined_pedal_smoothness")
        else:
            vals = [v for v in (left_ps, right_ps) if v is not None]
            combined_ps = round(float(np.mean(vals)), 1) if vals else None

        left_te = safe_mean(df, "left_torque_effectiveness") or session_info.get("avg_left_torque_effectiveness")
        right_te = safe_mean(df, "right_torque_effectiveness") or session_info.get("avg_right_torque_effectiveness")

        # 파워 존 분포 (Coggan 5존 기준, 분 단위)
        p_filled = power.fillna(0)
        z1_min = round(float((p_filled < ftp * 0.56).sum()) / 60.0, 1)
        z2_min = round(float(((p_filled >= ftp * 0.56) & (p_filled < ftp * 0.76)).sum()) / 60.0, 1)
        z3_min = round(float(((p_filled >= ftp * 0.76) & (p_filled < ftp * 0.91)).sum()) / 60.0, 1)
        z4_min = round(float(((p_filled >= ftp * 0.91) & (p_filled < ftp * 1.06)).sum()) / 60.0, 1)
        z5_min = round(float((p_filled >= ftp * 1.06).sum()) / 60.0, 1)

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
            "avg_cadence": avg_cadence,
            "efficiency_factor_EF": efficiency_factor,
            "aerobic_decoupling_pct": decoupling_pct,
            "left_balance_pct": left_bal,
            "right_balance_pct": right_bal,
            "left_pedal_smoothness_pct": round(float(left_ps), 1) if left_ps is not None else None,
            "right_pedal_smoothness_pct": round(float(right_ps), 1) if right_ps is not None else None,
            "combined_pedal_smoothness_pct": round(float(combined_ps), 1) if combined_ps is not None else None,
            "left_torque_effectiveness_pct": round(float(left_te), 1) if left_te is not None else None,
            "right_torque_effectiveness_pct": round(float(right_te), 1) if right_te is not None else None,
            "Z1_recovery_min": z1_min,
            "Z2_endurance_min": z2_min,
            "Z3_tempo_min": z3_min,
            "Z4_threshold_min": z4_min,
            "Z5_vo2max_min": z5_min,
        }
    except Exception as e:
        print(f"[WARN] FIT 분석 실패 ({fit_path.name}): {e}")
        return None


def analyze_all_fits() -> pd.DataFrame:
    """FIT_DIR 내 모든 .fit 파일을 최신순으로 일괄 분석하여 CSV 저장"""
    fit_files = sorted(FIT_DIR.glob("*.fit"), key=lambda p: p.name, reverse=True)
    if not fit_files:
        print(f"[INFO] 분석할 .fit 파일이 없습니다: {FIT_DIR}")
        return pd.DataFrame()

    results: List[Dict[str, Any]] = []
    for fp in fit_files:
        res = analyze_single_fit(fp, ftp=FTP_WATTS)
        if res:
            results.append(res)

    df_out = pd.DataFrame(results)
    df_out.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"[OK] 총 {len(df_out)}개 FIT 파일 분석 완료 -> {OUTPUT_CSV}")
    return df_out


if __name__ == "__main__":
    analyze_all_fits()
