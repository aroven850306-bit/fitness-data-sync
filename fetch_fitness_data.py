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
    print(f"✅ 스트라바 라이딩 데이터 {len(df_strava)}건 저장 완료.")


def get_garmin_access_token(api_key):
    """가민 Personal API Key(GCDC...)를 이용해 Access Token 발급"""
    token_url = "https://personal.connectapi.garmin.com/personalapitoken-service/personal-api/personal-api-key/token"
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Accept": "application/json",
    }
    res = requests.post(token_url, headers=headers)
    if res.status_code != 200:
        print(f"❌ 가민 Access Token 발급 실패 (상태 코드 {res.status_code}):", res.text)
        return None

    # 응답이 JSON 객체이거나 순수 문자열 토큰일 경우 모두 처리
    try:
        data = res.json()
        if isinstance(data, dict):
            return data.get("accessToken") or data.get("access_token") or data.get("token")
        elif isinstance(data, str):
            return data.strip()
    except ValueError:
        return res.text.strip().strip('"')
    return None


def fetch_garmin_data():
    if not GARMIN_API_KEY:
        print("⚠️ GARMIN_API_KEY가 설정되지 않아 건너뜁니다.")
        return

    # 1단계: Access Token 발급
    access_token = get_garmin_access_token(GARMIN_API_KEY)
    if not access_token:
        return
    print("✅ 가민 Personal API Access Token 발급 성공!")

    # 발급받은 Access Token으로
