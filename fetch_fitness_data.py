import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta
from garminconnect import Garmin

# GitHub Secrets에서 환경 변수 불러오기
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")


def fetch_strava_data():
    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        print("⚠️ 스트라바 API 키가 모두 설정되지 않아 건너뜁니다.")
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
        print("❌ 스트라바 토큰 발급 실패:", res)
        return

    activities_url = "https://www.strava.com/api/v3/athlete/activities"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"per_page": 100, "page": 1}
    activities = requests.get(activities_url, headers=headers, params=params).json()

    if not isinstance(activities, list):
        print("❌ 스트라바 데이터 조회 실패:", activities)
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
                "elapsed_time_min": round(act.get("elapsed_time", 0) / 60, 1),
                "elevation_gain_m": act.get("total_elevation_gain"),
                "avg_speed_kph": round(act.get("average_speed", 0) * 3.6, 1),
                "max_speed_kph": round(act.get("max_speed", 0) * 3.6, 1),
                "avg_watts": act.get("average_watts"),
                "weighted_avg_watts_NP": act.get("weighted_average_watts"),
                "max_watts": act.get("max_watts"),
                "kilojoules": act.get("kilojoules"),
                "avg_cadence": act.get("average_cadence"),
                "avg_hr": act.get("average_heartrate"),
                "max_hr": act.get("max_heartrate"),
                "suffer_score": act.get("suffer_score"),
                "device_watts": act.get("device_watts"),
            })

    df_strava = pd.DataFrame(ride_list)
    df_strava.to_csv("strava_activities.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 스트라바 라이딩 데이터 {len(df_strava)}건 저장 완료.")


def fetch_garmin_data():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        print("⚠️ 가민 계정 정보가 설정되지 않아 건너뜁니다.")
        return

    try:
        client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        client.login()
        print("✅ 가민 커넥트 로그인 성공")

        # ---------------------------------------------------------
        # 1. 일별 건강 / 회복 / 훈련 부하 지표 (최근 30일)
        # ---------------------------------------------------------
        health_list = []
        for i in range(30):
            target_date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            
            stats = client.get_stats(target_date) or {}
            hrv = client.get_hrv_data(target_date) or {}
            sleep = client.get_sleep_data(target_date) or {}
            training = client.get_training_status(target_date) or {}
            readiness = client.get_training_readiness(target_date) or []

            hrv_summary = hrv.get("hrvSummary", {}) if isinstance(hrv, dict) else {}
            sleep_dto = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}
            sleep_scores = sleep_dto.get("sleepScores", {}).get("overall", {}) if isinstance(sleep_dto, dict) else {}
            
            # 훈련 상태 및 급성/만성 부하 추출
            most_recent_status = training.get("mostRecentTrainingStatus", {}) if isinstance(training, dict) else {}
            latest_status_map = most_recent_status.get("latestTrainingStatusData", {}) if isinstance(most_recent_status, dict) else {}
            first_device_status = next(iter(latest_status_map.values()), {}) if isinstance(latest_status_map, dict) else {}
            acute_dto = first_device_status.get("acuteTrainingLoadDTO", {}) if isinstance(first_device_status, dict) else {}

            # 훈련 준비도 (당일 첫 번째 기록)
            readiness_item = readiness[0] if isinstance(readiness, list) and len(readiness) > 0 else {}

            health_list.append({
                "date": target_date,
                "resting_hr": stats.get("restingHeartRate"),
                "min_hr": stats.get("minHeartRate"),
                "max_hr": stats.get("maxHeartRate"),
                "avg_stress": stats.get("averageStressLevel"),
                "max_stress": stats.get("maxStressLevel"),
                "body_battery_high": stats.get("bodyBatteryHighestValue"),
                "body_battery_low": stats.get("bodyBatteryLowestValue"),
                "body_battery_charged": stats.get("bodyBatteryChargedValue"),
                "body_battery_drained": stats.get("bodyBatteryDrainedValue"),
                "hrv_last_night_avg": hrv_summary.get("lastNightAvg"),
                "hrv_weekly_avg": hrv_summary.get("weeklyAvg"),
                "hrv_status": hrv_summary.get("status"),
                "sleep_score": sleep_scores.get("value"),
                "sleep_total_hr": round(sleep_dto.get("sleepTimeSeconds", 0) / 3600, 2) if sleep_dto.get("sleepTimeSeconds") else None,
                "sleep_deep_min": round(sleep_dto.get("deepSleepSeconds", 0) / 60, 1) if sleep_dto.get("deepSleepSeconds") else None,
                "sleep_light_min": round(sleep_dto.get("lightSleepSeconds", 0) / 60, 1) if sleep_dto.get("lightSleepSeconds") else None,
                "sleep_rem_min": round(sleep_dto.get("remSleepSeconds", 0) / 60, 1) if sleep_dto.get("remSleepSeconds") else None,
                "sleep_awake_min": round(sleep_dto.get("awakeSleepSeconds", 0) / 60, 1) if sleep_dto.get("awakeSleepSeconds") else None,
                "sleep_avg_spo2": sleep_dto.get("averageSpO2Value"),
                "sleep_avg_respiration": sleep_dto.get("averageRespirationValue"),
                "training_readiness_score": readiness_item.get("score"),
                "training_readiness_level": readiness_item.get("level"),
                "training_status": first_device_status.get("trainingStatusFeedbackPhrase"),
                "acute_load": acute_dto.get("dailyTrainingLoadAcute"),
                "chronic_load": acute_dto.get("dailyTrainingLoadChronic"),
                "acwr_ratio": acute_dto.get("dailyAcuteChronicWorkloadRatio"),
                "active_kcal": stats.get("activeKilocalories"),
                "steps": stats.get("totalSteps"),
            })

        df_garmin = pd.DataFrame(health_list)
        df_garmin.to_csv("garmin_health.csv", index=False, encoding="utf-8-sig")
        print(f"✅ 가민 건강/회복/부하 데이터 {len(df_garmin)}건 저장 완료.")

        # ---------------------------------------------------------
        # 2. 가민 사이클링 활동 상세 요약 & 원본 FIT 파일 다운로드
        # ---------------------------------------------------------
        activities = client.get_activities(0, 30)  # 최근 30개 활동 조회
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
                    "max_cadence": act.get("maxBikingCadenceInRevPerMinute"),
                    "left_right_balance": act.get("leftRightBalance"),
                    "aerobic_training_effect": act.get("aerobicTrainingEffect"),
                    "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
                    "vo2_max": act.get("vO2MaxValue"),
                    "calories": act.get("calories"),
                })

                # 최신 5개 사이클링 활동의 원본 FIT 파일 다운로드 및 압축 해제
                if fit_download_count < 5 and act_id:
                    try:
                        zip_bytes = client.download_activity(
                            act_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
                        )
                        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                            for member in zf.namelist():
                                if member.lower().endswith(".fit"):
                                    fit_filename = f"fit_files/{date_prefix}_{act_id}.fit"
                                    with open(fit_filename, "wb") as f:
                                        f.write(zf.read(member))
                                    fit_download_count += 1
                                    print(f"📥 원본 FIT 저장 완료: {fit_filename}")
                    except Exception as fit_err:
                        print(f"⚠️ FIT 다운로드 실패 (ID {act_id}):", fit_err)

        df_garmin_acts = pd.DataFrame(garmin_act_list)
        df_garmin_acts.to_csv("garmin_activities.csv", index=False, encoding="utf-8-sig")
        print(f"✅ 가민 사이클링 활동 {len(df_garmin_acts)}건 및 원본 FIT {fit_download_count}개 저장 완료.")

    except Exception as e:
        print("❌ 가민 데이터 수집 실패:", e)


if __name__ == "__main__":
    print("🚀 피트니스 전체 데이터 및 FIT 수집을 시작합니다...")
    fetch_strava_data()
    fetch_garmin_data()
    print("🎉 모든 작업이 완료되었습니다.")
