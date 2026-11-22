import os
import glob
import numpy as np
import pandas as pd
from fitparse import FitFile


def calculate_mmp(power_series, seconds):
    """지정된 시간(초) 동안의 최대 평균 파워(MMP) 계산"""
    if len(power_series) < seconds:
        return None
    rolling_mean = power_series.rolling(window=seconds).mean()
    return round(float(rolling_mean.max()), 1)


def calculate_decoupling(df):
    """전반부 대비 후반부 유산소 디커플링(심박 드리프트 %) 계산"""
    valid = df[(df["power"] > 0) & (df["heart_rate"] > 0)].copy()
    if len(valid) < 600:  # 최소 10분 이상 데이터 필요
        return None

    mid = len(valid) // 2
    first_half = valid.iloc[:mid]
    second_half = valid.iloc[mid:]

    ef1 = first_half["power"].mean() / first_half["heart_rate"].mean()
    ef2 = second_half["power"].mean() / second_half["heart_rate"].mean()

    if ef1 == 0:
        return None
    decoupling = ((ef1 - ef2) / ef1) * 100
    return round(float(decoupling), 2)


def analyze_all_fit_files(fit_dir="fit_files", current_ftp=220):
    fit_paths = sorted(glob.glob(os.path.join(fit_dir, "*.fit")), reverse=True)
    if not fit_paths:
        print("⚠️ 분석할 .fit 파일이 없습니다.", flush=True)
        return

    results = []
    for path in fit_paths:
        try:
            fitfile = FitFile(path)
            records = []
            for record in fitfile.get_messages("record"):
                row = {d.name: d.value for d in record}
                records.append(row)

            if not records:
                continue

            df = pd.DataFrame(records)
            for col in ["power", "heart_rate", "cadence", "left_right_balance"]:
                if col not in df.columns:
                    df[col] = np.nan

            # 문자열('right' 등)이나 결측치가 섞여 있어도 안전하게 숫자로 변환
            df["power"] = pd.to_numeric(df["power"], errors="coerce")
            df["heart_rate"] = pd.to_numeric(df["heart_rate"], errors="coerce")
            df["cadence"] = pd.to_numeric(df["cadence"], errors="coerce")

            power = df["power"].fillna(0)
            valid_power = df["power"].dropna()
            valid_hr = df["heart_rate"].dropna()
            valid_cad = df["cadence"].dropna()

            # Normalized Power (NP) 계산: 30초 이동평균의 4제곱 평균의 4제곱근
            rolling_30s = power.rolling(window=30, min_periods=1).mean()
            np_watts = round(float((rolling_30s ** 4).mean() ** 0.25), 1) if len(power) > 0 else None

            # 파워 존 분포 (분 단위)
            z1_recovery_min = round(float((power < current_ftp * 0.56).sum() / 60), 1)
            z2_endurance_min = round(float(((power >= current_ftp * 0.56) & (power <= current_ftp * 0.75)).sum() / 60), 1)
            z3_tempo_min = round(float(((power > current_ftp * 0.75) & (power <= current_ftp * 0.90)).sum() / 60), 1)
            z4_threshold_min = round(float(((power > current_ftp * 0.90) & (power <= current_ftp * 1.05)).sum() / 60), 1)
            z5_vo2max_min = round(float((power > current_ftp * 1.05).sum() / 60), 1)

            # 좌우 밸런스 평균 (문자열 'right' 제외 후 비트 플래그 보정)
            lr_num = pd.to_numeric(df["left_right_balance"], errors="coerce").dropna()
            lr_clean = lr_num.apply(lambda x: (int(x) & 0x7F) if x > 100 else float(x))
            left_pct = round(float(lr_clean.mean()), 1) if len(lr_clean) > 0 else None

            mmp_20m = calculate_mmp(power, 1200)
            est_ftp = round(mmp_20m * 0.95, 1) if mmp_20m else None

            results.append({
                "file": os.path.basename(path),
                "duration_min": round(len(df) / 60, 1),
                "avg_power": round(float(valid_power.mean()), 1) if len(valid_power) > 0 else None,
                "NP_watts": np_watts,
                "max_power": round(float(valid_power.max()), 1) if len(valid_power) > 0 else None,
                "MMP_5s": calculate_mmp(power, 5),
                "MMP_1m": calculate_mmp(power, 60),
                "MMP_5m": calculate_mmp(power, 300),
                "MMP_20m": mmp_20m,
                "est_FTP_from_20m": est_ftp,
                "avg_hr": round(float(valid_hr.mean()), 1) if len(valid_hr) > 0 else None,
                "max_hr": round(float(valid_hr.max()), 1) if len(valid_hr) > 0 else None,
                "avg_cadence": round(float(valid_cad.mean()), 1) if len(valid_cad) > 0 else None,
                "aerobic_decoupling_pct": calculate_decoupling(df),
                "left_balance_pct": left_pct,
                "Z1_recovery_min": z1_recovery_min,
                "Z2_endurance_min": z2_endurance_min,
                "Z3_tempo_min": z3_tempo_min,
                "Z4_threshold_min": z4_threshold_min,
                "Z5_vo2max_min": z5_vo2max_min,
            })
            print(f"📊 FIT 정밀 분석 완료: {os.path.basename(path)}", flush=True)

        except Exception as e:
            print(f"⚠️ FIT 분석 오류 ({path}): {e}", flush=True)

    if results:
        df_res = pd.DataFrame(results)
        df_res.to_csv("fit_analysis_report.csv", index=False, encoding="utf-8-sig")
        print(f"✅ 총 {len(df_res)}개 FIT 파일 정밀 분석 리포트(fit_analysis_report.csv) 저장 완료!", flush=True)


if __name__ == "__main__":
    analyze_all_fit_files()
