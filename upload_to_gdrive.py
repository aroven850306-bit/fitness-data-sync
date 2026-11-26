import os
import io
import csv
import json
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# Google Drive API 단일 스코프만 사용 (Google Sheets API 활성화 불필요)
SCOPES = ['https://www.googleapis.com/auth/drive']

creds_json = json.loads(os.environ['GDRIVE_CREDENTIALS'])
folder_id = os.environ['GDRIVE_FOLDER_ID']

creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)


def find_existing_file(filename, mime_type=None):
    """지정된 폴더 내에서 동일한 이름의 기존 파일 ID를 조회"""
    query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    res = drive_service.files().list(
        q=query,
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()
    files = res.get('files', [])
    return files[0]['id'] if files else None


def overwrite_google_sheet_via_drive(csv_bytes, sheet_name):
    """Google Drive API의 files().update()를 이용해 기존 Google 스프레드시트를 CSV 데이터로 직접 덮어쓰기"""
    sheet_id = find_existing_file(sheet_name, mime_type='application/vnd.google-apps.spreadsheet')
    if not sheet_id:
        print(f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. 사용자 계정으로 빈 시트를 먼저 생성해 주세요.")
        return

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(csv_bytes),
            mimetype='text/csv',
            resumable=True
        )
        drive_service.files().update(
            fileId=sheet_id,
            media_body=media,
            supportsAllDrives=True
        ).execute()
        print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_name}")
    except HttpError as e:
        print(f"❌ 스프레드시트 업데이트 오류 ({sheet_name}): {e}")


def update_google_sheet_from_csv(csv_path, sheet_name):
    """CSV 파일을 읽어 Pandas4Warning 없이 안전하게 변환한 뒤 Google 스프레드시트 덮어쓰기"""
    if not os.path.exists(csv_path):
        print(f"⚠️ 로컬 파일 없음 (건너뜀): {csv_path}")
        return

    df = pd.read_csv(csv_path, dtype=str).fillna('')
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding='utf-8')
    overwrite_google_sheet_via_drive(buf.getvalue().encode('utf-8-sig'), sheet_name)


def update_google_sheet_from_txt(txt_path, sheet_name):
    """latest_ride_1s_summary.txt 내용을 1열 CSV로 변환해 Google 스프레드시트(latest_ride_1s_summary) 덮어쓰기"""
    if not os.path.exists(txt_path):
        print(f"⚠️ 요약 TXT 파일 없음 (건너뜀): {txt_path}")
        return

    with open(txt_path, 'r', encoding='utf-8') as f:
        lines = [line.rstrip('\n') for line in f.readlines()]

    if not lines:
        lines = ['데이터 없음']

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['summary_line'])
    for line in lines:
        writer.writerow([line])

    overwrite_google_sheet_via_drive(buf.getvalue().encode('utf-8-sig'), sheet_name)


def main():
    print("🚀 Google Drive 자동 동기화를 시작합니다...")
    print(f"🔑 인증된 서비스 계정 이메일: {creds_json.get('client_email')}")

    # 1. 핵심 4대 CSV -> 기존 Google 스프레드시트 덮어쓰기
    csv_targets = [
        ('fit_analysis_report.csv', 'fit_analysis_report'),
        ('garmin_activities.csv', 'garmin_activities'),
        ('garmin_health.csv', 'garmin_health'),
        ('strava_activities.csv', 'strava_activities'),
        ('readiness_v2_summary.csv', 'readiness_v2_summary')
        ('strava_segments.csv', 'strava_segments'),
    ]
    for csv_file, sheet_name in csv_targets:
        update_google_sheet_from_csv(csv_file, sheet_name)

    # 2. 1초 통합 요약 TXT -> 기존 Google 스프레드시트(latest_ride_1s_summary) 덮어쓰기
    update_google_sheet_from_txt('latest_ride_1s_summary.txt', 'latest_ride_1s_summary')

    print("🎉 Google Drive 동기화 작업이 완료되었습니다.")


if __name__ == '__main__':
    main()
