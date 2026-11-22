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

    # 최근 14일치 데이터 조회 범위 (Unix Timestamp 및 ISO 날짜)
    end_time = int(time.time())
    start_time = int((datetime.now() - timedelta(days=14)).timestamp())
    
    # GCDC 키가 지원하는 주요 헤더 및 엔드포인트 조합 테스트
    endpoints_and_headers = [
        # 조합 1: Personal API 표준 X-API-Key 헤더 + Wellness Dailies
        (
            "https://apis.garmin.com/wellness-api/rest/dailies",
            {"X-API-Key": GARMIN_API_KEY, "Accept": "application/json"},
            {"uploadStartTimeInSeconds": start_time, "uploadEndTimeInSeconds": end_time},
        ),
        # 조합 2: Personal Data 전용 엔드포인트 + X-API-Key
        (
            "https://apis.garmin.com/personal-api/rest/dailies",
            {"X-API-Key": GARMIN_API_KEY, "Accept": "application/json"},
            {"uploadStartTimeInSeconds": start_time, "uploadEndTimeInSeconds": end_time},
        ),
        # 조합 3: Authorization API-Key 헤더 형식
        (
            "https://apis.garmin.com/wellness-api/rest/dailies",
            {"Authorization": f"API-Key {GARMIN_API_KEY}", "Accept": "application/json"},
            {"uploadStartTimeInSeconds": start_time, "uploadEndTimeInSeconds": end_time},
        ),
    ]

    dailies_data = None
    for idx, (url, headers, params) in enumerate(endpoints_and_headers, 1):
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            print(f"✅ 가민 API 연결 성공 (방식 {idx})")
            dailies_data = res.json()
            break
        else:
            print(f"⚠️ 가민 방식 {idx} 시도 결과 ({res.status_code}): {res.text[:120]}")

    if dailies_data is None:
        print("❌ 모든 가민 Personal API 호출 방식이 실패했습니다. 발급 페이지의 샘플 URL/헤더 확인이 필요합니다.")
        return

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
