import os
import json
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 1. Google Drive 및 Sheets API 인증 설정
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

creds_json = json.loads(os.environ['GDRIVE_CREDENTIALS'])
folder_id = os.environ['GDRIVE_FOLDER_ID']

creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)


def find_existing_file(filename, mime_type=None):
    """지정된 폴더 내에서 동일한 이름의 기존 파일 ID를 조회 (서비스 계정 용량 소모 방지용 덮어쓰기 대상 찾기)"""
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


def update_google_sheet_from_csv(csv_path, sheet_name):
    """CSV 데이터를 기존 Google 스프레드시트에 덮어쓰기 (신규 생성 금지로 403 용량 초과 방지)"""
    if not os.path.exists(csv_path):
        print(f"⚠️ 로컬 파일 없음 (건너뜀): {csv_path}")
        return

    sheet_id = find_existing_file(sheet_name, mime_type='application/vnd.google-apps.spreadsheet')
    if not sheet_id:
        print(f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. 사용자 계정으로 빈 시트를 먼저 생성해 주세요.")
        return

    try:
        df = pd.read_csv(csv_path).fillna('')
        values = [df.columns.tolist()] + df.astype(str).values.tolist()

        # 기존 시트 내용 초기화 후 최신 데이터 기록
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range='A1:ZZ'
        ).execute()

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption='USER_ENTERED',
            body={'values': values}
        ).execute()
        print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_name}")
    except HttpError as e:
        print(f"❌ 스프레드시트 업데이트 오류 ({sheet_name}): {e}")


def update_google_sheet_from_txt(txt_path, sheet_name):
    """latest_ride_1s_summary.txt 내용을 Google 스프레드시트에 줄 단위로 덮어쓰기"""
    if not os.path.exists(txt_path):
        print(f"⚠️ 요약 TXT 파일 없음 (건너뜀): {txt_path}")
        return

    sheet_id = find_existing_file(sheet_name, mime_type='application/vnd.google-apps.spreadsheet')
    if not sheet_id:
        print(f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. 사용자 계정으로 빈 시트를 먼저 생성해 주세요.")
        return

    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            lines = [[line.rstrip('\n')] for line in f.readlines()]

        if not lines:
            lines = [['데이터 없음']]

        sheets_service.spreadsheets().values().clear(
            spreadsheetId=sheet_id,
            range='A1:Z'
        ).execute()

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range='A1',
            valueInputOption='RAW',
            body={'values': lines}
        ).execute()
        print(f"🔄 1초 통합 요약 스프레드시트 덮어쓰기 완료: {sheet_name}")
    except HttpError as e:
        print(f"❌ 1초 통합 요약 시트 업데이트 오류 ({sheet_name}): {e}")


def main():
    print("🚀 Google Drive 자동 동기화를 시작합니다...")
    print(f"🔑 인증된 서비스 계정 이메일: {creds_json.get('client_email')}")

    # 1. 핵심 4대 CSV -> 기존 Google 스프레드시트 덮어쓰기
    csv_targets = [
        ('fit_analysis_report.csv', 'fit_analysis_report'),
        ('garmin_activities.csv', 'garmin_activities'),
        ('garmin_health.csv', 'garmin_health'),
        ('strava_activities.csv', 'strava_activities'),
    ]
    for csv_file, sheet_name in csv_targets:
        update_google_sheet_from_csv(csv_file, sheet_name)

    # 2. 1초 통합 요약 TXT -> Google 스프레드시트(latest_ride_1s_summary) 덮어쓰기
    # (일반 바이너리/압축 파일 신규 업로드는 서비스 계정 0GB 제한으로 403 에러를 유발하므로 제외)
    update_google_sheet_from_txt('latest_ride_1s_summary.txt', 'latest_ride_1s_summary')

    print("🎉 Google Drive 동기화 작업이 완료되었습니다.")


if __name__ == '__main__':
    main()
