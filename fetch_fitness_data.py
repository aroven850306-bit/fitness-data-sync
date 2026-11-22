import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta

# GitHub Secrets에서 환경 변수 불러오기 (앞뒤 공백 자동 제거)
STRAVA_CLIENT_ID = (os.environ.get("STRAVA_CLIENT_ID") or "").strip()
STRAVA_CLIENT_SECRET = (os.environ.get("STRAVA_CLIENT_SECRET") or "").strip()
STRAVA_REFRESH_TOKEN = (os.environ.get("STRAVA_REFRESH_TOKEN") or "").strip()
GARMIN_API_KEY = (os.environ.get("GARMIN_API_KEY") or "").strip()


def fetch_strava_data():
    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        print("⚠️ 스트라바 API 키가 모두 설정되지 않아 건너뜁니다.", flush=True)
        return

    auth_url = "https://www.strava.com/oauth/token"
    payload = {
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    res = requests.post(auth_url, data=payload).json()
    access_token = res.get("access_token")

    if not access_token:
        print("❌ 스트라바 토큰 발급 실패:", res, flush=True)
        return

    activities_url = "https://www.strava.com/api/v3/athlete/activities"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"per_page": 100, "page": 1}
    activities = requests.get(activities_url, headers=headers, params=params).json()

    if not isinstance(activities, list):
        print("❌ 스트라바 데이터 조회 실패:", activities, flush=True)
        return

    ride_list = []
    for act in activities:
        if act.get("type") in ["Ride", "VirtualRide"]:
            ride_list.append({
                "id": act.get("id"),
                "date": act.get("start_date_local"),
                "name": act.get("name"),
                "type": act.get("type"),
                "distance_km": round(act.get("distance", 0) / 1000, 2),
                "moving_time_min": round(act.get("moving_time", 0) / 60, 1),
                "elevation_gain_m": act.get("total_elevation_gain"),
                "avg_speed_kph": round(act.get("average_speed", 0) * 3.6, 1),
                "avg_watts": act.get("average_watts"),
                "weighted_avg_watts_NP": act.get("weighted_average_watts"),
                "max_watts": act.get("max_watts"),
                "kilojoules": act.get("kilojoules"),
                "avg_cadence": act.get("average_cadence"),
                "avg_hr": act.get("average_heartrate"),
                "max_hr": act.get("max_heartrate"),
                "suffer_score": act.get("suffer_score"),
            })

    df_strava = pd.DataFrame(ride_list)
    df_strava.to_csv("strava_activities.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 스트라바 라이딩 데이터 {len(df_strava)}건 저장 완료.", flush=True)


def get_garmin_access_token(api_key):
    token_url = "https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token"
    headers = {
        "Authorization": f"ApiKey {api_key}",
    }
    res = requests.post(token_url, headers=headers)
    print(f"🔍 가민 토큰 발급 응답 코드: {res.status_code}", flush=True)
    if res.status_code != 200:
        print("❌ 가민 Access Token 발급 실패 상세:", res.text, flush=True)
        return None

    # 순수 문자열(text/plain) 또는 JSON 응답 모두 안전하게 처리
    raw_text = res.text.strip().strip('"')
    if raw_text.startswith("{"):
        data = res.json()
        return data.get("accessToken") or data.get("access_token") or data.get("token")
    return raw_text


def fetch_garmin_data():
    if not GARMIN_API_KEY:
        print("⚠️ GARMIN_API_KEY가 설정되지 않아 건너뜁니다.", flush=True)
        return

    access_token = get_garmin_access_token(GARMIN_API_KEY)
    if not access_token:
        return
    print("✅ 가민 Personal API Access Token 발급 성공!", flush=True)

    auth_headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    # 가능한 가민 Personal API / Connect API 경로 순차 확인
    base_candidates = [
        "https://personal.connectapi.garmin.com",
        "https://connectapi.garmin.com",
    ]
    
    act_res = None
    working_base = None
    for base_url in base_candidates:
        url = f"{base_url}/activitylist-service/activities/search/activities"
        res = requests.get(url, headers=auth_headers, params={"start": 0, "limit": 30})
        print(f"🔍 경로 테스트 [{base_url}] -> 상태 코드: {res.status_code}", flush=True)
        if res.status_code == 200:
            act_res = res
            working_base = base_url
            break
        else:
            print(f"   응답 내용: {res.text[:150]}", flush=True)

    if act_res is None:
        print("❌ 가민 활동 목록 조회에 모두 실패했습니다.", flush=True)
        return

    print(f"✅ 가민 활동 데이터 연결 성공! ({working_base})", flush=True)
    activities = act_res.json()
    garmin_act_list = []
    os.makedirs("fit_files", exist_ok=True)
    fit_download_count = 0

    for act in activities:
        act_type = act.get("activityType", {}).get("typeKey", "")
        if "cycling" in act_type or "biking" in act_type or "ride" in act_type:
            act_id = act.get("activityId")
            start_local = act.get("startTimeLocal", "")
            date_prefix = start_local[:10] if start_local else "unknown"

            garmin_act_list.append({
                "activity_id": act_id,
                "date": start_local,
                "name": act.get("activityName"),
                "type": act_type,
                "distance_km": round(act.get("distance", 0) / 1000, 2),
                "duration_min": round(act.get("duration", 0) / 60, 1),
                "elevation_gain_m": act.get("elevationGain"),
                "avg_power": act.get("avgPower"),
                "norm_power_NP": act.get("normPower"),
                "max_power": act.get("maxPower"),
                "max_20min_power": act.get("max20MinPower"),
                "training_stress_score_TSS": act.get("trainingStressScore"),
                "intensity_factor_IF": act.get("intensityFactor"),
                "avg_hr": act.get("averageHR"),
                "max_hr": act.get("maxHR"),
                "avg_cadence": act.get("averageBikingCadenceInRevPerMinute"),
                "left_right_balance": act.get("leftRightBalance"),
                "aerobic_training_effect": act.get("aerobicTrainingEffect"),
                "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
                "vo2_max": act.get("vO2MaxValue"),
            })

            # 최신 5개 사이클링 활동의 원본 FIT 파일 다운로드 및 압축 해제
            if fit_download_count < 5 and act_id:
                fit_candidates = [
                    f"{working_base}/download-service/files/activity/{act_id}",
                    f"{working_base}/personal-api/rest/activities/{act_id}/fit",
                    f"https://connectapi.garmin.com/download-service/files/activity/{act_id}",
                ]
                fit_headers = {"Authorization": f"Bearer {access_token}"}
                for fit_url in fit_candidates:
                    fit_res = requests.get(fit_url, headers=fit_headers)
                    if fit_res.status_code == 200 and len(fit_res.content) > 100:
                        if zipfile.is_zipfile(io.BytesIO(fit_res.content)):
                            with zipfile.ZipFile(io.BytesIO(fit_res.content)) as zf:
                                for member in zf.namelist():
                                    if member.lower().endswith(".fit"):
                                        fit_filename = f"fit_files/{date_prefix}_{act_id}.fit"
                                        with open(fit_filename, "wb") as f:
                                            f.write(zf.read(member))
                                        fit_download_count += 1
                                        print(f"📥 원본 FIT(ZIP) 저장 완료: {fit_filename}", flush=True)
                        else:
                            fit_filename = f"fit_files/{date_prefix}_{act_id}.fit"
                            with open(fit_filename, "wb") as f:
                                f.write(fit_res.content)
                            fit_download_count += 1
                            print(f"📥 원본 FIT 저장 완료: {fit_filename}", flush=True)
                        break
                    else:
                        print(f"⚠️ FIT 경로 시도 [{fit_url}] -> 상태 코드: {fit_res.status_code}", flush=True)

    df_garmin_acts = pd.DataFrame(garmin_act_list)
    df_garmin_acts.to_csv("garmin_activities.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 가민 활동 {len(df_garmin_acts)}건 및 원본 FIT {fit_download_count}개 저장 완료.", flush=True)

    # 최근 14일치 일별 건강/회복/HRV 지표 수집
    health_list = []
    for i in range(14):
        target_date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_res = requests.get(f"{working_base}/usersummary-service/usersummary/daily/{target_date}", headers=auth_headers)
        hrv_res = requests.get(f"{working_base}/hrv-service/hrv/{target_date}", headers=auth_headers)
        sleep_res = requests.get(f"{working_base}/wellness-service/wellness/dailySleepData/{target_date}", headers=auth_headers)

        daily = daily_res.json() if daily_res.status_code == 200 else {}
        hrv = hrv_res.json() if hrv_res.status_code == 200 else {}
        sleep = sleep_res.json() if sleep_res.status_code == 200 else {}

        hrv_summary = hrv.get("hrvSummary", {}) if isinstance(hrv, dict) else {}
        sleep_dto = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}
        sleep_scores = sleep_dto.get("sleepScores", {}).get("overall", {}) if isinstance(sleep_dto, dict) else {}

        health_list.append({
            "date": target_date,
            "resting_hr": daily.get("restingHeartRate"),
            "avg_stress": daily.get("averageStressLevel"),
            "body_battery_high": daily.get("bodyBatteryHighestValue"),
            "body_battery_low": daily.get("bodyBatteryLowestValue"),
            "hrv_last_night_avg": hrv_summary.get("lastNightAvg"),
            "hrv_status": hrv_summary.get("status"),
            "sleep_score": sleep_scores.get("value"),
            "sleep_hours": round(sleep_dto.get("sleepTimeSeconds", 0) / 3600, 1) if sleep_dto.get("sleepTimeSeconds") else None,
            "steps": daily.get("totalSteps"),
        })

    df_garmin_health = pd.DataFrame(health_list)
    df_garmin_health.to_csv("garmin_health.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 가민 건강/HRV 데이터 {len(df_garmin_health)}건 저장 완료.", flush=True)


if __name__ == "__main__":
    print("🚀 피트니스 데이터 및 원본 FIT 수집을 시작합니다...", flush=True)
    fetch_strava_data()
    fetch_garmin_data()
    print("🎉 모든 작업이 완료되었습니다.", flush=True)
