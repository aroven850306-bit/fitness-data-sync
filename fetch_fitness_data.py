#!/usr/bin/env python3
"""
Garmin Connect + Strava 데이터 수집 스크립트
- garminconnect 0.3.x+ (GCDC_0... 토큰 문자열 / GARMINTOKENS 환경변수 / tokenstore) 완벽 지원
- GARMIN_API_KEY (GCDC_0... 네이티브 토큰 / Base64 / JSON / 이메일:비밀번호) 및 GARMIN_EMAIL/PASSWORD 지원
- Garmin 활동 전체 누적 수집 및 MyFitnessPal 자동 중복 제거
- 가민 웰니스 확장: HRV 기준 범위(baseline), 수면 단계/점수, 안정시 심박·스트레스·바디배터리 대체 엔드포인트 보완
- 활동별 원본 .fit 파일 자동 다운로드 (이미 존재하는 파일은 건너뛰어 누적 유지)
"""

import base64
import inspect
import io
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = DATA_DIR / "fit_files"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIT_DIR.mkdir(parents=True, exist_ok=True)

GARMIN_TOKEN_DIR = os.getenv("GARMIN_TOKEN_DIR", str(DATA_DIR / ".garmin_tokens"))

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "").strip()
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()

FTP_WATTS = float(os.getenv("FTP_WATTS", "195"))
WELLNESS_DAYS = int(os.getenv("WELLNESS_DAYS", "30"))
ACTIVITY_LIMIT = int(os.getenv("ACTIVITY_LIMIT", "100"))


def decode_garmin_api_key(raw_key: str) -> Tuple[str, str, Optional[str], Optional[str]]:
    """
    GARMIN_API_KEY에서 인증 수단 추출:
    반환값: (email, password, token_dir_path, raw_token_str)
    """
    raw_key = raw_key.strip()
    if not raw_key:
        return "", "", None, None

    if raw_key.startswith("GCDC_"):
        return "", "", None, raw_key

    candidates = [raw_key]
    try:
        padded = raw_key + "=" * ((4 - len(raw_key) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
        if decoded:
            candidates.append(decoded)
    except Exception:
        pass

    for cand in candidates:
        if cand.startswith("GCDC_"):
            return "", "", None, cand

        if cand.startswith("{"):
            try:
                data = json.loads(cand)
                if "oauth1_token" in data or "oauth2_token" in data:
                    tmp_dir = Path(tempfile.mkdtemp(prefix="garmin_tokens_"))
                    if "oauth1_token" in data:
                        (tmp_dir / "oauth1_token.json").write_text(
                            json.dumps(data["oauth1_token"]), encoding="utf-8"
                        )
                    if "oauth2_token" in data:
                        (tmp_dir / "oauth2_token.json").write_text(
                            json.dumps(data["oauth2_token"]), encoding="utf-8"
                        )
                    return "", "", str(tmp_dir), None

                if "access_token" in data and "refresh_token" in data:
                    tmp_dir = Path(tempfile.mkdtemp(prefix="garmin_tokens_"))
                    (tmp_dir / "oauth2_token.json").write_text(
                        json.dumps(data), encoding="utf-8"
                    )
                    return "", "", str(tmp_dir), None

                u = str(data.get("email") or data.get("username") or data.get("user") or "").strip()
                p = str(data.get("password") or data.get("pass") or data.get("pwd") or "").strip()
                if u and p:
                    return u, p, None, None
            except Exception:
                pass

        for sep in ("\n", "|", ":"):
            if sep in cand and not cand.startswith("{"):
                parts = [x.strip() for x in cand.split(sep, 1)]
                if len(parts) == 2 and "@" in parts[0] and parts[1]:
                    return parts[0], parts[1], None, None

    return "", "", None, raw_key


def init_garmin() -> Garmin:
    """
    garminconnect 0.3.x+ (`GCDC_0...` 토큰 형식) 및 0.2.x 모두 지원하는 Garmin 초기화.
    Garmin.login 소스 확인 결과:
    - `if len(tokenstore) > 512:` 이면 `self.client.loads(tokenstore)` 호출
    - `else:` 이면 `Path(tokenstore).expanduser().resolve()` 디렉토리에서 토큰 파일을 추출합니다.
    """
    email = os.getenv("GARMIN_EMAIL", "").strip()
    password = os.getenv("GARMIN_PASSWORD", "").strip()
    raw_key = os.getenv("GARMIN_API_KEY", "").strip()

    token_dir_from_key: Optional[str] = None
    raw_token_str: Optional[str] = None
    if (not email or not password) and raw_key:
        k_email, k_pass, token_dir_from_key, raw_token_str = decode_garmin_api_key(raw_key)
        email = email or k_email
        password = password or k_pass

    token_path = Path(GARMIN_TOKEN_DIR)

    # 1) 로컬 토큰 디렉토리가 존재하면 우선 시도
    if token_path.exists():
        try:
            c = Garmin()
            c.login(str(token_path))
            return c
        except Exception:
            pass

    # 2) GARMIN_API_KEY가 JSON/Base64 토큰 번들인 경우
    if token_dir_from_key:
        try:
            c = Garmin()
            c.login(token_dir_from_key)
            return c
        except Exception as e:
            print(f"[WARN] GARMIN_API_KEY 토큰 디렉토리 로그인 실패: {e}", flush=True)

    # 3) GARMIN_API_KEY가 garminconnect 0.3.x 네이티브 토큰 문자열(GCDC_0...)인 경우
    if raw_token_str:
        # 3-a) c.client.loads(raw_token_str) 호출 후 실제 에러 출력
        try:
            c = Garmin()
            if hasattr(c, "client") and hasattr(c.client, "loads"):
                c.client.loads(raw_token_str)
                return c
        except Exception as e_loads:
            print(f"[WARN] c.client.loads(GCDC) 직접 호출 실패: {e_loads}", flush=True)

        # 3-b) 공백 패딩으로 len(tokenstore) > 512 조건을 만족시켜 Garmin().login(padded) 호출
        try:
            padded_tok = raw_token_str.ljust(600)
            c = Garmin()
            c.login(padded_tok)
            return c
        except Exception as e_pad:
            print(f"[WARN] Garmin().login(padded_GCDC) 실패: {e_pad}", flush=True)

        # 3-c) 디렉토리 내부에서 garminconnect가 기대하는 모든 파일명 후보 생성 후 c.login(tmp_dir)
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="garmin_gcdc_all_"))
            for fname in (
                "garmin_tokens",
                "garmin_tokens.json",
                "garmin_token",
                "garmin_token.json",
                "oauth1_token.json",
                "oauth2_token.json",
                "oauth_tokens.json",
                "tokens.json",
                "token.json",
                ".garmin_tokens",
                "session.json",
                "gcdc_token",
                "gcdc_token.txt",
            ):
                (tmp_dir / fname).write_text(raw_token_str, encoding="utf-8")
            c = Garmin()
            c.login(str(tmp_dir))
            return c
        except Exception as e_dir:
            print(f"[WARN] 전체 후보 파일명 디렉토리 로그인 실패: {e_dir}", flush=True)

    # 4) 이메일/비밀번호 로그인
    if email and password:
        c = Garmin(email, password)
        c.login()
        token_path.mkdir(parents=True, exist_ok=True)
        if hasattr(c, "client") and hasattr(c.client, "dump"):
            c.client.dump(str(token_path))
        elif hasattr(c, "garth") and hasattr(c.garth, "dump"):
            c.garth.dump(str(token_path))
        return c

    # 진단용: Garmin.login의 토큰 파일 로딩부(20~75번째 줄) 및 client.loads 시그니처 출력
    try:
        login_src = inspect.getsource(Garmin.login)
        lines = [line.strip() for line in login_src.splitlines()[18:75]]
        diag_login = " | ".join(lines)
    except Exception:
        diag_login = "N/A"
    try:
        c_tmp = Garmin()
        loads_src = inspect.getsource(c_tmp.client.loads)
        diag_loads = " | ".join(line.strip() for line in loads_src.splitlines()[:25])
    except Exception:
        diag_loads = "N/A"

    raise ValueError(
        f"GARMIN_API_KEY(GCDC) 인증에 실패했습니다.\n"
        f"[Garmin.login 19~75행]: {diag_login}\n"
        f"[client.loads 1~25행]: {diag_loads}"
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
    """가민 활동 목록 수집 + 기존 CSV 병합 + MyFitnessPal 중복 제거 + 전체 FIT 누적 다운로드"""
    csv_path = DATA_DIR / "garmin_activities.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()

    raw_activities = client.get_activities(0, limit)
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
        avg_power = act.get("averagePower")
        np_watts = act.get("normPower")
        tss = act.get("trainingStressScore")
        if_val = act.get("intensityFactor")

        if pd.isna(if_val) and np_watts and FTP_WATTS > 0:
            if_val = round(float(np_watts) / FTP_WATTS, 4)
        if pd.isna(tss) and np_watts and duration_sec > 0 and FTP_WATTS > 0:
            tss = round((duration_sec * float(np_watts) * float(if_val)) / (FTP_WATTS * 3600) * 100, 2)

        row = {
            "activity_id": act_id,
            "date": start_local,
            "name": act.get("activityName", ""),
            "type": act_type,
            "distance_km": round((act.get("distance") or 0) / 1000, 2),
            "duration_min": round(duration_sec / 60, 1),
            "elevation_gain_m": round(act.get("elevationGain") or 0, 1) if act.get("elevationGain") else None,
            "avg_power": avg_power,
            "norm_power_NP": np_watts,
            "max_power": act.get("maxPower"),
            "max_20min_power": act.get("max20MinPower"),
            "training_stress_score_TSS": tss,
            "intensity_factor_IF": if_val,
            "avg_hr": act.get("averageHR"),
            "max_hr": act.get("maxHR"),
            "avg_cadence": act.get("averageBikingCadenceInRevPerMinute"),
            "left_right_balance": act.get("leftRightBalance"),
            "aerobic_training_effect": act.get("aerobicTrainingEffect"),
            "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
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
    """가민 웰니스(HRV 기준 범위 + 수면/안정심박/스트레스/바디배터리 다중 엔드포인트 대체 경로) 수집"""
    csv_path = DATA_DIR / "garmin_health.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    today = datetime.now().date()

    for offset in range(days):
        d_str = (today - timedelta(days=offset)).isoformat()

        resting_hr = None
        avg_stress = None
        bb_high = None
        bb_low = None
        hrv_avg = None
        hrv_status = None
        hrv_base_low = None
        hrv_base_high = None
        sleep_score = None
        sleep_hours = None
        steps = None

        try:
            stats = client.get_stats(d_str) or {}
            resting_hr = stats.get("restingHeartRate")
            avg_stress = stats.get("averageStressLevel")
            bb_high = stats.get("bodyBatteryHighestValue")
            bb_low = stats.get("bodyBatteryLowestValue")
            steps = stats.get("totalSteps")
        except Exception:
            pass

        if resting_hr is None:
            try:
                rhr_data = client.get_rhr_day(d_str) or {}
                metrics_map = rhr_data.get("allMetrics", {}).get("metricsMap", {})
                rhr_list = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                if rhr_list:
                    resting_hr = rhr_list[0].get("value")
            except Exception:
                pass
        if resting_hr is None:
            try:
                hr_data = client.get_heart_rates(d_str) or {}
                resting_hr = hr_data.get("restingHeartRate")
            except Exception:
                pass

        if bb_high is None or bb_low is None:
            try:
                bb_list = client.get_body_battery(d_str, d_str) or []
                if bb_list and isinstance(bb_list, list):
                    bb_vals = [
                        v[1]
                        for entry in bb_list
                        for v in (entry.get("bodyBatteryValuesArray") or [])
                        if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] is not None
                    ]
                    if bb_vals:
                        bb_high = max(bb_vals)
                        bb_low = min(bb_vals)
            except Exception:
                pass

        if avg_stress is None:
            try:
                stress_data = client.get_stress_data(d_str) or {}
                avg_stress = stress_data.get("avgStressLevel")
            except Exception:
                pass

        try:
            hrv_data = client.get_hrv_data(d_str) or {}
            hrv_summary = hrv_data.get("hrvSummary") or {}
            hrv_avg = hrv_summary.get("lastNightAvg")
            hrv_status = hrv_summary.get("status")
            baseline = hrv_summary.get("baseline") or {}
            hrv_base_low = baseline.get("balancedLow")
            hrv_base_high = baseline.get("balancedUpper")
        except Exception:
            pass

        try:
            sleep_data = client.get_sleep_data(d_str) or {}
            daily_sleep = sleep_data.get("dailySleepDTO") or {}
            sleep_sec = daily_sleep.get("sleepTimeSeconds")
            if sleep_sec:
                sleep_hours = round(float(sleep_sec) / 3600, 2)
            sleep_scores = daily_sleep.get("sleepScores") or {}
            overall = sleep_scores.get("overall") or {}
            sleep_score = overall.get("value") or daily_sleep.get("sleepScore")
        except Exception:
            pass

        rows.append(
            {
                "date": d_str,
                "resting_hr": resting_hr,
                "avg_stress": avg_stress,
                "body_battery_high": bb_high,
                "body_battery_low": bb_low,
                "hrv_last_night_avg": hrv_avg,
                "hrv_status": hrv_status,
                "hrv_baseline_low": hrv_base_low,
                "hrv_baseline_high": hrv_base_high,
                "sleep_score": sleep_score,
                "sleep_hours": sleep_hours,
                "steps": steps,
            }
        )
        time.sleep(0.15)

    new_df = pd.DataFrame(rows)
    if not existing_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined = combined.sort_values("date").groupby("date", as_index=False).last()
    else:
        combined = new_df

    combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
    combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return combined


def fetch_strava_activities(per_page: int = 100) -> pd.DataFrame:
    """Strava API에서 활동 이력 수집 및 누적 저장"""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
        print("[INFO] Strava 환경변수가 설정되지 않아 Strava 수집을 건너뜁니다.", flush=True)
        return pd.DataFrame()

    csv_path = DATA_DIR / "strava_activities.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()

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

    act_resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": per_page, "page": 1},
        timeout=20,
    )
    act_resp.raise_for_status()
    items = act_resp.json()

    rows = []
    for it in items:
        if it.get("type") not in ("Ride", "VirtualRide"):
            continue
        rows.append(
            {
                "id": int(it["id"]),
                "date": it.get("start_date_local"),
                "name": it.get("name"),
                "type": it.get("type"),
                "distance_km": round((it.get("distance") or 0) / 1000, 2),
                "moving_time_min": round((it.get("moving_time") or 0) / 60, 1),
                "elevation_gain_m": round(it.get("total_elevation_gain") or 0, 1),
                "avg_speed_kph": round((it.get("average_speed") or 0) * 3.6, 1),
                "avg_watts": it.get("average_watts"),
                "weighted_avg_watts_NP": it.get("weighted_average_watts"),
                "max_watts": it.get("max_watts"),
                "kilojoules": it.get("kilojoules"),
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

    return combined


def main() -> None:
    print("=== Fitness 데이터 수집 시작 ===", flush=True)
    garmin_client = init_garmin()

    act_df = fetch_garmin_activities(garmin_client, limit=ACTIVITY_LIMIT)
    fit_count = len(list(FIT_DIR.glob("*.fit")))
    print(f"[OK] Garmin 활동: {len(act_df)}건 / 누적 FIT 파일: {fit_count}개", flush=True)

    health_df = fetch_garmin_health(garmin_client, days=WELLNESS_DAYS)
    print(f"[OK] Garmin 웰니스: {len(health_df)}일치 저장 완료", flush=True)

    strava_df = fetch_strava_activities(per_page=100)
    if not strava_df.empty:
        print(f"[OK] Strava 활동: {len(strava_df)}건 저장 완료", flush=True)

    print("=== 수집 완료 ===", flush=True)


if __name__ == "__main__":
    main()
