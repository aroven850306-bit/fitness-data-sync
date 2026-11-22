import os
import json
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GDRIVE_CREDENTIALS = (os.environ.get("GDRIVE_CREDENTIALS") or "").strip()
RAW_FOLDER_ID = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()

TARGET_FILES = [
    "fit_analysis_report.csv",
    "garmin_activities.csv",
    "garmin_health.csv",
    "strava_activities.csv",
]


def extract_folder_id(raw_value):
    """전체 URL이 입력되었더라도 순수 Google Drive 폴더 ID만 안전하게 추출"""
    if "folders/" in raw_value:
        match = re.search(r"folders/([a-zA-Z0-9_-]+)", raw_value)
        if match:
            return match.group(1)
    return raw_value.split("?")[0].strip()


def upload_files_to_drive():
    if not GDRIVE_CREDENTIALS or not RAW_FOLDER_ID:
        print("⚠️ GDRIVE_CREDENTIALS 또는 GDRIVE_FOLDER_ID가 설정되지 않아 업로드를 건너뜁니다.", flush=True)
        return

    folder_id = extract_folder_id(RAW_FOLDER_ID)

    try:
        creds_info = json.loads(GDRIVE_CREDENTIALS)
        client_email = creds_info.get("client_email", "알 수 없음")
        print(f"🔑 인증된 서비스 계정 이메일: {client_email}", flush=True)

        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        service = build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"❌ Google Drive 인증 실패: {e}", flush=True)
        return

    # 폴더 접근 권한 사전 검증
    try:
        folder_info = service.files().get(
            fileId=folder_id,
            fields="id, name",
            supportsAllDrives=True
        ).execute()
        print(f"📂 대상 폴더 연결 성공: '{folder_info.get('name')}'", flush=True)
    except Exception as e:
        print(f"❌ 폴더 접근 실패! Google Drive에서 해당 폴더를 '{client_email}' 계정과 '편집자' 권한으로 공유했는지 확인해 주세요.", flush=True)
        print(f"   상세 오류: {e}", flush=True)
        return

    for filename in TARGET_FILES:
        if not os.path.exists(filename):
            print(f"⚠️ 로컬 파일이 없어 건너뜁니다: {filename}", flush=True)
            continue

        # 노트북에서 바로 읽을 수 있도록 확장자를 뗀 Google 스프레드시트 이름으로 저장
        sheet_title = filename.replace(".csv", "")

        try:
            query = f"name = '{sheet_title}' and '{folder_id}' in parents and trashed = false"
            results = service.files().list(
                q=query,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            existing_files = results.get("files", [])

            media = MediaFileUpload(filename, mimetype="text/csv", resumable=True)

            if existing_files:
                file_id = existing_files[0]["id"]
                service.files().update(
                    fileId=file_id,
                    media_body=media,
                    supportsAllDrives=True
                ).execute()
                print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_title}", flush=True)
            else:
                file_metadata = {
                    "name": sheet_title,
                    "parents": [folder_id],
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                }
                service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True
                ).execute()
                print(f"📤 Google 스프레드시트 신규 생성 완료: {sheet_title}", flush=True)

        except Exception as e:
            print(f"❌ 업로드 오류 ({filename}): {e}", flush=True)


if __name__ == "__main__":
    print("🚀 Google Drive 자동 동기화를 시작합니다...", flush=True)
    upload_files_to_drive()
    print("🎉 Google Drive 동기화 작업이 완료되었습니다.", flush=True)
