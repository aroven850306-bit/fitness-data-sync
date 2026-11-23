#!/usr/bin/env python3
"""
Garmin Connect + Strava 데이터 수집 스크립트
- 루트 디렉토리(./)에 CSV 및 fit_files/ 저장
- Garmin 세션 토큰(JSON/파일/디렉토리) 우선 복원 + 이메일/비밀번호 로그인 fallback (429 회피 지원)
- 가민 웰니스 + Training Readiness / Training Load / ACWR 수집
- 기존 CSV의 유효 값 보존 안전 병합
"""

import io
import json
import os
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))
FIT_DIR = DATA_DIR / "fit_files"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIT_DIR.mkdir(parents=True, exist_ok=True)

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "").strip()
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "").strip()
GARMIN_TOKEN_DIR = os.getenv("GARMIN_TOKEN_DIR", str(DATA_DIR / ".garmin_tokens")).strip()
GARMIN_TOKEN = os.getenv("GARMIN_TOKEN", "").strip()
GARMIN_API_KEY = os.getenv("GARMIN_API_KEY", "").strip()

STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "").strip()
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()

FTP_WATTS = float(os.getenv("FTP_WATTS", "195"))
WELLNESS_DAYS = int(os.getenv("WELLNESS_DAYS", "30"))
ACTIVITY_LIMIT = int(os.getenv("ACTIVITY_LIMIT", "100"))


def _find_first_value(data: Any, candidate_keys: Iterable[str]) -> Any:
    """중첩 dict/list 구조에서 candidate_keys 중 먼저 발견되는 유효 값을 반환"""
    key_list = list(candidate_keys)

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            for k in key_list:
                if k in node and node[k] is not None and node[k] != "":
                    return node[k]
            for v in node.values():
                found = _walk(v)
                if found is not None and found != "":
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item)
                if found is not None and found != "":
                    return found
        return None

    return _walk(data)


def _to_float(val: Any, ndigits: Optional[int] = 2) -> Optional[float]:
    """숫자 변환 후 반올림 반환 (정수형 ndigits=0이면 int 반환)"""
    if val is None or val == "":
        return None
    try:
        num = float(val)
        if ndigits == 0:
            return int(round(num))
        if ndigits is not None:
            return round(num, ndigits)
        return num
    except (ValueError, TypeError):
        return None


def _merge_prefer_non_null(existing_df: pd.DataFrame, new_df: pd.DataFrame, key_col: str) -> pd.DataFrame:
    """기존 데이터와 신규 데이터를 병합하되, 새 값이 비어 있을 때 기존 유효 값을 보존"""
    if existing_df.empty:
        return new_df.copy()
    if new_df.empty:
        return existing_df.copy()

    ex = existing_df.copy()
    nw = new_df.copy()

    ex[key_col] = ex[key_col].astype(str)
    nw[key_col] = nw[key_col].astype(str)

    all_cols = list(dict.fromkeys(list(ex.columns) + list(nw.columns)))
    ex = ex.reindex(columns=all_cols)
    nw = nw.reindex(columns=all_cols)

    ex_map: Dict[str, Dict[str, Any]] = {}
    for _, r in ex.iterrows():
        ex_map[str(r[key_col])] = r.to_dict()

    for _, r in nw.iterrows():
        k = str(r[key_col])
        row_dict = r.to_dict()
        prev_dict = ex_map.get(k)
        if prev_dict:
            merged_row = dict(prev_dict)
            for col in all_cols:
                val = row_dict.get(col)
                if pd.notna(val) and val != "":
                    merged_row[col] = val
            ex_map[k] = merged_row
        else:
            ex_map[k] = row_dict

    return pd.DataFrame(list(ex_map.values()), columns=all_cols)


def _save_token_store(client: Garmin, target_dir: Path) -> None:
    """버전 차이를 고려해 토큰 스토어를 안전하게 저장"""
    target_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(client, "client") and hasattr(client.client, "dump"):
        client.client.dump(str(target_dir))
    elif hasattr(client, "garth") and hasattr(client.garth, "dump"):
        client.garth.dump(str(target_dir))


def init_garmin() -> Garmin:
    """저장된 세션 토큰(JSON/디렉토리)을 우선 사용하고, 필요 시 이메일/비밀번호로 로그인"""
    token_candidates = [v for v in (GARMIN_TOKEN, GARMIN_API_KEY) if v]

    # 1. JSON 세션 토큰 문자열(GARMIN_TOKEN 또는 GARMIN_API_KEY)이 있는 경우 우선 시도
    for raw_tok in token_candidates:
        try:
            parsed = json.loads(raw_tok)
            if isinstance(parsed, dict) and ("di_token" in parsed or "oauth2_token" in parsed):
                tmp_dir = Path(tempfile.mkdtemp(prefix="garmin_tok_"))
                (tmp_dir / "garmin_tokens.json").write_text(
                    json.dumps(parsed), encoding="utf-8"
                )
                client = Garmin()
                client.login(str(tmp_dir))
                _save_token_store(client, Path(GARMIN_TOKEN_DIR))
                print("[OK] 세션 토큰으로 Garmin 로그인 성공", flush=True)
                return client
        except Exception:
            pass

    # 2. 로컬 토큰 디렉토리/파일 캐시 시도
    if GARMIN_TOKEN_DIR:
        tok_path = Path(GARMIN_TOKEN_DIR)
        if tok_path.exists():
            try:
                client = Garmin()
                client.login(str(tok_path))
                _save_token_store(client, tok_path)
                print("[OK] 캐시된 토큰 스토어로 Garmin 로그인 성공", flush=True)
                return client
            except Exception as e:
                print(f"[WARN] 캐시 토큰 로그인 실패: {e}", flush=True)

    # 3. 이메일/비밀번호 로그인 fallback (429 발생 시 대기 후 재시도)
    if not (GARMIN_EMAIL and GARMIN_PASSWORD):
        raise RuntimeError(
            "Garmin 인증 정보가 없습니다. 유효한 JSON 세션 토큰(GARMIN_TOKEN/GARMIN_API_KEY) 또는 "
            "GARMIN_EMAIL / GARMIN_PASSWORD를 설정해 주세요."
        )

    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
            client.login()
            _save_token_store(client, Path(GARMIN_TOKEN_DIR))
            print("[OK] 이메일/비밀번호로 Garmin 로그인 성공", flush=True)
            return client
        except Exception as e:
            last_err = e
            msg = str(e)
            if "429" in msg or "rate limit" in msg.lower():
                wait_sec = attempt * 15
                print(
                    f"[WARN] Garmin 429 Rate Limit 감지 (시도 {attempt}/3) — {wait_sec}초 대기 후 재시도...",
                    flush=True,
                )
                time.sleep(wait_sec)
            else:
                break

    raise RuntimeError(f"Garmin 로그인 실패: {last_err}") from last_err


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
        combined = _merge_prefer_non_null(existing_df, new_df, "activity_id")
    elif not new_df.empty:
        combined = new_df
    else:
        combined = existing_df

    if not combined.empty:
        combined = combined[~combined["name"].astype(str).str.contains("MyFitnessPal", case=False, na=False)]
        combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
        combined.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return combined


def _fetch_readiness_and_load(client: Garmin, d_str: str) -> Dict[str, Any]:
    """특정 날짜(YYYY-MM-DD)의 Training Readiness 및 Training Load / ACWR 조회"""
    out: Dict[str, Any] = {
        "training_readiness_score": None,
        "training_readiness_level": None,
        "training_status": None,
        "acute_load": None,
        "chronic_load": None,
        "acwr": None,
    }

    try:
        readiness_raw = client.get_training_readiness(d_str)
        if readiness_raw:
            score = _find_first_value(
                readiness_raw,
                ["trainingReadinessScore", "score", "readinessScore", "overallScore"],
            )
            level = _find_first_value(
                readiness_raw,
                ["trainingReadinessLevel", "level", "category", "status"],
            )
            out["training_readiness_score"] = _to_float(score, ndigits=0)
            if level is not None:
                out["training_readiness_level"] = str(level)
    except Exception:
        pass

    try:
        status_raw = client.get_training_status(d_str)
        if status_raw:
            status_label = _find_first_value(
                status_raw,
                ["trainingStatusPhrase", "trainingStatus", "statusPhrase", "status"],
            )
            acute = _find_first_value(
                status_raw,
                ["dailyAcuteLoad", "acuteLoad", "acuteTrainingLoad", "atl"],
            )
            chronic = _find_first_value(
                status_raw,
                ["dailyChronicLoad", "chronicLoad", "chronicTrainingLoad", "ctl"],
            )
            acwr_val = _find_first_value(
                status_raw,
                ["acwr", "loadRatio", "acuteChronicWorkloadRatio", "acwrRatio"],
            )

            if status_label is not None:
                out["training_status"] = str(status_label)
            out["acute_load"] = _to_float(acute, ndigits=1)
            out["chronic_load"] = _to_float(chronic, ndigits=1)
            out["acwr"] = _to_float(acwr_val, ndigits=2)

            if (
                out["acwr"] is None
                and out["acute_load"] is not None
                and out["chronic_load"] is not None
                and out["chronic_load"] > 0
            ):
                out["acwr"] = round(out["acute_load"] / out["chronic_load"], 2)
    except Exception:
        pass

    return out


def fetch_garmin_health(client: Garmin, days: int = WELLNESS_DAYS) -> pd.DataFrame:
    """가민 웰니스 + Training Readiness / Training Load / ACWR 수집"""
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

        rl_info = _fetch_readiness_and_load(client, d_str)

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
                "training_readiness_score": rl_info["training_readiness_score"],
                "training_readiness_level": rl_info["training_readiness_level"],
                "training_status": rl_info["training_status"],
                "acute_load": rl_info["acute_load"],
                "chronic_load": rl_info["chronic_load"],
                "acwr": rl_info["acwr"],
            }
        )
        time.sleep(0.15)

    new_df = pd.DataFrame(rows)
    if not existing_df.empty:
        combined = _merge_prefer_non_null(existing_df, new_df, "date")
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
        combined = _merge_prefer_non_null(existing_df, new_df, "id")
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
