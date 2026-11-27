#!/usr/bin/env python3
"""
Garmin Connect + Strava 데이터 수집 스크립트 (운영 정리 및 Strava 확장 통합 버전)
- 루트 디렉토리(./)에 CSV 및 fit_files/ 저장
- Garmin 세션 토큰(JSON/파일/디렉토리) 우선 복원 + 이메일/비밀번호 로그인 fallback (429 회피 지원)
- 가민 웰니스 + Training Readiness / Training Load / ACWR 수집
- 바디배터리 현재 잔량(body_battery_current) 실측값 안전 추출 (없으면 None으로 유지)
- Strava 확장 수집:
  1) 활동 상세 조회(RPE perceived_exertion, 칼로리, 트레이너 여부, 최대 속도)
  2) 세그먼트 PR / 업힐 기록(strava_segments.csv 별도 누적 저장 및 활동별 요약 병합)
  3) Strava 파워·심박 존 분포(zones) 및 야외 라이딩 경사도 구간별 파워(평지/완경사/업힐) 집계
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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
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
BASELINE_FILE = Path(os.getenv("PERFORMANCE_BASELINE_FILE", str(DATA_DIR / "performance_baseline.json")))


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
        if not np.isfinite(num):
            return None
        if ndigits == 0:
            return int(round(num))
        if ndigits is not None:
            return round(num, ndigits)
        return num
    except (ValueError, TypeError):
        return None


def _load_performance_baseline() -> Tuple[float, float, float]:
    """
    기준 성능값(FTP, LTHR, HRmax)을 로드합니다.
    우선순위: 환경변수(FTP_WATTS/LTHR_BPM/MAX_HR) -> performance_baseline.json -> 공식 확정 기본값(194W, 147bpm, 173bpm)
    """
    ftp_val: Optional[float] = None
    lthr_val: Optional[float] = None
    max_hr_val: Optional[float] = None

    if BASELINE_FILE.exists():
        try:
            payload = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                ftp_val = _to_float(
                    _find_first_value(payload, ["official_ftp_watts", "ftp_watts", "ftp"]),
                    ndigits=1,
                )
                lthr_val = _to_float(
                    _find_first_value(payload, ["official_lthr_bpm", "lthr_bpm", "lthr"]),
                    ndigits=1,
                )
                max_hr_val = _to_float(
                    _find_first_value(payload, ["max_hr_bpm", "max_hr", "hrmax"]),
                    ndigits=0,
                )
        except Exception as e:
            print(f"[WARN] 기준선 파일({BASELINE_FILE.name}) 파싱 실패: {e}", flush=True)

    env_ftp = os.getenv("FTP_WATTS", "").strip()
    env_lthr = os.getenv("LTHR_BPM", "").strip()
    env_max_hr = os.getenv("MAX_HR", "").strip()

    resolved_ftp = float(env_ftp) if env_ftp else (ftp_val if ftp_val is not None else 194.0)
    resolved_lthr = float(env_lthr) if env_lthr else (lthr_val if lthr_val is not None else 147.0)
    resolved_max_hr = float(env_max_hr) if env_max_hr else (max_hr_val if max_hr_val is not None else 173.0)
    return resolved_ftp, resolved_lthr, resolved_max_hr


# 공식 확정 기준치 자동 연동 (194W / 147bpm / 173bpm 기본값, 환경변수 및 baseline JSON 우선 반영)
FTP_WATTS, LTHR_BPM, MAX_HR_BPM = _load_performance_baseline()
WELLNESS_DAYS = int(os.getenv("WELLNESS_DAYS", "30"))
ACTIVITY_LIMIT = int(os.getenv("ACTIVITY_LIMIT", "100"))
# Strava 상세/존/스트림 API 호출 대상 최신 미수집 활동 수 (15분 100회 제한 보호)
STRAVA_DETAIL_LIMIT = int(os.getenv("STRAVA_DETAIL_LIMIT", "15"))


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
    """특정 날짜(YYYY-MM-DD)의 Training Readiness, Training Load/ACWR 및 훈련 부하 포커스 3종 조회"""
    out: Dict[str, Any] = {
        "training_readiness_score": None,
        "training_readiness_level": None,
        "training_status": None,
        "acute_load": None,
        "chronic_load": None,
        "acwr": None,
        "training_load_low_aerobic": None,
        "training_load_high_aerobic": None,
        "training_load_anaerobic": None,
    }

    for fn_name in ("get_training_readiness", "get_morning_training_readiness"):
        if hasattr(client, fn_name):
            try:
                raw = getattr(client, fn_name)(d_str)
                if raw:
                    score = _find_first_value(
                        raw,
                        ["trainingReadinessScore", "score", "readinessScore", "overallScore"],
                    )
                    level = _find_first_value(
                        raw,
                        ["trainingReadinessLevel", "level", "category", "status"],
                    )
                    out["training_readiness_score"] = _to_float(score, ndigits=0)
                    if level is not None:
                        out["training_readiness_level"] = str(level)
                    if out["training_readiness_score"] is not None:
                        break
            except Exception:
                pass

    # garminconnect 내부 connectapi 경로 fallback (Training Readiness)
    if out["training_readiness_score"] is None and hasattr(client, "connectapi"):
        for endpoint in (
            f"/metrics-service/metrics/trainingreadiness/{d_str}",
            f"/metrics-service/metrics/morningtrainingreadiness/{d_str}",
        ):
            try:
                raw = client.connectapi(endpoint)
                if raw:
                    score = _find_first_value(
                        raw,
                        ["score", "trainingReadinessScore", "readinessScore", "overallScore"],
                    )
                    level = _find_first_value(
                        raw,
                        ["level", "trainingReadinessLevel", "category", "feedbackShort"],
                    )
                    out["training_readiness_score"] = _to_float(score, ndigits=0)
                    if level is not None and out["training_readiness_level"] is None:
                        out["training_readiness_level"] = str(level)
                    if out["training_readiness_score"] is not None:
                        break
            except Exception:
                pass

    def _extract_load_fields(payload: Any) -> None:
        if not payload:
            return
        status_label = _find_first_value(
            payload,
            ["trainingStatusPhrase", "trainingStatus", "statusPhrase", "status"],
        )
        acute = _find_first_value(
            payload,
            ["dailyAcuteLoad", "acuteLoad", "acuteTrainingLoad", "atl", "dailyAcuteTrainingLoad"],
        )
        chronic = _find_first_value(
            payload,
            ["dailyChronicLoad", "chronicLoad", "chronicTrainingLoad", "ctl", "dailyChronicTrainingLoad"],
        )
        acwr_val = _find_first_value(
            payload,
            ["acwr", "loadRatio", "acuteChronicWorkloadRatio", "acwrRatio"],
        )
        low_aero = _find_first_value(
            payload,
            [
                "monthlyLoadAerobicLow",
                "dailyLoadAerobicLow",
                "aerobicLowLoad",
                "lowAerobicLoad",
                "trainingLoadAerobicLow",
            ],
        )
        high_aero = _find_first_value(
            payload,
            [
                "monthlyLoadAerobicHigh",
                "dailyLoadAerobicHigh",
                "aerobicHighLoad",
                "highAerobicLoad",
                "trainingLoadAerobicHigh",
            ],
        )
        anaerobic = _find_first_value(
            payload,
            [
                "monthlyLoadAnaerobic",
                "dailyLoadAnaerobic",
                "anaerobicLoad",
                "trainingLoadAnaerobic",
            ],
        )

        if status_label is not None and out["training_status"] is None:
            out["training_status"] = str(status_label)
        if acute is not None and out["acute_load"] is None:
            out["acute_load"] = _to_float(acute, ndigits=1)
        if chronic is not None and out["chronic_load"] is None:
            out["chronic_load"] = _to_float(chronic, ndigits=1)
        if acwr_val is not None and out["acwr"] is None:
            out["acwr"] = _to_float(acwr_val, ndigits=2)
        if low_aero is not None and out["training_load_low_aerobic"] is None:
            out["training_load_low_aerobic"] = _to_float(low_aero, ndigits=1)
        if high_aero is not None and out["training_load_high_aerobic"] is None:
            out["training_load_high_aerobic"] = _to_float(high_aero, ndigits=1)
        if anaerobic is not None and out["training_load_anaerobic"] is None:
            out["training_load_anaerobic"] = _to_float(anaerobic, ndigits=1)

    for fn_name in ("get_training_status", "get_max_metrics"):
        if hasattr(client, fn_name):
            try:
                _extract_load_fields(getattr(client, fn_name)(d_str))
            except Exception:
                pass

    # garminconnect 내부 connectapi 경로 fallback (Training Status / Load Balance Focus)
    if hasattr(client, "connectapi") and (
        out["acute_load"] is None
        or out["chronic_load"] is None
        or out["training_load_low_aerobic"] is None
    ):
        for endpoint in (
            f"/metrics-service/metrics/trainingstatus/aggregated/{d_str}",
            f"/metrics-service/metrics/trainingstatus/daily/{d_str}",
            f"/metrics-service/metrics/trainingloadbalance/latest/{d_str}",
        ):
            try:
                _extract_load_fields(client.connectapi(endpoint))
            except Exception:
                pass

    if (
        out["acwr"] is None
        and out["acute_load"] is not None
        and out["chronic_load"] is not None
        and out["chronic_load"] > 0
    ):
        out["acwr"] = round(out["acute_load"] / out["chronic_load"], 2)

    return out


def fetch_garmin_health(client: Garmin, days: int = WELLNESS_DAYS) -> pd.DataFrame:
    """가민 웰니스 + Training Readiness / Training Load / ACWR + 바디배터리 현재 잔량 수집"""
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
        bb_current = None
        hrv_avg = None
        hrv_status = None
        hrv_base_low = None
        hrv_base_high = None
        hrv_weekly_avg = None
        min_hr = None
        max_hr = None
        sleep_score = None
        sleep_hours = None
        deep_sleep_min = None
        rem_sleep_min = None
        light_sleep_min = None
        awake_sleep_min = None
        max_stress = None
        rest_stress_sec = None
        bb_charged = None
        bb_drained = None
        avg_respiration = None
        avg_spo2 = None
        active_kcal = None
        total_kcal = None
        steps = None

        try:
            stats = client.get_stats(d_str) or {}
            resting_hr = stats.get("restingHeartRate")
            min_hr = _find_first_value(
                stats,
                ["minHeartRate", "restingHeartRate", "minHr", "lowestHeartRate"],
            )
            max_hr = _find_first_value(
                stats,
                ["maxHeartRate", "maxHr", "highestHeartRate"],
            )
            avg_stress = stats.get("averageStressLevel")
            max_stress = _find_first_value(
                stats,
                ["maxStressLevel", "highestStressLevel", "stressMax"],
            )
            rest_stress_sec = _find_first_value(
                stats,
                ["restStressDuration", "restStressSeconds", "lowStressDuration"],
            )
            avg_respiration = _find_first_value(
                stats,
                ["avgWakingRespirationValue", "avgRespirationValue", "averageRespirationValue"],
            )
            avg_spo2 = _find_first_value(
                stats,
                ["averageSpo2", "avgSpo2", "spo2Value"],
            )
            bb_high = stats.get("bodyBatteryHighestValue")
            bb_low = stats.get("bodyBatteryLowestValue")
            bb_charged = _find_first_value(
                stats,
                ["bodyBatteryChargedValue", "bodyBatteryCharged", "chargedBodyBattery"],
            )
            bb_drained = _find_first_value(
                stats,
                ["bodyBatteryDrainedValue", "bodyBatteryDrained", "drainedBodyBattery"],
            )
            bb_current = _find_first_value(
                stats,
                ["bodyBatteryMostRecentValue", "bodyBatteryCurrentValue", "currentBodyBattery", "bodyBatteryLatestValue"],
            )
            active_kcal = _find_first_value(
                stats,
                ["activeKilocalories", "activeKcal", "activeCalories"],
            )
            total_kcal = _find_first_value(
                stats,
                ["totalKilocalories", "totalKcal", "calories"],
            )
            if total_kcal is None and active_kcal is not None:
                bmr_kcal = _find_first_value(
                    stats,
                    ["bmrKilocalories", "restingKilocalories", "bmrKcal"],
                )
                if bmr_kcal is not None:
                    try:
                        total_kcal = float(active_kcal) + float(bmr_kcal)
                    except (ValueError, TypeError):
                        pass
            steps = stats.get("totalSteps")
        except Exception:
            pass

        if resting_hr is None or min_hr is None or max_hr is None:
            try:
                hr_data = client.get_heart_rates(d_str) or {}
                if resting_hr is None:
                    resting_hr = hr_data.get("restingHeartRate")
                if min_hr is None:
                    min_hr = _find_first_value(
                        hr_data,
                        ["minHeartRate", "restingHeartRate", "minHr"],
                    )
                if max_hr is None:
                    max_hr = _find_first_value(
                        hr_data,
                        ["maxHeartRate", "maxHr"],
                    )
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

        # get_body_battery에서 시계열 마지막 유효 잔량(bb_current) 및 최고/최저 보완 추출
        try:
            bb_list = client.get_body_battery(d_str, d_str) or []
            if bb_list and isinstance(bb_list, list):
                bb_pairs = [
                    (v[0], v[1])
                    for entry in bb_list
                    for v in (entry.get("bodyBatteryValuesArray") or [])
                    if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] is not None
                ]
                if bb_pairs:
                    bb_pairs.sort(key=lambda item: item[0])
                    bb_vals = [p[1] for p in bb_pairs]
                    if bb_high is None:
                        bb_high = max(bb_vals)
                    if bb_low is None:
                        bb_low = min(bb_vals)
                    if bb_current is None:
                        bb_current = bb_pairs[-1][1]
                if bb_charged is None:
                    bb_charged = _find_first_value(
                        bb_list,
                        ["bodyBatteryChargedValue", "charged", "totalCharged"],
                    )
                if bb_drained is None:
                    bb_drained = _find_first_value(
                        bb_list,
                        ["bodyBatteryDrainedValue", "drained", "totalDrained"],
                    )
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

        # get_body_battery에서 시계열 마지막 유효 잔량(bb_current) 및 최고/최저 보완 추출
        try:
            bb_list = client.get_body_battery(d_str, d_str) or []
            if bb_list and isinstance(bb_list, list):
                bb_pairs = [
                    (v[0], v[1])
                    for entry in bb_list
                    for v in (entry.get("bodyBatteryValuesArray") or [])
                    if isinstance(v, (list, tuple)) and len(v) >= 2 and v[1] is not None
                ]
                if bb_pairs:
                    bb_pairs.sort(key=lambda item: item[0])
                    bb_vals = [p[1] for p in bb_pairs]
                    if bb_high is None:
                        bb_high = max(bb_vals)
                    if bb_low is None:
                        bb_low = min(bb_vals)
                    if bb_current is None:
                        bb_current = bb_pairs[-1][1]
        except Exception:
            pass

        if avg_stress is None:
            try:
                stress_data = client.get_stress_data(d_str) or {}
                avg_stress = stress_data.get("avgStressLevel")
                if max_stress is None:
                    max_stress = _find_first_value(
                        stress_data,
                        ["maxStressLevel", "highestStressLevel", "stressMax"],
                    )
                if rest_stress_sec is None:
                    rest_stress_sec = _find_first_value(
                        stress_data,
                        ["restStressDuration", "restStressSeconds", "lowStressDuration"],
                    )
            except Exception:
                pass

        if max_stress is None or rest_stress_sec is None:
            try:
                all_day_stress = client.get_all_day_stress(d_str) or {}
                if max_stress is None:
                    max_stress = _find_first_value(
                        all_day_stress,
                        ["maxStressLevel", "highestStressLevel", "stressMax"],
                    )
                if rest_stress_sec is None:
                    rest_stress_sec = _find_first_value(
                        all_day_stress,
                        ["restStressDuration", "restStressSeconds", "lowStressDuration"],
                    )
            except Exception:
                pass

        if avg_respiration is None and hasattr(client, "get_respiration_data"):
            try:
                resp_data = client.get_respiration_data(d_str) or {}
                avg_respiration = _find_first_value(
                    resp_data,
                    ["avgWakingRespirationValue", "avgSleepRespirationValue", "avgRespirationValue", "averageRespirationValue"],
                )
            except Exception:
                pass

        if avg_spo2 is None and hasattr(client, "get_spo2_data"):
            try:
                spo2_data = client.get_spo2_data(d_str) or {}
                avg_spo2 = _find_first_value(
                    spo2_data,
                    ["averageSpo2", "avgSpo2", "latestSpo2", "spo2Value"],
                )
            except Exception:
                pass

        try:
            hrv_data = client.get_hrv_data(d_str) or {}
            hrv_summary = hrv_data.get("hrvSummary") or {}
            hrv_avg = hrv_summary.get("lastNightAvg")
            hrv_status = hrv_summary.get("status")
            hrv_weekly_avg = _find_first_value(
                hrv_summary,
                ["weeklyAvg", "lastNight5MinHigh", "baselineBalancedAvg", "weeklyAverage"],
            ) or _find_first_value(
                hrv_data,
                ["weeklyAvg", "weeklyAverage"],
            )
            baseline = hrv_summary.get("baseline") or {}
            hrv_base_low = baseline.get("balancedLow")
            hrv_base_high = baseline.get("balancedUpper")
        except Exception:
            pass

        try:
            sleep_data = client.get_sleep_data(d_str) or {}
            daily_sleep = sleep_data.get("dailySleepDTO") or {}
            sleep_sec = daily_sleep.get("sleepTimeSeconds") or _find_first_value(
                sleep_data, ["sleepTimeSeconds"]
            )
            if sleep_sec:
                sleep_hours = round(float(sleep_sec) / 3600, 2)
            sleep_scores = daily_sleep.get("sleepScores") or {}
            overall = sleep_scores.get("overall") or {}
            sleep_score = overall.get("value") or daily_sleep.get("sleepScore")

            deep_sec = daily_sleep.get("deepSleepSeconds")
            if deep_sec is None:
                deep_sec = _find_first_value(sleep_data, ["deepSleepSeconds", "deepSleepDurationInSeconds"])
            if deep_sec is not None:
                deep_sleep_min = _to_float(float(deep_sec) / 60.0, ndigits=0)

            rem_sec = daily_sleep.get("remSleepSeconds")
            if rem_sec is None:
                rem_sec = _find_first_value(sleep_data, ["remSleepSeconds", "remSleepDurationInSeconds"])
            if rem_sec is not None:
                rem_sleep_min = _to_float(float(rem_sec) / 60.0, ndigits=0)

            light_sec = daily_sleep.get("lightSleepSeconds")
            if light_sec is None:
                light_sec = _find_first_value(sleep_data, ["lightSleepSeconds", "lightSleepDurationInSeconds"])
            if light_sec is not None:
                light_sleep_min = _to_float(float(light_sec) / 60.0, ndigits=0)

            awake_sec = daily_sleep.get("awakeSleepSeconds")
            if awake_sec is None:
                awake_sec = _find_first_value(sleep_data, ["awakeSleepSeconds", "awakeSleepDurationInSeconds"])
            if awake_sec is not None:
                awake_sleep_min = _to_float(float(awake_sec) / 60.0, ndigits=0)

            if avg_respiration is None:
                avg_respiration = _find_first_value(
                    daily_sleep, ["averageRespirationValue", "avgRespirationValue"]
                ) or _find_first_value(sleep_data, ["averageRespirationValue", "avgRespirationValue"])
            if avg_spo2 is None:
                avg_spo2 = _find_first_value(
                    daily_sleep, ["averageSpO2Value", "averageSpo2", "avgSpo2"]
                ) or _find_first_value(sleep_data, ["averageSpO2Value", "averageSpo2", "avgSpo2"])
        except Exception:
            pass

        rl_info = _fetch_readiness_and_load(client, d_str)

        rows.append(
            {
                "date": d_str,
                "resting_hr": resting_hr,
                "min_hr": _to_float(min_hr, ndigits=0),
                "max_hr": _to_float(max_hr, ndigits=0),
                "avg_stress": avg_stress,
                "max_stress": _to_float(max_stress, ndigits=0),
                "rest_stress_sec": _to_float(rest_stress_sec, ndigits=0),
                "body_battery_high": bb_high,
                "body_battery_low": bb_low,
                "body_battery_charged": _to_float(bb_charged, ndigits=0),
                "body_battery_drained": _to_float(bb_drained, ndigits=0),
                "body_battery_current": bb_current,
                "hrv_last_night_avg": hrv_avg,
                "hrv_weekly_avg": _to_float(hrv_weekly_avg, ndigits=0),
                "hrv_status": hrv_status,
                "hrv_baseline_low": hrv_base_low,
                "hrv_baseline_high": hrv_base_high,
                "sleep_score": sleep_score,
                "sleep_hours": sleep_hours,
                "deep_sleep_min": deep_sleep_min,
                "rem_sleep_min": rem_sleep_min,
                "light_sleep_min": light_sleep_min,
                "awake_sleep_min": awake_sleep_min,
                "avg_respiration": _to_float(avg_respiration, ndigits=1),
                "avg_spo2": _to_float(avg_spo2, ndigits=0),
                "active_kcal": _to_float(active_kcal, ndigits=0),
                "total_kcal": _to_float(total_kcal, ndigits=0),
                "steps": steps,
                "training_readiness_score": rl_info["training_readiness_score"],
                "training_readiness_level": rl_info["training_readiness_level"],
                "training_status": rl_info["training_status"],
                "acute_load": rl_info["acute_load"],
                "chronic_load": rl_info["chronic_load"],
                "acwr": rl_info["acwr"],
                "training_load_low_aerobic": rl_info["training_load_low_aerobic"],
                "training_load_high_aerobic": rl_info["training_load_high_aerobic"],
                "training_load_anaerobic": rl_info["training_load_anaerobic"],
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


# ==============================================================================
# Strava 확장 수집 헬퍼 (세그먼트 PR/업힐 기록, 경사도별 파워, 존 데이터, RPE)
# ==============================================================================

def _strava_get(
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
) -> Optional[Any]:
    """Strava API GET 요청 래퍼 (429 Rate Limit 및 권한/미존재 오류 안전 처리)"""
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code == 429:
            print("[WARN] Strava API Rate Limit(429) 도달 — 추가 상세 조회를 일시 중단합니다.", flush=True)
            return None
        if resp.status_code in (401, 403, 404):
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] Strava GET 실패 ({url}): {e}", flush=True)
        return None


def _parse_strava_zones(zones_payload: Any) -> Dict[str, Any]:
    """Strava /activities/{id}/zones 응답에서 파워 존(Z1~Z5+) 및 심박 존(HRZ1~HRZ5) 체류 시간(분) 추출"""
    out: Dict[str, Any] = {
        "strava_pz1_min": None,
        "strava_pz2_min": None,
        "strava_pz3_min": None,
        "strava_pz4_min": None,
        "strava_pz5_plus_min": None,
        "strava_hrz1_min": None,
        "strava_hrz2_min": None,
        "strava_hrz3_min": None,
        "strava_hrz4_min": None,
        "strava_hrz5_min": None,
    }
    if not isinstance(zones_payload, list):
        return out

    for z_group in zones_payload:
        if not isinstance(z_group, dict):
            continue
        z_type = str(z_group.get("type", "")).lower()
        buckets = z_group.get("distribution_buckets") or []
        if not buckets:
            continue

        mins = [round((float(b.get("time") or 0)) / 60.0, 1) for b in buckets]
        if z_type == "power":
            if len(mins) >= 1:
                out["strava_pz1_min"] = mins[0]
            if len(mins) >= 2:
                out["strava_pz2_min"] = mins[1]
            if len(mins) >= 3:
                out["strava_pz3_min"] = mins[2]
            if len(mins) >= 4:
                out["strava_pz4_min"] = mins[3]
            if len(mins) >= 5:
                out["strava_pz5_plus_min"] = round(sum(mins[4:]), 1)
        elif z_type == "heartrate":
            if len(mins) >= 1:
                out["strava_hrz1_min"] = mins[0]
            if len(mins) >= 2:
                out["strava_hrz2_min"] = mins[1]
            if len(mins) >= 3:
                out["strava_hrz3_min"] = mins[2]
            if len(mins) >= 4:
                out["strava_hrz4_min"] = mins[3]
            if len(mins) >= 5:
                out["strava_hrz5_min"] = round(sum(mins[4:]), 1)

    return out


def _compute_grade_power_metrics(streams_payload: Any) -> Dict[str, Any]:
    """
    Strava /activities/{id}/streams (watts, grade_smooth, moving)에서
    경사도 구간별 평균 파워(평지 <2%, 완경사 2~5%, 업힐 >=5%) 및 최대 경사도 산출
    """
    out: Dict[str, Any] = {
        "max_grade_pct": None,
        "flat_avg_watts": None,        # 경사도 < 2.0%
        "false_flat_avg_watts": None,  # 경사도 2.0% ~ < 5.0%
        "climb_avg_watts": None,       # 경사도 >= 5.0%
        "climb_time_min": None,        # 경사도 >= 5.0% 누적 시간(분)
    }
    if not isinstance(streams_payload, dict):
        return out

    watts_data = (streams_payload.get("watts") or {}).get("data")
    grade_data = (streams_payload.get("grade_smooth") or {}).get("data")
    moving_data = (streams_payload.get("moving") or {}).get("data")

    if not watts_data or not grade_data:
        return out

    n = min(len(watts_data), len(grade_data))
    if n < 30:
        return out

    w_arr = np.array([float(v) if v is not None else np.nan for v in watts_data[:n]], dtype=float)
    g_arr = np.array([float(v) if v is not None else np.nan for v in grade_data[:n]], dtype=float)

    if moving_data and len(moving_data) >= n:
        m_arr = np.array([bool(v) for v in moving_data[:n]], dtype=bool)
    else:
        m_arr = np.ones(n, dtype=bool)

    valid = np.isfinite(w_arr) & np.isfinite(g_arr) & m_arr & (w_arr > 0)
    if int(np.sum(valid)) < 20:
        return out

    g_valid = g_arr[valid]
    w_valid = w_arr[valid]

    sane = (g_valid >= -35.0) & (g_valid <= 35.0)
    if int(np.sum(sane)) < 20:
        return out

    g_sane = g_valid[sane]
    w_sane = w_valid[sane]

    out["max_grade_pct"] = round(float(np.max(g_sane)), 1)

    flat_mask = (g_sane >= -2.0) & (g_sane < 2.0)
    mod_mask = (g_sane >= 2.0) & (g_sane < 5.0)
    climb_mask = g_sane >= 5.0

    if int(np.sum(flat_mask)) >= 15:
        out["flat_avg_watts"] = round(float(np.mean(w_sane[flat_mask])), 1)
    if int(np.sum(mod_mask)) >= 15:
        out["false_flat_avg_watts"] = round(float(np.mean(w_sane[mod_mask])), 1)
    if int(np.sum(climb_mask)) >= 10:
        out["climb_avg_watts"] = round(float(np.mean(w_sane[climb_mask])), 1)
        out["climb_time_min"] = round(float(np.sum(climb_mask)) / 60.0, 1)

    return out


def _extract_segment_efforts(
    activity_id: int,
    activity_date: str,
    activity_name: str,
    detail_payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Strava 상세 활동의 segment_efforts에서 세그먼트 PR/업힐 기록 추출"""
    efforts = detail_payload.get("segment_efforts") or []
    seg_rows: List[Dict[str, Any]] = []
    pr_names: List[str] = []
    best_climb_summary: Optional[str] = None
    best_climb_score = -1.0

    for eff in efforts:
        if not isinstance(eff, dict):
            continue
        seg = eff.get("segment") or {}
        effort_id = eff.get("id")
        if effort_id is None:
            continue

        seg_name = str(eff.get("name") or seg.get("name") or "")
        dist_m = float(eff.get("distance") or seg.get("distance") or 0.0)
        elapsed_sec = int(eff.get("elapsed_time") or 0)
        moving_sec = int(eff.get("moving_time") or elapsed_sec)
        avg_grade = _to_float(seg.get("average_grade"), ndigits=1)
        max_grade = _to_float(seg.get("maximum_grade"), ndigits=1)
        elev_high = _to_float(seg.get("elevation_high"), ndigits=1)
        elev_low = _to_float(seg.get("elevation_low"), ndigits=1)
        elev_diff = round(elev_high - elev_low, 1) if (elev_high is not None and elev_low is not None) else None
        climb_cat = seg.get("climb_category")
        avg_watts = _to_float(eff.get("average_watts"), ndigits=1)
        avg_hr = _to_float(eff.get("average_heartrate"), ndigits=1)
        max_hr = _to_float(eff.get("max_heartrate"), ndigits=0)
        avg_cad = _to_float(eff.get("average_cadence"), ndigits=1)
        pr_rank = eff.get("pr_rank")
        kom_rank = eff.get("kom_rank")

        is_pr = pr_rank == 1
        if is_pr and seg_name:
            pr_names.append(seg_name)

        is_climb = bool((avg_grade is not None and avg_grade >= 3.0) or (climb_cat and int(climb_cat) >= 1))
        if is_climb and elapsed_sec > 0:
            score = dist_m * max(avg_grade or 1.0, 1.0)
            if score > best_climb_score:
                best_climb_score = score
                w_str = f" · {avg_watts}W" if avg_watts is not None else ""
                best_climb_summary = f"{seg_name} ({round(dist_m/1000, 2)}km/{avg_grade}% · {elapsed_sec}s{w_str})"

        seg_rows.append(
            {
                "effort_id": int(effort_id),
                "activity_id": int(activity_id),
                "date": activity_date,
                "activity_name": activity_name,
                "segment_id": seg.get("id"),
                "segment_name": seg_name,
                "distance_km": round(dist_m / 1000.0, 2),
                "elapsed_time_sec": elapsed_sec,
                "moving_time_sec": moving_sec,
                "avg_grade_pct": avg_grade,
                "max_grade_pct": max_grade,
                "elev_gain_m": elev_diff,
                "climb_category": climb_cat,
                "avg_watts": avg_watts,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "avg_cadence": avg_cad,
                "pr_rank": pr_rank,
                "kom_rank": kom_rank,
                "is_pr": is_pr,
            }
        )

    summary_cols = {
        "segment_count": len(seg_rows),
        "segment_pr_count": len(pr_names),
        "segment_pr_names": " | ".join(pr_names) if pr_names else None,
        "key_climb_segment": best_climb_summary,
    }
    return seg_rows, summary_cols


def fetch_strava_activities(
    per_page: int = 100,
    detail_limit: int = STRAVA_DETAIL_LIMIT,
) -> pd.DataFrame:
    """Strava API에서 활동 이력 + 확장 지표(RPE, 세그먼트 PR/업힐, 경사도별 파워, 존 데이터) 수집 및 누적 저장"""
    if not (STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REFRESH_TOKEN):
        print("[INFO] Strava 환경변수가 설정되지 않아 Strava 수집을 건너뜁니다.", flush=True)
        return pd.DataFrame()

    csv_path = DATA_DIR / "strava_activities.csv"
    seg_csv_path = DATA_DIR / "strava_segments.csv"
    existing_df = pd.read_csv(csv_path) if csv_path.exists() else pd.DataFrame()
    existing_seg_df = pd.read_csv(seg_csv_path) if seg_csv_path.exists() else pd.DataFrame()

    existing_ids_with_detail = set()
    if not existing_df.empty and "detail_synced" in existing_df.columns:
        synced_rows = existing_df[existing_df["detail_synced"] == True]  # noqa: E712
        existing_ids_with_detail = set(synced_rows["id"].astype(str).tolist())

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
    auth_headers = {"Authorization": f"Bearer {access_token}"}

    act_resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=auth_headers,
        params={"per_page": per_page, "page": 1},
        timeout=20,
    )
    act_resp.raise_for_status()
    items = act_resp.json()

    rows: List[Dict[str, Any]] = []
    all_segment_rows: List[Dict[str, Any]] = []
    detail_fetched_count = 0

    for it in items:
        if it.get("type") not in ("Ride", "VirtualRide"):
            continue

        act_id = int(it["id"])
        act_date = str(it.get("start_date_local") or "")
        act_name = str(it.get("name") or "")
        is_trainer = bool(it.get("trainer"))

        row: Dict[str, Any] = {
            "id": act_id,
            "date": act_date,
            "name": act_name,
            "type": it.get("type"),
            "trainer": is_trainer,
            "distance_km": round((it.get("distance") or 0) / 1000, 2),
            "moving_time_min": round((it.get("moving_time") or 0) / 60, 1),
            "elevation_gain_m": round(it.get("total_elevation_gain") or 0, 1),
            "avg_speed_kph": round((it.get("average_speed") or 0) * 3.6, 1),
            "max_speed_kph": round((it.get("max_speed") or 0) * 3.6, 1) if it.get("max_speed") else None,
            "avg_watts": it.get("average_watts"),
            "weighted_avg_watts_NP": it.get("weighted_average_watts"),
            "max_watts": it.get("max_watts"),
            "kilojoules": it.get("kilojoules"),
            "calories_kcal": it.get("calories"),
            "avg_cadence": it.get("average_cadence"),
            "avg_hr": it.get("average_heartrate"),
            "max_hr": it.get("max_heartrate"),
            "suffer_score": it.get("suffer_score"),
            "perceived_exertion_rpe": it.get("perceived_exertion"),
            "segment_count": None,
            "segment_pr_count": None,
            "segment_pr_names": None,
            "key_climb_segment": None,
            "max_grade_pct": None,
            "flat_avg_watts": None,
            "false_flat_avg_watts": None,
            "climb_avg_watts": None,
            "climb_time_min": None,
            "strava_pz1_min": None,
            "strava_pz2_min": None,
            "strava_pz3_min": None,
            "strava_pz4_min": None,
            "strava_pz5_plus_min": None,
            "strava_hrz1_min": None,
            "strava_hrz2_min": None,
            "strava_hrz3_min": None,
            "strava_hrz4_min": None,
            "strava_hrz5_min": None,
            "detail_synced": False,
        }

        need_detail = (str(act_id) not in existing_ids_with_detail) and (detail_fetched_count < detail_limit)
        if need_detail:
            detail_data = _strava_get(
                f"https://www.strava.com/api/v3/activities/{act_id}",
                headers=auth_headers,
                params={"include_all_efforts": "true"},
            )
            if isinstance(detail_data, dict):
                detail_fetched_count += 1
                row["detail_synced"] = True

                # 1) RPE (주관적 운동 자각도 1~10) & 칼로리 보완
                if detail_data.get("perceived_exertion") is not None:
                    row["perceived_exertion_rpe"] = _to_float(detail_data.get("perceived_exertion"), ndigits=1)
                if detail_data.get("calories") is not None and row["calories_kcal"] is None:
                    row["calories_kcal"] = _to_float(detail_data.get("calories"), ndigits=0)

                # 2) 세그먼트 PR / 업힐 기록 추출
                seg_rows, seg_summary = _extract_segment_efforts(
                    activity_id=act_id,
                    activity_date=act_date,
                    activity_name=act_name,
                    detail_payload=detail_data,
                )
                all_segment_rows.extend(seg_rows)
                row.update(seg_summary)

                # 3) Strava 파워/심박 존 분포 조회
                zones_data = _strava_get(
                    f"https://www.strava.com/api/v3/activities/{act_id}/zones",
                    headers=auth_headers,
                )
                if zones_data is not None:
                    row.update(_parse_strava_zones(zones_data))

                # 4) 야외 라이딩 경사도 구간별 파워 스트림 분석
                if not is_trainer and (it.get("total_elevation_gain") or 0) > 0:
                    streams_data = _strava_get(
                        f"https://www.strava.com/api/v3/activities/{act_id}/streams",
                        headers=auth_headers,
                        params={
                            "keys": "watts,grade_smooth,moving",
                            "key_by_type": "true",
                        },
                    )
                    if streams_data is not None:
                        row.update(_compute_grade_power_metrics(streams_data))

                time.sleep(0.35)

        rows.append(row)

    new_df = pd.DataFrame(rows)
    if not existing_df.empty and not new_df.empty:
        combined = _merge_prefer_non_null(existing_df, new_df, "id")
    else:
        combined = new_df if not new_df.empty else existing_df

    if not combined.empty:
        combined = combined.sort_values("date", ascending=False).reset_index(drop=True)
        combined.to_csv(csv_path, index=False, encoding="utf-8-sig")

    if all_segment_rows:
        new_seg_df = pd.DataFrame(all_segment_rows)
        if not existing_seg_df.empty:
            combined_seg = _merge_prefer_non_null(existing_seg_df, new_seg_df, "effort_id")
        else:
            combined_seg = new_seg_df
        combined_seg = combined_seg.sort_values(["date", "effort_id"], ascending=[False, True]).reset_index(drop=True)
        combined_seg.to_csv(seg_csv_path, index=False, encoding="utf-8-sig")
        print(f"[OK] Strava 세그먼트 기록: {len(combined_seg)}건 저장 완료 ({seg_csv_path.name})", flush=True)

    return combined


def main() -> None:
    print(
        f"=== Fitness 데이터 수집 시작 (FTP={FTP_WATTS}W, LTHR={LTHR_BPM}bpm, HRmax={MAX_HR_BPM}bpm) ===",
        flush=True,
    )
    garmin_client = init_garmin()

    act_df = fetch_garmin_activities(garmin_client, limit=ACTIVITY_LIMIT)
    fit_count = len(list(FIT_DIR.glob("*.fit")))
    print(f"[OK] Garmin 활동: {len(act_df)}건 / 누적 FIT 파일: {fit_count}개", flush=True)

    health_df = fetch_garmin_health(garmin_client, days=WELLNESS_DAYS)
    print(f"[OK] Garmin 웰니스: {len(health_df)}일치 저장 완료", flush=True)

    strava_df = fetch_strava_activities(per_page=100, detail_limit=STRAVA_DETAIL_LIMIT)
    if not strava_df.empty:
        print(f"[OK] Strava 활동: {len(strava_df)}건 저장 완료", flush=True)

    print("=== 수집 완료 ===", flush=True)


if __name__ == "__main__":
    main()
