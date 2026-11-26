import csv
import io
import json
import os
import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

# Google Drive API 단일 스코프만 사용 (Google Sheets API 별도 활성화 불필요)
SCOPES = ["https://www.googleapis.com/auth/drive"]

# 동기화 대상 CSV 파일 및 대상 Google 스프레드시트 이름 매핑
CSV_SYNC_TARGETS = [
    ("fit_analysis_report.csv", "fit_analysis_report"),
    ("garmin_activities.csv", "garmin_activities"),
    ("garmin_health.csv", "garmin_health"),
    ("strava_activities.csv", "strava_activities"),
    ("readiness_v2_summary.csv", "readiness_v2_summary"),
    ("strava_segments.csv", "strava_segments"),
]

# 동기화 대상 1초 통합 요약 TXT 파일 및 Google 스프레드시트 이름 매핑
TXT_SYNC_TARGETS = [
    ("latest_ride_1s_summary.txt", "latest_ride_1s_summary"),
]


def _create_drive_service():
    """
    환경변수(GDRIVE_CREDENTIALS, GDRIVE_FOLDER_ID)에서 서비스 계정 자격증명과
    대상 Google Drive 폴더 ID를 읽어 Drive API v3 클라이언트를 생성합니다.
    """
    creds_raw = os.environ.get("GDRIVE_CREDENTIALS", "").strip()
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if not creds_raw:
        raise ValueError("환경변수 'GDRIVE_CREDENTIALS'가 설정되지 않았습니다.")
    if not folder_id:
        raise ValueError("환경변수 'GDRIVE_FOLDER_ID'가 설정되지 않았습니다.")

    try:
        creds_json = json.loads(creds_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"환경변수 'GDRIVE_CREDENTIALS' JSON 파싱 실패: {e}") from e

    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)
    return service, folder_id, creds_json.get("client_email", "")


def find_existing_file(drive_service, folder_id, filename, mime_type=None):
    """
    지정된 Google Drive 폴더 내에서 휴지통에 없는 동일 이름의 파일 ID를 조회합니다.
    선택적으로 MIME 타입(예: 스프레드시트)으로 필터링합니다.
    """
    escaped_name = filename.replace("'", "\\'")
    query = f"'{folder_id}' in parents and name = '{escaped_name}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"

    try:
        res = (
            drive_service.files()
            .list(
                q=query,
                fields="files(id, name, mimeType)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = res.get("files", [])
        return files[0]["id"] if files else None
    except HttpError as e:
        print(f"❌ Drive 파일 조회 실패 ({filename}): {e}")
        return None


def overwrite_google_sheet_via_drive(drive_service, folder_id, csv_bytes, sheet_name):
    """
    Google Drive API의 files().update()를 이용해 기존 Google 스프레드시트를
    UTF-8 BOM이 포함된 CSV 바이트 스트림으로 직접 덮어씁니다.
    """
    sheet_id = find_existing_file(
        drive_service,
        folder_id,
        sheet_name,
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    if not sheet_id:
        print(
            f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. "
            "사용자 계정으로 빈 시트를 먼저 생성하고 서비스 계정에 공유해 주세요."
        )
        return

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
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
    except Exception as e:
        print(f"❌ 스프레드시트 업로드 중 예기치 못한 오류 ({sheet_name}): {e}")


def update_google_sheet_from_csv(drive_service, folder_id, csv_path, sheet_name):
    """
    로컬 CSV 파일을 문자열 기준으로 안전하게 읽어 빈 값을 공백으로 정규화한 뒤
    대상 Google 스프레드시트를 덮어씁니다.
    """
    if not os.path.exists(csv_path):
        print(f"⚠️ 로컬 CSV 파일 없음 (건너뜀): {csv_path}")
        return

    try:
        df = pd.read_csv(csv_path, dtype=str).fillna("")
        buf = io.StringIO()
        df.to_csv(buf, index=False, encoding="utf-8")
        overwrite_google_sheet_via_drive(
            drive_service,
            folder_id,
            buf.getvalue().encode("utf-8-sig"),
            sheet_name,
        )
    except Exception as e:
        print(f"❌ CSV 처리 중 오류 ({csv_path}): {e}")


def update_google_sheet_from_txt(drive_service, folder_id, txt_path, sheet_name):
    """
    요약 TXT 파일의 각 줄을 'summary_line' 단일 열 CSV로 변환해
    대상 Google 스프레드시트를 덮어씁니다.
    """
    if not os.path.exists(txt_path):
        print(f"⚠️ 로컬 요약 TXT 파일 없음 (건너뜀): {txt_path}")
        return

    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\r\n") for line in f.readlines()]

        if not lines:
            lines = ["데이터 없음"]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["summary_line"])
        for line in lines:
            writer.writerow([line])

        overwrite_google_sheet_via_drive(
            drive_service,
            folder_id,
            buf.getvalue().encode("utf-8-sig"),
            sheet_name,
        )
    except Exception as e:
        print(f"❌ TXT 처리 중 오류 ({txt_path}): {e}")


def main():
    """핵심 CSV 및 요약 TXT 파일을 Google Drive 스프레드시트에 동기화합니다."""
    print("🚀 Google Drive 자동 동기화를 시작합니다...")
    try:
        drive_service, folder_id, client_email = _create_drive_service()
    except Exception as e:
        print(f"❌ Google Drive 초기화 실패: {e}")
        raise

    print(f"🔑 인증된 서비스 계정 이메일: {client_email}")

    # 1. 핵심 CSV -> 기존 Google 스프레드시트 덮어쓰기
    for csv_file, sheet_name in CSV_SYNC_TARGETS:
        update_google_sheet_from_csv(drive_service, folder_id, csv_file, sheet_name)

    # 2. 1초 통합 요약 TXT -> 기존 Google 스프레드시트 덮어쓰기
    for txt_file, sheet_name in TXT_SYNC_TARGETS:
        update_google_sheet_from_txt(drive_service, folder_id, txt_file, sheet_name)

    print("🎉 Google Drive 동기화 작업이 완료되었습니다.")


if __name__ == "__main__":
    main()
