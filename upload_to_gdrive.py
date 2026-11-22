import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GDRIVE_CREDENTIALS = (os.environ.get("GDRIVE_CREDENTIALS") or "").strip()
GDRIVE_FOLDER_ID = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()

TARGET_FILES = [
    "fit_analysis_report.csv",
    "garmin_activities.csv",
    "garmin_health.csv",
    "strava_activities.csv",
]


def upload_files_to_drive():
    if not GDRIVE_CREDENTIALS or not GDRIVE_FOLDER_ID:
        print("⚠️ GDRIVE_CREDENTIALS 또는 GDRIVE_FOLDER_ID가 설정되지 않아 업로드를 건너뜁니다.", flush=True)
        return

    try:
        creds_info = json.loads(GDRIVE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        service = build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"❌ Google Drive 인증 실패: {e}", flush=True)
        return

    for filename in TARGET_FILES:
        if not os.path.exists(filename):
            print(f"⚠️ 로컬 파일이 없어 건너뜁니다: {filename}", flush=True)
            continue

        try:
            # 지정된 폴더 내에 동일한 이름의 파일이 이미 있는지 검색
            query = f"name = '{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed = false"
            results = service.files().list(q=query, fields="files(id, name)").execute()
            existing_files = results.get("files", [])

            media = MediaFileUpload(filename, mimetype="text/csv", resumable=True)

            if existing_files:
                # 기존 파일이 있으면 ID를 유지한 채 최신 내용으로 덮어쓰기(Update)
                file_id = existing_files[0]["id"]
                service.files().update(fileId=file_id, media_body=media).execute()
                print(f"🔄 Google Drive 파일 덮어쓰기 완료: {filename}", flush=True)
            else:
                # 기존 파일이 없으면 새로 생성(Create)
                file_metadata = {
                    "name": filename,
                    "parents": [GDRIVE_FOLDER_ID],
                }
                service.files().create(body=file_metadata, media_body=media, fields="id").execute()
                print(f"📤 Google Drive 새 파일 업로드 완료: {filename}", flush=True)

        except Exception as e:
            print(f"❌ 업로드 오류 ({filename}): {e}", flush=True)


if __name__ == "__main__":
    print("🚀 Google Drive 자동 동기화를 시작합니다...", flush=True)
    upload_files_to_drive()
    print("🎉 Google Drive 동기화 작업이 완료되었습니다.", flush=True)
