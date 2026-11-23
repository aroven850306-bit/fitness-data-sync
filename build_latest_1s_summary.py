#!/usr/bin/env python3
"""
가장 최근 .fit 파일의 1초 시계열을 바탕으로
1) 세션 전체 요약 지표(MMP, 존 체류 시간, EF, 디커플링)와
2) 시간 순서 구간별(20% 5구간) 흐름 및 고출력·코스팅·후반 심박 반응을 결합한
사람이 바로 읽을 수 있는 텍스트 요약을 생성합니다.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitparse
import numpy as np
import pandas as pd

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = DATA_DIR / "fit_files"
SUMMARY_TXT_PATH = DATA_DIR / "latest_ride_1s_summary.txt"

FTP_WATTS = float(os.getenv("FTP_WATTS", "195"))
MAX_HR = float(os.getenv("MAX_HR", "178"))


def _rolling_max_1s(arr: np.ndarray, window_sec: int) -> Optional[float]:
    if len(arr) < window_sec or window_sec <= 0:
        return None
    kernel = np.ones(window_sec, dtype=float) / float(window_sec)
    conv = np.convolve(arr, kernel, mode="valid")
    return round(float(np.max(conv)), 1) if len(conv) > 0 else None


def _extract_1s_df(fit_path: Path) -> pd.DataFrame:
    ff = fitparse.FitFile(str(fit_path))
    records: List[Dict[str, Any]] = []

    for msg in ff.get_messages("record"):
        vals = {d.name: d.value for d in msg if d.value is not None}
        if any(k in vals for k in ("power", "heart_rate", "cadence")):
            records.append({
                "timestamp": vals.get("timestamp"),
                "power": pd.to_numeric(vals.get("power"), errors="coerce"),
                "cadence": pd.to_numeric(vals.get("cadence"), errors="coerce"),
                "hr": pd.to_numeric(vals.get("heart_rate"), errors="coerce"),
            })

    if not records:
        return pd.DataFrame(columns=["timestamp", "power", "cadence", "hr"])

    df = pd.DataFrame(records)
    if df["timestamp"].notna().sum() >= 2:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        valid = ts.notna()
        if valid.sum() >= 2:
            sub = df.loc[valid].copy()
            sub["_ts"] = ts.loc[valid]
            sub = sub.sort_values("_ts").drop_duplicates(subset=["_ts"], keep="last").set_index("_ts")
            full_idx = pd.date_range(sub.index.min(), sub.index.max(), freq="1s")
            if len(full_idx) <= 43200:
                reindexed = sub.reindex(full_idx)
                for col in ("power", "cadence", "hr"):
                    reindexed[col] = pd.to_numeric(reindexed[col], errors="coerce")
                return reindexed.reset_index().rename(columns={"index": "timestamp"})

    for col in ("power", "cadence", "hr"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


def _fmt_num(val: Optional[float], unit: str = "", digits: int = 1) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "-"
    return f"{val:.{digits}f}{unit}"


def build_combined_summary(fit_path: Path, ftp: float = FTP_WATTS, max_hr: float = MAX_HR) -> str:
    df_1s = _extract_1s_df(fit_path)
    n = len(df_1s)
    if n == 0:
        return f"[직전 라이딩 1초 요약] {fit_path.name}: 1초 레코드가 없습니다."

    p_raw = df_1s["power"].to_numpy(dtype=float)
    cad_raw = df_1s["cadence"].to_numpy(dtype=float)
    hr_raw = df_1s["hr"].to_numpy(dtype=float)

    p_zero = np.nan_to_num(p_raw, nan=0.0)
    p_valid = p_raw[~np.isnan(p_raw)]
    cad_valid = cad_raw[~np.isnan(cad_raw)]
    hr_valid = hr_raw[~np.isnan(hr_raw)]

    duration_min = round(n / 60.0, 1)
    avg_p = round(float(np.mean(p_zero)), 1) if len(p_zero) else None
    np_p = None
    if len(p_zero) >= 30:
        p30 = np.convolve(p_zero, np.ones(30) / 30.0, mode="valid")
        np_p = round(float(np.mean(p30 ** 4) ** 0.25), 1)
    elif len(p_valid) > 0:
        np_p = round(float(np.mean(p_valid)), 1)

    vi = round(float(np_p) / float(avg_p), 2) if (np_p and avg_p and avg_p > 0) else None
    work_kj = round(float(np.sum(p_zero)) / 1000.0, 1)
    coast_pct = round(float(np.mean(p_zero <= 0.0) * 100.0), 1)

    mmp_5s = _rolling_max_1s(p_zero, 5)
    mmp_30s = _rolling_max_1s(p_zero, 30)
    mmp_1m = _rolling_max_1s(p_zero, 60)
    mmp_5m = _rolling_max_1s(p_zero, 300)
    mmp_20m = _rolling_max_1s(p_zero, 1200)
    est_ftp = round(mmp_20m * 0.95, 1) if mmp_20m else None

    avg_cad = round(float(np.mean(cad_valid)), 1) if len(cad_valid) else None
    avg_hr = round(float(np.mean(hr_valid)), 1) if len(hr_valid) else None
    max_hr_obs = round(float(np.max(hr_valid)), 1) if len(hr_valid) else None

    pct_ftp = p_zero / ftp if ftp > 0 else np.zeros_like(p_zero)
    pz1 = round(float(np.sum(pct_ftp < 0.56)) / 60.0, 1)
    pz2 = round(float(np.sum((pct_ftp >= 0.56) & (pct_ftp < 0.76))) / 60.0, 1)
    pz3 = round(float(np.sum((pct_ftp >= 0.76) & (pct_ftp < 0.91))) / 60.0, 1)
    pz4 = round(float(np.sum((pct_ftp >= 0.91) & (pct_ftp < 1.06))) / 60.0, 1)
    pz5 = round(float(np.sum(pct_ftp >= 1.06)) / 60.0, 1)

    ef1 = ef2 = decoup = None
    if n >= 120:
        mid = n // 2
        m_p1, m_p2 = float(np.mean(p_zero[:mid])), float(np.mean(p_zero[mid:]))
        hr1_arr = hr_raw[:mid][~np.isnan(hr_raw[:mid])]
        hr2_arr = hr_raw[mid:][~np.isnan(hr_raw[mid:])]
        if len(hr1_arr) > 0 and len(hr2_arr) > 0 and m_p1 > 0 and m_p2 > 0:
            m_hr1, m_hr2 = float(np.mean(hr1_arr)), float(np.mean(hr2_arr))
            if m_hr1 > 0 and m_hr2 > 0:
                ef1 = round(m_p1 / m_hr1, 2)
                ef2 = round(m_p2 / m_hr2, 2)
                decoup = round(((ef1 - ef2) / ef1) * 100.0, 1)

    lines: List[str] = [
        f"=== 직전 라이딩 1초 통합 요약 ({fit_path.name}) ===",
        f"[1. 전체 요약] 시간: {duration_min}분({n}초) | 평균파워: {_fmt_num(avg_p, 'W')} | NP: {_fmt_num(np_p, 'W')} | VI: {_fmt_num(vi, '', 2)} | 일량: {work_kj}kJ | 0W비율: {coast_pct}%",
        f"[2. 최대출력(MMP)] 5s: {_fmt_num(mmp_5s, 'W')} | 30s: {_fmt_num(mmp_30s, 'W')} | 1m: {_fmt_num(mmp_1m, 'W')} | 5m: {_fmt_num(mmp_5m, 'W')} | 20m: {_fmt_num(mmp_20m, 'W')} (추정FTP: {_fmt_num(est_ftp, 'W')})",
        f"[3. 존체류(분)] Z1:{pz1} | Z2:{pz2} | Z3:{pz3} | Z4:{pz4} | Z5:{pz5} | 평균심박: {_fmt_num(avg_hr, 'bpm')} (최대 {_fmt_num(max_hr_obs, 'bpm')}) | 평균케이던스: {_fmt_num(avg_cad, 'rpm')}",
        f"[4. 효율·디커플링] 전반EF: {_fmt_num(ef1, '', 2)} -> 후반EF: {_fmt_num(ef2, '', 2)} | 디커플링: {_fmt_num(decoup, '%')}",
        "[5. 시간대별 5구간(각 20%) 흐름]",
    ]

    seg_edges = np.linspace(0, n, 6, dtype=int)
    peak_seg_idx = 0
    peak_seg_power = -1.0
    max_coast_seg_idx = 0
    max_coast_ratio = -1.0

    for i in range(5):
        s, e = int(seg_edges[i]), int(seg_edges[i + 1])
        if e <= s:
            continue
        t_start = round(s / 60.0, 1)
        t_end = round(e / 60.0, 1)
        sp = float(np.mean(p_zero[s:e]))
        scad_arr = cad_raw[s:e][~np.isnan(cad_raw[s:e])]
        shr_arr = hr_raw[s:e][~np.isnan(hr_raw[s:e])]
        scoast = float(np.mean(p_zero[s:e] <= 0.0) * 100.0)

        scad = round(float(np.mean(scad_arr)), 1) if len(scad_arr) else None
        shr = round(float(np.mean(shr_arr)), 1) if len(shr_arr) else None

        if sp > peak_seg_power:
            peak_seg_power = sp
            peak_seg_idx = i + 1
        if scoast > max_coast_ratio:
            max_coast_ratio = scoast
            max_coast_seg_idx = i + 1

        lines.append(
            f"  - 구간{i+1} ({t_start}~{t_end}분): 평균 {sp:.1f}W | 심박 {_fmt_num(shr, 'bpm')} | 케이던스 {_fmt_num(scad, 'rpm')} | 0W {scoast:.1f}%"
        )

    hr_trend_note = "심박 비교 데이터 부족"
    if len(hr_valid) >= 60:
        first_hr = float(np.mean(hr_raw[: n // 2][~np.isnan(hr_raw[: n // 2])]))
        last_hr = float(np.mean(hr_raw[int(n * 0.8) :][~np.isnan(hr_raw[int(n * 0.8) :])]))
        hr_diff = round(last_hr - first_hr, 1)
        if hr_diff >= 6:
            hr_trend_note = f"후반(80~100%) 심박이 전반 대비 +{hr_diff}bpm 상승(피로·드리프트 누적 경향)"
        elif hr_diff <= -4:
            hr_trend_note = f"후반(80~100%) 심박이 전반 대비 {hr_diff}bpm 안정/하강(쿨다운 또는 여력 유지)"
        else:
            hr_trend_note = f"후반(80~100%) 심박이 전반 대비 {hr_diff:+.1f}bpm로 비교적 일정 유지"

    lines.append(
        f"[6. 핵심 이벤트] 최고출력 구간: 구간{peak_seg_idx} ({peak_seg_power:.1f}W) | 코스팅 최다 구간: 구간{max_coast_seg_idx} ({max_coast_ratio:.1f}%) | 후반 심박 반응: {hr_trend_note}"
    )

    return "\n".join(lines)


def main() -> None:
    fit_files = sorted(FIT_DIR.glob("*.fit"), key=lambda p: p.name, reverse=True)
    if not fit_files:
        print(f"[INFO] {FIT_DIR} 폴더에 .fit 파일이 없습니다.", flush=True)
        return

    latest_fit = fit_files[0]
    summary_text = build_combined_summary(latest_fit, ftp=FTP_WATTS, max_hr=MAX_HR)
    SUMMARY_TXT_PATH.write_text(summary_text + "\n", encoding="utf-8")
    print(summary_text, flush=True)
    print(f"\n[OK] 통합 요약 저장 완료: {SUMMARY_TXT_PATH}", flush=True)


if __name__ == "__main__":
    main()
