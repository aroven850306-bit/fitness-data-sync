import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta

# GitHub Secrets에서 환경 변수 불러오기
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET")
STRAVA_REFRESH_TOKEN = os.environ.get("STRAVA_REFRESH_TOKEN")
GARMIN_API_KEY = os.environ.get("GARMIN_API_KEY")


def fetch_strava_data():
    if not all([STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN]):
        print("⚠️ 스트라바 API 키가 모두 설정되지 않아 건너뜁니다.")
        return

    # 1. Access Token 갱신
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

    # 2. 최근 활동 100개 가져오기
    activities_url = "https://www.strava.com/api/v3/athlete/activities"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"per_page": 100, "page": 1}
    activities = requests.get(activities_url, headers=headers, params=params).json()

    if not isinstance(activities, list):
        print("❌ 스트라바 데이터 조회 실패:", activities)
        return

    # 3. 라이딩 데이터 필터링 및 정리
    ride_list = []
    for act in activities:
        if act.get("type") in ["Ride", "VirtualRide"]:
            ride_list.append({
                "date": act.get("start_date_local"),
                "name": act.get("name"),
                "type": act.get("type"),
                "distance_km": round(act.get("distance", 0) / 1000, 2),
                "moving_time_min": round(act.get("moving_time", 0) / 60, 1),
                "avg_watts": act.get("average_watts"),
                "weighted_avg_watts_NP": act.get("weighted_average_watts"),
                "max_watts": act.get("max_watts"),
                "avg_hr": act.get("average_heartrate"),
                "max_hr": act.get("max_heartrate"),
                "suffer_score": act.get("suffer_score"),
            })

    df_strava = pd.DataFrame(ride_list)
    df_strava.to_csv("strava_activities.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 스트라바 라이딩 데이터 {len(df_strava)}건 저장 완료.")


def fetch_garmin_data():
    if not GARMIN_API_KEY:
        print("⚠️ 가민 API 키가 설정되지 않아 건너뜁니다.")
        return

    base_url = "https://apis.garmin.com/wellness-api/rest"
    headers = {
        "Authorization": f"Bearer {GARMIN_API_KEY}",
        "Accept": "application/json",
    }

    # 최근 14일치 데이터 조회 범위 설정 (Unix Timestamp)
    end_time = int(time.time())
    start_time = int((datetime.now() - timedelta(days=14)).timestamp())
    params = {
        "uploadStartTimeInSeconds": start_time,
        "uploadEndTimeInSeconds": end_time,
    }

    # 일별 요약 데이터 (Dailies: 안정시 심박, 스트레스 등)
    dailies_res = requests.get(f"{base_url}/dailies", headers=headers, params=params)
    
    if dailies_res.status_code != 200:
        print(f"❌ 가민 API 호출 실패 (상태 코드 {dailies_res.status_code}):", dailies_res.text)
        return

    dailies_data = dailies_res.json()
    health_list = []

    for item in dailies_data:
        health_list.append({
            "calendar_date": item.get("calendarDate"),
            "resting_hr": item.get("restingHeartRateInBeatsPerMinute"),
            "min_hr": item.get("minHeartRateInBeatsPerMinute"),
            "max_hr": item.get("maxHeartRateInBeatsPerMinute"),
            "avg_stress": item.get("averageStressLevel"),
            "max_stress": item.get("maxStressLevel"),
            "steps": item.get("steps"),
            "active_kcal": item.get("activeKilocalories"),
            "moderate_intensity_min": round(item.get("moderateIntensityDurationInSeconds", 0) / 60, 1),
            "vigorous_intensity_min": round(item.get("vigorousIntensityDurationInSeconds", 0) / 60, 1),
        })

    df_garmin = pd.DataFrame(health_list)
    if not df_garmin.empty:
        df_garmin = df_garmin.drop_duplicates(subset=["calendar_date"]).sort_values("calendar_date", ascending=False)
    
    df_garmin.to_csv("garmin_health.csv", index=False, encoding="utf-8-sig")
    print(f"✅ 가민 건강/컨디션 데이터 {len(df_garmin)}건 저장 완료.")


if __name__ == "__main__":
    print("🚀 피트니스 데이터 수집을 시작합니다...")
    fetch_strava_data()
    fetch_garmin_data()
    print("🎉 모든 작업이 완료되었습니다.")
