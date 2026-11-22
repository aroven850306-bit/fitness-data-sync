#!/usr/bin/env python3
"""
Garmin Connect + Strava 데이터 수집 스크립트 (체성분 제외 + 1초 단위 파워/케이던스/심박 분석)
- 체성분(체중/체지방/골격근량/BMI) 수집 제외
- 1초 단위 파워(W) · 케이던스(rpm) · 심박(bpm) 정밀 분석 지원
- 기존 수집 항목 캐싱으로 반복 실행 시 계산 시간 최소화
- 선택적 1초 시계열 압축(.json.gz) 저장 지원(용량/속도 안내 포함)
"""

import base64
import gzip
import io
import json
import os
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = DATA_DIR / "fit_files"
STREAM_1S_DIR = DATA_DIR / "streams_1s_gz"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIT_DIR.mkdir(parents=True, exist_ok=True)

GARMIN_TOKEN_DIR = os.getenv("GARMIN_TOKEN_DIR", str(DATA_DIR / ".garmin_tokens"))

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "").strip()
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()

FTP_WATTS = float(os.getenv("FTP_WATTS", "195"))
MAX_HR = float(os.getenv("MAX_HR", "178"))
WELLNESS_DAYS = int(os.getenv("WELLNESS_DAYS", "30"))
ACTIVITY_LIMIT = int(os.getenv("ACTIVITY_LIMIT", "100"))
STRAVA_DETAIL_LIMIT = int(os.getenv("STRAVA_DETAIL_LIMIT", "25"))
STRAVA_STREAM_LIMIT = int(os.getenv("STRAVA_STREAM_LIMIT", "15"))
SAVE_1S_STREAM_GZ = os.getenv("SAVE_1S_STREAM_GZ", "false").strip().lower() in ("1", "true", "yes")


def safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """엔드포인트 미지원이나 권한 이슈가 있어도 전체 수집이 멈추지 않도록 안전 호출"""
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def parse_garmin_credentials(raw_key: str) -> Tuple[str, str, Optional[str]]:
    """GARMIN_API_KEY에서 인증 수단 추출: (email, password, token_json_str)"""
    raw_key = raw_key.strip()
    if not raw_key:
        return "", "", None

    candidates = [raw_key]
    try:
        padded = raw_key + "=" * ((4 - len(raw_key) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
        if decoded:
            candidates.append(decoded)
    except Exception:
        pass

    for cand in candidates:
        if cand.startswith("{"):
            try:
                data = json.loads(cand)
                if any(k in data for k in ("di_token", "di_refresh_token", "oauth1_token", "oauth2_token")):
                    return "", "", json.dumps(data)
                u = str(data.get("email") or data.get("username") or data.get("user") or "").strip()
                p = str(data.get("password") or data.get("pass") or data.get("pwd") or "").strip()
                if u and p:
                    return u, p, None
            except Exception:
                pass

        for sep in ("\n", "|", ":"):
            if sep in cand and not cand.startswith("{"):
                parts = [x.strip() for x in cand.split(sep, 1)]
                if len(parts) == 2 and "@" in parts[0] and parts[1]:
                    return parts[0], parts[1], None

    return "", "", None


def init_garmin() -> Garmin:
    """garminconnect 0.3.x+ 인증 초기화"""
    email = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    raw_key = os.getenv("GARMIN_API_KEY", "").strip()
    garmin_tokens_env = os.getenv("GARMINTOKENS", "").strip()

    token_json_str: Optional[str] = None
    if raw_key:
        k_email, k_pass, token_json_str = parse_garmin_credentials(raw_key)
        email = email or k_email
        password = password or k_pass

    token_path = Path(GARMIN_TOKEN_DIR)

    if token_path.exists():
        try:
            c = Garmin()
            c.login(str(token_path))
            return c
        except Exception:
            pass

    for tok_candidate in (garmin_tokens_env, token_json_str):
        if tok_candidate:
            try:
                c = Garmin()
                c.login(tok_candidate)
                return c
            except Exception:
                pass

    if email and password:
        c = Garmin(email, password)
        c.login()
        token_path.mkdir(parents=True, exist_ok=True)
        if hasattr(c, "client") and hasattr(c.client, "dump"):
            c.client.dump(str(token_path))
        return c

    if raw_key.startswith("GCDC_"):
        raise ValueError(
            "현재 GitHub Secret `GARMIN_API_KEY`에 등록된 `GCDC_0...` 값은 Garmin Connect 로그인 토큰이 아닙니다.\n"
            "GitHub Secrets에 `GARMIN_EMAIL`과 `GARMIN_PASSWORD`를 등록하거나, "
            "`g.client.dumps()`로 발급한 JSON 토큰을 등록해 주세요."
        )

    raise ValueError(
        "Garmin 인증 정보가 없습니다. GitHub Secrets에 `GARMIN_EMAIL`과 `GARMIN_PASSWORD`를 등록해 주세요."
    )


def is_myfitnesspal_duplicate(row: Dict[str, Any], existing_rows: List[Dict[str, Any]]) -> bool:
    """MyFitnessPal 연동으로 생성된 수동 중복 칼로리/운동 기록 필터링"""
    name = str(row.get("name", "")).lower()
    if "myfitnesspal" in name:
        return True

    has_sensor = any(
        pd.notna(row.get(k)) and float(row.get(k) or 0) > 0
        for k in ("avg_power", "norm_power_NP", "avg_hr")
    )
    if has_sensor:
        return False

    try:
        dt_curr = datetime.strptime(str(row["date"])[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False

    for prev in existing_rows:
        try:
            dt_prev = datetime.strptime(str(prev["date"])[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if abs((dt_curr - dt_prev).total_seconds()) <= 600:
            prev_has_sensor = any(
                pd.notna(prev.get(k)) and float(prev.get(k) or 0) > 0
                for k in ("avg_power", "norm_power_NP", "avg_hr")
            )
            if prev_has_sensor:
                return True
    return False


def download_fit_file(client: Garmin, activity_id: int, start_local: str) -> Optional[Path]:
    """개별 활동의 .fit 원본 파일을 다운로드하여 누적 보관"""
    date_prefix = str(start_local)[:10] if start_local else "unknown"
    target_path = FIT_DIR / f"{date_prefix}_{activity_id}.fit"
    if target_path.exists() and target_path.stat().st_size > 0:
        return target_path

    try:
        raw_bytes = client.download_activity(
            activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
        )
        if not raw_bytes:
            return None

        if raw_bytes[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
                if not fit_names:
                    return None
                target_path.write_bytes(zf.read(fit_names[0]))
        else:
            target_path.write_bytes(raw_bytes)
        return target_path
    except Exception as e:
        print(f"[WARN] FIT 다운로드 실패 (activity_id={activity_id}): {e}", flush=True)
        return None


def fetch_garmin_activities(client: Garmin, limit: int = ACTIVITY_LIMIT) -> pd.DataFrame:
    """가민 활동 목록 수집 + 기존 CSV 병합 + FIT 다운로드"""
    csv_path = DATA_DIR / "garmin_activities.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()

    raw_activities = client.get_activities(0, limit) or []
    cleaned_rows: List[Dict[str, Any]] = []

    sorted_raw = sorted(
        raw_activities,
        key=lambda x: (
            "myfitnesspal" in str(x.get("activityName", "")).lower(),
            x.get("averagePower") is None and x.get("averageHR") is None,
        ),
    )

    for act in sorted_raw:
        act_type = act.get("activityType", {}).get("typeKey", "")
        if "cycling" not in act_type and "biking" not in act_type:
            continue

        act_id = int(act["activityId"])
        start_local = act.get("startTimeLocal", "")
        duration_sec = act.get("duration") or 0
        moving_sec = act.get("movingDuration") or duration_sec
        avg_power = act.get("averagePower")
        np_watts = act.get("normPower")
        tss = act.get("trainingStressScore")
        if_val = act.get("intensityFactor")

        if pd.isna(if_val) and np_watts and FTP_WATTS > 0:
            if_val = round(float(np_watts) / FTP_WATTS, 4)
        if pd.isna(tss) and np_watts and duration_sec > 0 and FTP_WATTS > 0 and pd.notna(if_val):
            tss = round((duration_sec * float(np_watts) * float(if_val)) / (FTP_WATTS * 3600) * 100, 2)

        avg_speed_mps = act.get("averageSpeed")
        max_speed_mps = act.get("maxSpeed")

        row = {
            "activity_id": act_id,
            "date": start_local,
            "name": act.get("activityName", ""),
            "type": act_type,
            "distance_km": round((act.get("distance") or 0) / 1000, 2),
            "duration_min": round(duration_sec / 60, 1),
            "moving_duration_min": round(moving_sec / 60, 1),
            "avg_speed_kph": round(float(avg_speed_mps) * 3.6, 2) if avg_speed_mps else None,
            "max_speed_kph": round(float(max_speed_mps) * 3.6, 2) if max_speed_mps else None,
            "elevation_gain_m": round(act.get("elevationGain") or 0, 1) if act.get("elevationGain") is not None else None,
            "elevation_loss_m": round(act.get("elevationLoss") or 0, 1) if act.get("elevationLoss") is not None else None,
            "calories_kcal": act.get("calories"),
            "avg_power": avg_power,
            "norm_power_NP": np_watts,
            "max_power": act.get("maxPower"),
            "max_20min_power": act.get("max20MinPower"),
            "training_stress_score_TSS": tss,
            "intensity_factor_IF": if_val,
            "avg_hr": act.get("averageHR"),
            "max_hr": act.get("maxHR"),
            "avg_cadence": act.get("averageBikingCadenceInRevPerMinute"),
            "max_cadence": act.get("maxBikingCadenceInRevPerMinute"),
            "left_right_balance": act.get("leftRightBalance"),
            "aerobic_training_effect": act.get("aerobicTrainingEffect"),
            "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
            "activity_training_load": act.get("activityTrainingLoad"),
            "moderate_intensity_min": act.get("moderateIntensityMinutes"),
            "vigorous_intensity_min": act.get("vigorousIntensityMinutes"),
            "vo2_max": act.get("vO2MaxValue"),
        }

        if is_myfitnesspal_duplicate(row, cleaned_rows):
            print(f"[INFO] MyFitnessPal 중복 활동 제외: {act_id} ({row['name']})", flush=True)
            continue

        cleaned_rows.append(row)
        download_fit_file(client, act_id, start_local)

    new_df = pd.DataFrame(cleaned_rows)
    if not existing_df.empty and not new_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["activity_id"], keep="last")
    elif not new_df.empty:
        combined = new_df
    else:
        combined = existing_df

    if not combined.empty:
        combined = combined[~combined["name"].astype(str).str.contains("MyFitnessPal", case=False, na=False)]
        combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
        combined.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return combined


def fetch_garmin_health(client: Garmin, days: int = WELLNESS_DAYS) -> pd.DataFrame:
    """
    가민 웰니스 수집 (체성분 제외):
    - 심박/스트레스/바디배터리/수면 단계/호흡수/SpO2/트레이닝 레디니스·상태·단기만성부하·ACWR
    """
    csv_path = DATA_DIR / "garmin_health.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    today = datetime.now().date()

    for offset in range(days):
        d_str = (today - timedelta(days=offset)).isoformat()

        resting_hr = min_hr = max_hr = None
        avg_stress = max_stress = rest_stress = None
        bb_high = bb_low = bb_charged = bb_drained = None
        hrv_avg = hrv_weekly_avg = hrv_status = hrv_base_low = hrv_base_high = None
        sleep_score = sleep_hours = deep_sleep_min = rem_sleep_min = light_sleep_min = awake_sleep_min = None
        avg_respiration = avg_spo2 = None
        steps = active_kcal = total_kcal = None
        readiness_score = readiness_level = training_status = None
        acute_load = chronic_load = acwr = None

        stats = safe_call(client.get_stats, d_str) or {}
        if isinstance(stats, dict):
            resting_hr = stats.get("restingHeartRate")
            min_hr = stats.get("minHeartRate")
            max_hr = stats.get("maxHeartRate")
            avg_stress = stats.get("averageStressLevel")
            max_stress = stats.get("maxStressLevel")
            rest_stress = stats.get("restStressDuration")
            bb_high = stats.get("bodyBatteryHighestValue")
            bb_low = stats.get("bodyBatteryLowestValue")
            bb_charged = stats.get("bodyBatteryChargedValue")
            bb_drained = stats.get("bodyBatteryDrainedValue")
            steps = stats.get("totalSteps")
            active_kcal = stats.get("activeKilocalories")
            total_kcal = stats.get("totalKilocalories")

        if resting_hr is None:
            rhr_data = safe_call(client.get_rhr_day, d_str) or {}
            metrics_map = rhr_data.get("allMetrics", {}).get("metricsMap", {}) if isinstance(rhr_data, dict) else {}
            rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
            if rhr_list:
                resting_hr = rhr_list[0].get("value")

        if resting_hr is None:
            hr_data = safe_call(client.get_heart_rates, d_str) or {}
            if isinstance(hr_data, dict):
                resting_hr = hr_data.get("restingHeartRate")
                min_hr = min_hr if min_hr is not None else hr_data.get("minHeartRate")
                max_hr = max_hr if max_hr is not None else hr_data.get("maxHeartRate")

        if bb_high is None or bb_low is None:
            bb_list = safe_call(client.get_body_battery, d_str, d_str) or []
            if isinstance(bb_list, list) and bb_list:
                bb_vals = [
                    v[1]
                    for entry in bb_list
                    for v in (entry.get("bodyBatteryValuesArray") or [])
                    if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] is not None
                ]
                if bb_vals:
                    bb_high = max(bb_vals)
                    bb_low = min(bb_vals)

        if avg_stress is None:
            stress_data = safe_call(client.get_stress_data, d_str) or {}
            if isinstance(stress_data, dict):
                avg_stress = stress_data.get("avgStressLevel")
                max_stress = max_stress if max_stress is not None else stress_data.get("maxStressLevel")

        hrv_data = safe_call(client.get_hrv_data, d_str) or {}
        if isinstance(hrv_data, dict):
            hrv_summary = hrv_data.get("hrvSummary") or {}
            hrv_avg = hrv_summary.get("lastNightAvg")
            hrv_weekly_avg = hrv_summary.get("weeklyAvg")
            hrv_status = hrv_summary.get("status")
            baseline = hrv_summary.get("baseline") or {}
            hrv_base_low = baseline.get("balancedLow")
            hrv_base_high = baseline.get("balancedUpper")

        sleep_data = safe_call(client.get_sleep_data, d_str) or {}
        if isinstance(sleep_data, dict):
            daily_sleep = sleep_data.get("dailySleepDTO") or {}
            sleep_sec = daily_sleep.get("sleepTimeSeconds")
            if sleep_sec:
                sleep_hours = round(float(sleep_sec) / 3600, 2)
            for key_sec, target_attr in [
                ("deepSleepSeconds", "deep"),
                ("remSleepSeconds", "rem"),
                ("lightSleepSeconds", "light"),
                ("awakeSleepSeconds", "awake"),
            ]:
                val_sec = daily_sleep.get(key_sec)
                if val_sec is not None:
                    mins = round(float(val_sec) / 60, 1)
                    if target_attr == "deep":
                        deep_sleep_min = mins
                    elif target_attr == "rem":
                        rem_sleep_min = mins
                    elif target_attr == "light":
                        light_sleep_min = mins
                    elif target_attr == "awake":
                        awake_sleep_min = mins
            avg_respiration = daily_sleep.get("averageRespirationValue")
            avg_spo2 = daily_sleep.get("averageSpO2Value")
            sleep_scores = daily_sleep.get("sleepScores") or {}
            overall = sleep_scores.get("overall") or {}
            sleep_score = overall.get("value") or daily_sleep.get("sleepScore")

        readiness_data = safe_call(client.get_training_readiness, d_str)
        if isinstance(readiness_data, list) and readiness_data:
            r0 = readiness_data[0] if isinstance(readiness_data[0], dict) else {}
            readiness_score = r0.get("score")
            readiness_level = r0.get("level")
        elif isinstance(readiness_data, dict):
            readiness_score = readiness_data.get("score")
            readiness_level = readiness_data.get("level")

        ts_data = safe_call(client.get_training_status, d_str) or {}
        if isinstance(ts_data, dict):
            training_status = ts_data.get("trainingStatusFeedbackPhrase") or ts_data.get("trainingStatus")
            load_dto = ts_data.get("mostRecentTrainingLoadDTO") or {}
            if isinstance(load_dto, dict):
                acute_load = load_dto.get("dailyTrainingLoadAcute")
                chronic_load = load_dto.get("dailyTrainingLoadChronic")
                acwr = load_dto.get("dailyAcuteChronicWorkloadRatio")

        rows.append(
            {
                "date": d_str,
                "resting_hr": resting_hr,
                "min_hr": min_hr,
                "max_hr": max_hr,
                "avg_stress": avg_stress,
                "max_stress": max_stress,
                "rest_stress_sec": rest_stress,
                "body_battery_high": bb_high,
                "body_battery_low": bb_low,
                "body_battery_charged": bb_charged,
                "body_battery_drained": bb_drained,
                "hrv_last_night_avg": hrv_avg,
                "hrv_weekly_avg": hrv_weekly_avg,
                "hrv_status": hrv_status,
                "hrv_baseline_low": hrv_base_low,
                "hrv_baseline_high": hrv_base_high,
                "sleep_score": sleep_score,
                "sleep_hours": sleep_hours,
                "deep_sleep_min": deep_sleep_min,
                "rem_sleep_min": rem_sleep_min,
                "light_sleep_min": light_sleep_min,
                "awake_sleep_min": awake_sleep_min,
                "avg_respiration": avg_respiration,
                "avg_spo2": avg_spo2,
                "training_readiness_score": readiness_score,
                "training_readiness_level": readiness_level,
                "training_status": training_status,
                "acute_load": acute_load,
                "chronic_load": chronic_load,
                "acwr": acwr,
                "steps": steps,
                "active_kcal": active_kcal,
                "total_kcal": total_kcal,
            }
        )
        time.sleep(0.12)

    new_df = pd.DataFrame(rows)
    if not existing_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined = combined.sort_values("date").groupby("date", as_index=False).last()
    else:
        combined = new_df

    combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
    combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return combined


def rolling_max_1s(arr: np.ndarray, window_sec: int) -> Optional[float]:
    """1초 간격 배열에서 지정 초(window_sec) 구간 롤링 최대 평균값 계산"""
    if len(arr) < window_sec or window_sec <= 0:
        return None
    kernel = np.ones(window_sec, dtype=float) / float(window_sec)
    conv = np.convolve(arr, kernel, mode="valid")
    if len(conv) == 0:
        return None
    return round(float(np.max(conv)), 1)


def resample_to_1s(df: pd.DataFrame) -> pd.DataFrame:
    """
    .fit record들을 타임스탬프 기준 1초 그리드로 정렬/보간:
    - power, heart_rate, cadence를 1초 단위로 정렬
    - timestamps가 없더라도 초당 1레코드로 취급
    """
    if "timestamp" in df.columns and df["timestamp"].notna().any():
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        valid = ts.notna()
        if valid.sum() >= 2:
            sub = df.loc[valid].copy()
            sub["_ts"] = ts.loc[valid]
            sub = sub.sort_values("_ts").drop_duplicates(subset=["_ts"], keep="last").set_index("_ts")
            full_idx = pd.date_range(sub.index.min(), sub.index.max(), freq="1s")
            # 너무 긴 휴지/비정상 타임스탬프 폭주 방지 (최대 12시간)
            if len(full_idx) <= 43200:
                reindexed = sub.reindex(full_idx)
                for col in ("power", "heart_rate", "cadence", "left_right_balance", "left_pedal_smoothness", "right_pedal_smoothness", "left_torque_effectiveness", "right_torque_effectiveness"):
                    if col in reindexed.columns:
                        reindexed[col] = pd.to_numeric(reindexed[col], errors="coerce")
                return reindexed.reset_index().rename(columns={"index": "timestamp"})

    out = df.copy()
    for col in ("power", "heart_rate", "cadence", "left_right_balance", "left_pedal_smoothness", "right_pedal_smoothness", "left_torque_effectiveness", "right_torque_effectiveness"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def parse_fit_1s_extended(fit_path: Path) -> Optional[Dict[str, Any]]:
    """
    .fit 파일을 1초 단위로 세분화 분석:
    - 파워(1s): 평균, 표준편차, 최대(1s), 0W 코스팅 비율, 1초 롤링 MMP (5s/10s/30s/1m/2m/5m/10m/20m/60m), 추정 FTP(20m * 0.95)
    - 케이던스(1s): 평균, 표준편차, 최대(1s), 페달링(cadence > 0) 비율, 페달링 중 평균 케이던스
    - 심박(1s): 평균, 표준편차, 최소/최대(1s), 1초 기준 전반/후반 효율(EF_1s) 및 유산소 디커플링
    - 파워 존(Z1~Z5) 및 심박 존(HR_Z1~HR_Z5) 1초 기준 체류 시간(분)
    - 선택 시 1초 파워/케이던스/심박 시계열을 .json.gz로 저장
    """
    try:
        from fitparse import FitFile
    except ImportError:
        return None

    try:
        fitfile = FitFile(str(fit_path))
        records: List[Dict[str, Any]] = []
        for rec in fitfile.get_messages("record"):
            vals = {d.name: d.value for d in rec if d.value is not None}
            if "power" in vals or "heart_rate" in vals or "cadence" in vals:
                records.append(vals)

        if not records:
            return None

        raw_df = pd.DataFrame(records)
        df_1s = resample_to_1s(raw_df)
        samples_1s = int(len(df_1s))
        duration_min = round(samples_1s / 60.0, 2)

        p_series = df_1s["power"] if "power" in df_1s else pd.Series(dtype=float)
        hr_series = df_1s["heart_rate"] if "heart_rate" in df_1s else pd.Series(dtype=float)
        cad_series = df_1s["cadence"] if "cadence" in df_1s else pd.Series(dtype=float)

        # 파워 1초 분석 (결측은 0W로 코스팅 반영 시 활용, 평균/MMP는 유효 샘플 기준)
        p_valid = p_series.dropna().to_numpy(dtype=float)
        p_zero_fill = p_series.fillna(0.0).to_numpy(dtype=float)

        avg_power_1s = round(float(np.mean(p_valid)), 1) if len(p_valid) else None
        std_power_1s = round(float(np.std(p_valid)), 1) if len(p_valid) else None
        max_power_1s = round(float(np.max(p_valid)), 1) if len(p_valid) else None
        zero_power_pct_1s = round(float(np.mean(p_zero_fill <= 0.0) * 100.0), 1) if len(p_zero_fill) else None
        work_kj_1s = round(float(np.sum(p_zero_fill)) / 1000.0, 1) if len(p_zero_fill) else None

        np_watts_1s = None
        if len(p_zero_fill) >= 30:
            p30 = np.convolve(p_zero_fill, np.ones(30) / 30.0, mode="valid")
            np_watts_1s = round(float(np.mean(p30 ** 4) ** 0.25), 1)
        elif len(p_valid) > 0:
            np_watts_1s = avg_power_1s

        vi_1s = round(float(np_watts_1s) / float(avg_power_1s), 3) if (np_watts_1s and avg_power_1s and avg_power_1s > 0) else None

        mmp_5s = rolling_max_1s(p_zero_fill, 5)
        mmp_10s = rolling_max_1s(p_zero_fill, 10)
        mmp_30s = rolling_max_1s(p_zero_fill, 30)
        mmp_1m = rolling_max_1s(p_zero_fill, 60)
        mmp_2m = rolling_max_1s(p_zero_fill, 120)
        mmp_5m = rolling_max_1s(p_zero_fill, 300)
        mmp_10m = rolling_max_1s(p_zero_fill, 600)
        mmp_20m = rolling_max_1s(p_zero_fill, 1200)
        mmp_60m = rolling_max_1s(p_zero_fill, 3600)
        est_ftp_20m = round(mmp_20m * 0.95, 1) if mmp_20m else None

        # 케이던스 1초 분석
        cad_valid = cad_series.dropna().to_numpy(dtype=float)
        avg_cad_1s = round(float(np.mean(cad_valid)), 1) if len(cad_valid) else None
        std_cad_1s = round(float(np.std(cad_valid)), 1) if len(cad_valid) else None
        max_cad_1s = round(float(np.max(cad_valid)), 1) if len(cad_valid) else None
        pedaling_pos = cad_valid[cad_valid > 0] if len(cad_valid) else np.array([])
        pedaling_cad_1s = round(float(np.mean(pedaling_pos)), 1) if len(pedaling_pos) else None
        pedaling_ratio_pct_1s = round(float(len(pedaling_pos) / len(cad_valid) * 100.0), 1) if len(cad_valid) else None

        # 심박 1초 분석
        hr_valid = hr_series.dropna().to_numpy(dtype=float)
        avg_hr_1s = round(float(np.mean(hr_valid)), 1) if len(hr_valid) else None
        std_hr_1s = round(float(np.std(hr_valid)), 1) if len(hr_valid) else None
        min_hr_1s = round(float(np.min(hr_valid)), 1) if len(hr_valid) else None
        max_hr_1s = round(float(np.max(hr_valid)), 1) if len(hr_valid) else None

        ef_1s = round(float(np_watts_1s) / float(avg_hr_1s), 3) if (np_watts_1s and avg_hr_1s and avg_hr_1s > 0) else None

        # 1초 정렬 기준 전반/후반 효율 & 유산소 디커플링
        ef_1st = ef_2nd = ef_delta_pct = decoupling_pct = None
        if "power" in df_1s and "heart_rate" in df_1s:
            paired = pd.DataFrame({
                "p": df_1s["power"].fillna(0.0),
                "hr": df_1s["heart_rate"],
            }).dropna()
            if len(paired) >= 120:
                mid = len(paired) // 2
                p1, h1 = paired["p"].iloc[:mid].to_numpy(), paired["hr"].iloc[:mid].to_numpy()
                p2, h2 = paired["p"].iloc[mid:].to_numpy(), paired["hr"].iloc[mid:].to_numpy()
                m1, m2 = np.mean(p1), np.mean(p2)
                hr1, hr2 = np.mean(h1), np.mean(h2)
                if m1 > 0 and hr1 > 0 and m2 > 0 and hr2 > 0:
                    ef_1st = round(float(m1 / hr1), 3)
                    ef_2nd = round(float(m2 / hr2), 3)
                    ef_delta_pct = round(float((ef_2nd - ef_1st) / ef_1st * 100.0), 2)
                    decoupling_pct = round(float(((m1 / hr1) - (m2 / hr2)) / (m1 / hr1) * 100.0), 2)

        # 좌/우 밸런스
        left_bal = right_bal = None
        if "left_right_balance" in df_1s:
            lrb = df_1s["left_right_balance"].dropna()
            if len(lrb) > 0:
                lv = float(np.mean(lrb))
                if lv > 100:
                    right_bal = round(lv - 128.0, 1)
                    left_bal = round(100.0 - right_bal, 1)
                else:
                    left_bal = round(lv, 1)
                    right_bal = round(100.0 - left_bal, 1)

        # 파워 존(1초 샘플 기준 분)
        z1_min = z2_min = z3_min = z4_min = z5_min = 0.0
        if len(p_zero_fill) > 0 and FTP_WATTS > 0:
            pct_ftp = p_zero_fill / FTP_WATTS
            z1_min = round(float(np.sum(pct_ftp < 0.56)) / 60.0, 1)
            z2_min = round(float(np.sum((pct_ftp >= 0.56) & (pct_ftp < 0.76))) / 60.0, 1)
            z3_min = round(float(np.sum((pct_ftp >= 0.76) & (pct_ftp < 0.91))) / 60.0, 1)
            z4_min = round(float(np.sum((pct_ftp >= 0.91) & (pct_ftp < 1.06))) / 60.0, 1)
            z5_min = round(float(np.sum(pct_ftp >= 1.06)) / 60.0, 1)

        # 심박 존(1초 샘플 기준 분)
        hr_z1_min = hr_z2_min = hr_z3_min = hr_z4_min = hr_z5_min = 0.0
        if len(hr_valid) > 0 and MAX_HR > 0:
            pct_hr = hr_valid / MAX_HR
            hr_z1_min = round(float(np.sum(pct_hr < 0.60)) / 60.0, 1)
            hr_z2_min = round(float(np.sum((pct_hr >= 0.60) & (pct_hr < 0.70))) / 60.0, 1)
            hr_z3_min = round(float(np.sum((pct_hr >= 0.70) & (pct_hr < 0.80))) / 60.0, 1)
            hr_z4_min = round(float(np.sum((pct_hr >= 0.80) & (pct_hr < 0.90))) / 60.0, 1)
            hr_z5_min = round(float(np.sum(pct_hr >= 0.90)) / 60.0, 1)

        stream_gz_file: Optional[str] = None
        if SAVE_1S_STREAM_GZ:
            STREAM_1S_DIR.mkdir(parents=True, exist_ok=True)
            gz_path = STREAM_1S_DIR / f"{fit_path.stem}_1s.json.gz"
            payload = {
                "file": fit_path.name,
                "sec_idx": list(range(samples_1s)),
                "power_1s": [None if np.isnan(x) else round(float(x), 1) for x in p_series.to_numpy(dtype=float)],
                "cadence_1s": [None if np.isnan(x) else round(float(x), 1) for x in cad_series.to_numpy(dtype=float)],
                "hr_1s": [None if np.isnan(x) else round(float(x), 1) for x in hr_series.to_numpy(dtype=float)],
            }
            with gzip.open(gz_path, "wt", encoding="utf-8") as gf:
                json.dump(payload, gf, separators=(",", ":"))
            stream_gz_file = gz_path.name

        return {
            "file": fit_path.name,
            "samples_1s": samples_1s,
            "duration_min": duration_min,
            "avg_power_1s": avg_power_1s,
            "std_power_1s": std_power_1s,
            "max_power_1s": max_power_1s,
            "NP_watts_1s": np_watts_1s,
            "VI_1s": vi_1s,
            "work_kj_1s": work_kj_1s,
            "zero_power_pct_1s": zero_power_pct_1s,
            "MMP_5s": mmp_5s,
            "MMP_10s": mmp_10s,
            "MMP_30s": mmp_30s,
            "MMP_1m": mmp_1m,
            "MMP_2m": mmp_2m,
            "MMP_5m": mmp_5m,
            "MMP_10m": mmp_10m,
            "MMP_20m": mmp_20m,
            "MMP_60m": mmp_60m,
            "est_FTP_from_20m": est_ftp_20m,
            "avg_cadence_1s": avg_cad_1s,
            "std_cadence_1s": std_cad_1s,
            "max_cadence_1s": max_cad_1s,
            "pedaling_cadence_1s": pedaling_cad_1s,
            "pedaling_ratio_pct_1s": pedaling_ratio_pct_1s,
            "avg_hr_1s": avg_hr_1s,
            "std_hr_1s": std_hr_1s,
            "min_hr_1s": min_hr_1s,
            "max_hr_1s": max_hr_1s,
            "EF_1s": ef_1s,
            "EF_1st_half": ef_1st,
            "EF_2nd_half": ef_2nd,
            "EF_delta_pct": ef_delta_pct,
            "aerobic_decoupling_pct": decoupling_pct,
            "left_balance_pct": left_bal,
            "right_balance_pct": right_bal,
            "Z1_recovery_min": z1_min,
            "Z2_endurance_min": z2_min,
            "Z3_tempo_min": z3_min,
            "Z4_threshold_min": z4_min,
            "Z5_vo2max_min": z5_min,
            "HR_Z1_min": hr_z1_min,
            "HR_Z2_min": hr_z2_min,
            "HR_Z3_min": hr_z3_min,
            "HR_Z4_min": hr_z4_min,
            "HR_Z5_min": hr_z5_min,
            "stream_1s_gz": stream_gz_file,
        }
    except Exception as e:
        print(f"[WARN] FIT 1초 파싱 실패 ({fit_path.name}): {e}", flush=True)
        return None


def sync_fit_extended() -> pd.DataFrame:
    """fit_files 폴더 내 누적 .fit 파일을 1초 단위로 파싱하여 garmin_fit_extended.csv로 저장"""
    out_path = DATA_DIR / "garmin_fit_extended.csv"
    existing_df = pd.read_csv(out_path) if out_path.exists() else pd.DataFrame()
    existing_files = (
        set(existing_df["file"].astype(str))
        if not existing_df.empty and "file" in existing_df.columns
        else set()
    )

    fit_files = sorted(FIT_DIR.glob("*.fit"), reverse=True)
    new_rows: List[Dict[str, Any]] = []

    for fp in fit_files:
        if fp.name in existing_files:
            continue
        parsed = parse_fit_1s_extended(fp)
        if parsed:
            new_rows.append(parsed)

    if not existing_df.empty and new_rows:
        combined = pd.concat([existing_df, pd.DataFrame(new_rows)], ignore_index=True)
        combined = combined.drop_duplicates(subset=["file"], keep="last")
    elif new_rows:
        combined = pd.DataFrame(new_rows)
    else:
        combined = existing_df

    if not combined.empty and "file" in combined.columns:
        combined = combined.sort_values("file", ascending=False).reset_index(drop=True)
        combined.to_csv(out_path, index=False, encoding="utf-8-sig")

    return combined


def summarize_strava_streams_1s(streams_json: Dict[str, Any]) -> Dict[str, Any]:
    """Strava 1초 해상도 스트림(watts, heartrate, cadence, grade_smooth, moving) 요약"""
    def arr(key: str) -> Optional[np.ndarray]:
        entry = streams_json.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("data"), list):
            return np.asarray(entry["data"], dtype=float)
        return None

    watts = arr("watts")
    hrs = arr("heartrate")
    cads = arr("cadence")
    grades = arr("grade_smooth")
    moving = arr("moving")

    climb_avg_watts = flat_avg_watts = coasting_ratio_pct = None
    if watts is not None and len(watts) > 0:
        if moving is not None and len(moving) == len(watts):
            mov_mask = moving > 0.5
            if np.sum(mov_mask) > 0:
                coasting_ratio_pct = round(
                    float(np.sum(watts[mov_mask] <= 0) / np.sum(mov_mask) * 100.0), 1
                )
        if grades is not None and len(grades) == len(watts):
            climb_mask = grades >= 3.0
            flat_mask = (grades > -3.0) & (grades < 3.0)
            if np.sum(climb_mask) >= 10:
                climb_avg_watts = round(float(np.mean(watts[climb_mask])), 1)
            if np.sum(flat_mask) >= 10:
                flat_avg_watts = round(float(np.mean(watts[flat_mask])), 1)

    return {
        "stream_1s_samples": int(len(watts)) if watts is not None else 0,
        "stream_1s_avg_watts": round(float(np.mean(watts)), 1) if (watts is not None and len(watts) > 0) else None,
        "stream_1s_std_watts": round(float(np.std(watts)), 1) if (watts is not None and len(watts) > 0) else None,
        "stream_1s_max_watts": round(float(np.max(watts)), 1) if (watts is not None and len(watts) > 0) else None,
        "stream_climb_avg_watts": climb_avg_watts,
        "stream_flat_avg_watts": flat_avg_watts,
        "stream_coasting_ratio_pct": coasting_ratio_pct,
        "stream_1s_avg_hr": round(float(np.mean(hrs)), 1) if (hrs is not None and len(hrs) > 0) else None,
        "stream_1s_std_hr": round(float(np.std(hrs)), 1) if (hrs is not None and len(hrs) > 0) else None,
        "stream_1s_max_hr": round(float(np.max(hrs)), 1) if (hrs is not None and len(hrs) > 0) else None,
        "stream_1s_avg_cadence": round(float(np.mean(cads)), 1) if (cads is not None and len(cads) > 0) else None,
        "stream_1s_std_cadence": round(float(np.std(cads)), 1) if (cads is not None and len(cads) > 0) else None,
        "stream_1s_max_cadence": round(float(np.max(cads)), 1) if (cads is not None and len(cads) > 0) else None,
    }


def fetch_strava_activities(
    per_page: int = 100,
    detail_limit: int = STRAVA_DETAIL_LIMIT,
    stream_limit: int = STRAVA_STREAM_LIMIT,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Strava 활동 목록 + 상세 + 1초 스트림 요약 수집"""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
        print("[INFO] Strava 환경변수가 설정되지 않아 Strava 수집을 건너뜁니다.", flush=True)
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    csv_path = DATA_DIR / "strava_activities.csv"
    detail_csv_path = DATA_DIR / "strava_activity_details.csv"
    streams_csv_path = DATA_DIR / "strava_streams_summary.csv"

    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()
    existing_detail_df = pd.read_csv(detail_csv_path) if detail_csv_path.exists() else pd.DataFrame()
    existing_streams_df = pd.read_csv(streams_csv_path) if streams_csv_path.exists() else pd.DataFrame()

    token_resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": STRAVA_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    act_resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=headers,
        params={"per_page": per_page, "page": 1},
        timeout=20,
    )
    act_resp.raise_for_status()
    items = act_resp.json() or []

    rows: List[Dict[str, Any]] = []
    ride_ids: List[int] = []

    for it in items:
        if it.get("type") not in ("Ride", "VirtualRide"):
            continue
        act_id = int(it["id"])
        ride_ids.append(act_id)
        rows.append(
            {
                "id": act_id,
                "date": it.get("start_date_local"),
                "name": it.get("name"),
                "type": it.get("type"),
                "trainer": it.get("trainer"),
                "distance_km": round((it.get("distance") or 0) / 1000, 2),
                "moving_time_min": round((it.get("moving_time") or 0) / 60, 1),
                "elevation_gain_m": round(it.get("total_elevation_gain") or 0, 1),
                "avg_speed_kph": round((it.get("average_speed") or 0) * 3.6, 1),
                "max_speed_kph": round((it.get("max_speed") or 0) * 3.6, 1),
                "avg_watts": it.get("average_watts"),
                "weighted_avg_watts_NP": it.get("weighted_average_watts"),
                "max_watts": it.get("max_watts"),
                "kilojoules": it.get("kilojoules"),
                "calories_kcal": it.get("calories"),
                "avg_cadence": it.get("average_cadence"),
                "avg_hr": it.get("average_heartrate"),
                "max_hr": it.get("max_heartrate"),
                "suffer_score": it.get("suffer_score"),
            }
        )

    new_df = pd.DataFrame(rows)
    if not existing_df.empty and not new_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["id"], keep="last")
    else:
        combined = new_df if not new_df.empty else existing_df

    if not combined.empty:
        combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
        combined.to_csv(csv_path, index=False, encoding="utf-8-sig")

    already_detailed = (
        set(existing_detail_df["id"].astype(int))
        if not existing_detail_df.empty and "id" in existing_detail_df.columns
        else set()
    )
    target_detail_ids = [aid for aid in ride_ids[:detail_limit] if aid not in already_detailed]
    detail_rows: List[Dict[str, Any]] = []

    for aid in target_detail_ids:
        try:
            d_resp = requests.get(
                f"https://www.strava.com/api/v3/activities/{aid}",
                headers=headers,
                timeout=20,
            )
            if not d_resp.ok:
                continue
            d_json = d_resp.json() or {}
            laps = d_json.get("laps") or []
            segs = d_json.get("segment_efforts") or []
            lap_powers = [l.get("average_watts") for l in laps if l.get("average_watts") is not None]
            detail_rows.append(
                {
                    "id": aid,
                    "name": d_json.get("name"),
                    "start_date_local": d_json.get("start_date_local"),
                    "lap_count": len(laps),
                    "max_lap_avg_watts": max(lap_powers) if lap_powers else None,
                    "segment_efforts_count": len(segs),
                    "pr_segments_count": sum(1 for s in segs if s.get("pr_rank") == 1),
                }
            )
            time.sleep(0.15)
        except Exception:
            continue

    if not existing_detail_df.empty and detail_rows:
        detail_combined = pd.concat([existing_detail_df, pd.DataFrame(detail_rows)], ignore_index=True)
        detail_combined = detail_combined.drop_duplicates(subset=["id"], keep="last")
    elif detail_rows:
        detail_combined = pd.DataFrame(detail_rows)
    else:
        detail_combined = existing_detail_df

    if not detail_combined.empty:
        detail_combined = detail_combined.sort_values("start_date_local", ascending=False).reset_index(drop=True)
        detail_combined.to_csv(detail_csv_path, index=False, encoding="utf-8-sig")

    already_streamed = (
        set(existing_streams_df["id"].astype(int))
        if not existing_streams_df.empty and "id" in existing_streams_df.columns
        else set()
    )
    target_stream_ids = [aid for aid in ride_ids[:stream_limit] if aid not in already_streamed]
    stream_rows: List[Dict[str, Any]] = []
    stream_keys = "time,distance,altitude,velocity_smooth,heartrate,cadence,watts,moving,grade_smooth"

    for aid in target_stream_ids:
        try:
            s_resp = requests.get(
                f"https://www.strava.com/api/v3/activities/{aid}/streams",
                headers=headers,
                params={"keys": stream_keys, "key_by_type": "true"},
                timeout=20,
            )
            if not s_resp.ok:
                continue
            s_json = s_resp.json() or {}
            summary = summarize_strava_streams_1s(s_json)
            summary["id"] = aid
            stream_rows.append(summary)
            time.sleep(0.15)
        except Exception:
            continue

    if not existing_streams_df.empty and stream_rows:
        streams_combined = pd.concat([existing_streams_df, pd.DataFrame(stream_rows)], ignore_index=True)
        streams_combined = streams_combined.drop_duplicates(subset=["id"], keep="last")
    elif stream_rows:
        streams_combined = pd.DataFrame(stream_rows)
    else:
        streams_combined = existing_streams_df

    if not streams_combined.empty:
        streams_combined.to_csv(streams_csv_path, index=False, encoding="utf-8-sig")

    return combined, detail_combined, streams_combined


def main() -> None:
    print("=== Fitness 데이터 수집 시작 (체성분 제외 / 1초 스트림 분석) ===", flush=True)
    garmin_client = init_garmin()

    act_df = fetch_garmin_activities(garmin_client, limit=ACTIVITY_LIMIT)
    fit_count = len(list(FIT_DIR.glob("*.fit")))
    print(f"[OK] Garmin 활동: {len(act_df)}건 / 누적 FIT 파일: {fit_count}개", flush=True)

    fit_ext_df = sync_fit_extended()
    print(f"[OK] FIT 1초 정밀 분석: {len(fit_ext_df)}건 저장 완료", flush=True)

    health_df = fetch_garmin_health(garmin_client, days=WELLNESS_DAYS)
    print(f"[OK] Garmin 웰니스(체성분 제외): {len(health_df)}일치 저장 완료", flush=True)

    strava_df, strava_detail_df, strava_streams_df = fetch_strava_activities(
        per_page=100,
        detail_limit=STRAVA_DETAIL_LIMIT,
        stream_limit=STRAVA_STREAM_LIMIT,
    )
    if not strava_df.empty:
        print(
            f"[OK] Strava 활동: {len(strava_df)}건 / 상세: {len(strava_detail_df)}건 / 1초스트림요약: {len(strava_streams_df)}건 저장 완료",
            flush=True,
        )

    print("=== 수집 완료 ===", flush=True)


if __name__ == "__main__":
    main()
