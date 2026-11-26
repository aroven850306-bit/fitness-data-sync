import csv
import io
import json
import os
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# Google Drive API 단일 스코프만 사용 (Google Sheets API 활성화 불필요)
SCOPES = ['https://www.googleapis.com/auth/drive']


def _create_drive_service():
    """환경변수에서 서비스 계정 자격증명과 대상 폴더 ID를 읽어 Google Drive 클라이언트를 생성합니다."""
    creds_raw = os.environ.get('GDRIVE_CREDENTIALS', '').strip()
    folder_id = os.environ.get('GDRIVE_FOLDER_ID', '').strip()
    if not creds_raw:
        raise ValueError("환경변수 'GDRIVE_CREDENTIALS'가 설정되지 않았습니다.")
    if not folder_id:
        raise ValueError("환경변수 'GDRIVE_FOLDER_ID'가 설정되지 않았습니다.")

    creds_json = json.loads(creds_raw)
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    service = build('drive', 'v3', credentials=creds)
    return service, folder_id, creds_json.get('client_email', '')


def find_existing_file(drive_service, folder_id, filename, mime_type=None):
    """지정된 폴더 내에서 동일한 이름의 기존 파일 ID를 조회합니다."""
    escaped_name = filename.replace("'", "\\'")
    query = f"'{folder_id}' in parents and name = '{escaped_name}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    res = drive_service.files().list(
        q=query,
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = res.get('files', [])
    return files[0]['id'] if files else None


def overwrite_google_sheet_via_drive(drive_service, folder_id, csv_bytes, sheet_name):
    """Google Drive API의 files().update()를 이용해 기존 Google 스프레드시트를 CSV 데이터로 직접 덮어씁니다."""
    sheet_id = find_existing_file(
        drive_service,
        folder_id,
        sheet_name,
        mime_type='application/vnd.google-apps.spreadsheet',
    )
    if not sheet_id:
        print(
            f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. "
            "사용자 계정으로 빈 시트를 먼저 생성해 주세요."
        )
        return

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(csv_bytes),
            mimetype='text/csv',
            resumable=True,
        )
        drive_service.files().update(
            fileId=sheet_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_name}")
    except HttpError as e:
        print(f"❌ 스프레드시트 업데이트 오류 ({sheet_name}): {e}")


def update_google_sheet_from_csv(drive_service, folder_id, csv_path, sheet_name):
    """CSV 파일을 읽어 안전하게 변환한 뒤 Google 스프레드시트를 덮어씁니다."""
    if not os.path.exists(csv_path):
        print(f"⚠️ 로컬 파일 없음 (건너뜀): {csv_path}")
        return

    try:
        df = pd.read_csv(csv_path, dtype=str).fillna('')
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding='utf-8')
        overwrite_google_sheet_via_drive(
            drive_service,
            folder_id,
            buf.getvalue().encode('utf-8-sig'),
            sheet_name,
        )
    except Exception as e:
        print(f"❌ CSV 처리 중 오류 ({csv_path}): {e}")


def update_google_sheet_from_txt(drive_service, folder_id, txt_path, sheet_name):
    """요약 TXT 파일 내용을 1열 CSV로 변환해 Google 스프레드시트를 덮어씁니다."""
    if not os.path.exists(txt_path):
        print(f"⚠️ 요약 TXT 파일 없음 (건너뜀): {txt_path}")
        return

    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f.readlines()]

        if not lines:
            lines = ['데이터 없음']

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['summary_line'])
        for line in lines:
            writer.writerow([line])

        overwrite_google_sheet_via_drive(
            drive_service,
            folder_id,
            buf.getvalue().encode('utf-8-sig'),
            sheet_name,
        )
    except Exception as e:
        print(f"❌ TXT 처리 중 오류 ({txt_path}): {e}")


def main():
    """핵심 CSV 및 요약 TXT 파일을 Google 스프레드시트에 동기화합니다."""
    print("🚀 Google Drive 자동 동기화를 시작합니다...")
    try:
        drive_service, folder_id, client_email = _create_drive_service()
    except Exception as e:
        print(f"❌ Google Drive 초기화 실패: {e}")
        raise

    print(f"🔑 인증된 서비스 계정 이메일: {client_email}")

    # 1. 핵심 CSV -> 기존 Google 스프레드시트 덮어쓰기 (누락된 쉼표 복원 완료)
    csv_targets = [
        ('fit_analysis_report.csv', 'fit_analysis_report'),
        ('garmin_activities.csv', 'garmin_activities'),
        ('garmin_health.csv', 'garmin_health'),
        ('strava_activities.csv', 'strava_activities'),
        ('readiness_v2_summary.csv', 'readiness_v2_summary'),
        ('strava_segments.csv', 'strava_segments'),
    ]
    for csv_file, sheet_name in csv_targets:
        update_google_sheet_from_csv(drive_service, folder_id, csv_file, sheet_name)

    # 2. 1초 통합 요약 TXT -> 기존 Google 스프레드시트(latest_ride_1s_summary) 덮어쓰기
    update_google_sheet_from_txt(
        drive_service,
        folder_id,
        'latest_ride_1s_summary.txt',
        'latest_ride_1s_summary',
    )

    print("🎉 Google Drive 동기화 작업이 완료되었습니다.")


if __name__ == '__main__':
    main()
