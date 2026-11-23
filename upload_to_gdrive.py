#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

GDRIVE_CREDENTIALS = (os.environ.get("GDRIVE_CREDENTIALS") or "").strip()
RAW_FOLDER_ID = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()
DATA_DIR = Path(os.getenv("FITNESS_DATA_DIR", "."))

SHEET_TARGETS = [
    ("fit_analysis_report.csv", "fit_analysis_report"),
    ("garmin_activities.csv", "garmin_activities"),
    ("garmin_health.csv", "garmin_health"),
    ("strava_activities.csv", "strava_activities"),
]

TEXT_OVERWRITE_TARGETS = [
    ("latest_ride_1s_summary.txt", "latest_ride_1s_summary.txt", "text/plain"),
]


def extract_folder_id(raw_value: str) -> str:
    """전체 URL이 입력되었더라도 순수 Google Drive 폴더 ID만 안전하게 추출"""
    if "folders/" in raw_value:
        match = re.search(r"folders/([a-zA-Z0-9_-]+)", raw_value)
        if match:
            return match.group(1)
    return raw_value.split("?")[0].strip()


def resolve_local_file(rel_path: str) -> Optional[Path]:
    """DATA_DIR 기준 경로와 현재 작업 디렉터리(.) 기준 경로를 순서대로 탐색"""
    candidates = [
        DATA_DIR / rel_path,
        Path(".") / rel_path,
        Path(os.path.basename(rel_path)),
    ]
    seen = set()
    for cand in candidates:
        norm = str(cand)
        if norm in seen:
            continue
        seen.add(norm)
        if cand.exists() and cand.is_file():
            return cand
    return None


def upsert_sheet(service, folder_id: str, rel_csv_path: str, sheet_title: str) -> None:
    local_path = resolve_local_file(rel_csv_path)
    if local_path is None:
        print(f"⚠️ 로컬 파일이 없어 건너뜁니다: {rel_csv_path}", flush=True)
        return

    query = (
        f"name = '{sheet_title}' and '{folder_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    existing_files = results.get("files", [])
    media = MediaFileUpload(str(local_path), mimetype="text/csv", resumable=True)

    if existing_files:
        file_id = existing_files[0]["id"]
        service.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_title} ({local_path})", flush=True)
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
            supportsAllDrives=True,
        ).execute()
        print(f"📤 Google 스프레드시트 신규 생성 완료: {sheet_title} ({local_path})", flush=True)


def upsert_text_file(service, folder_id: str, rel_path: str, drive_name: str, mime_type: str) -> None:
    local_path = resolve_local_file(rel_path)
    if local_path is None:
        print(f"⚠️ 로컬 파일이 없어 건너뜁니다: {rel_path}", flush=True)
        return

    query = f"name = '{drive_name}' and '{folder_id}' in parents and trashed = false"
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    existing_files = results.get("files", [])
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)

    if existing_files:
        file_id = existing_files[0]["id"]
        service.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"🔄 Google Drive 텍스트 덮어쓰기 완료: {drive_name} ({local_path})", flush=True)
    else:
        file_metadata = {
            "name": drive_name,
            "parents": [folder_id],
        }
        service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        print(f"📤 Google Drive 텍스트 생성 완료: {drive_name} ({local_path})", flush=True)


def upload_files_to_drive() -> None:
    if not GDRIVE_CREDENTIALS or not RAW_FOLDER_ID:
        print("⚠️ GDRIVE_CREDENTIALS 또는 GDRIVE_FOLDER_ID가 설정되지 않아 업로드를 건너뜁니다.", flush=True)
        return

    folder_id = extract_folder_id(RAW_FOLDER_ID)

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

    for rel_csv_path, sheet_title in SHEET_TARGETS:
        try:
            upsert_sheet(service, folder_id, rel_csv_path, sheet_title)
        except Exception as e:
            print(f"❌ 시트 업로드 오류 ({rel_csv_path}): {e}", flush=True)

    for rel_txt_path, drive_name, mime_type in TEXT_OVERWRITE_TARGETS:
        try:
            upsert_text_file(service, folder_id, rel_txt_path, drive_name, mime_type)
        except Exception as e:
            print(f"❌ 텍스트 업로드 오류 ({rel_txt_path}): {e}", flush=True)


if __name__ == "__main__":
    print("🚀 Google Drive 자동 동기화를 시작합니다...", flush=True)
    upload_files_to_drive()
    print("🎉 Google Drive 동기화 작업이 완료되었습니다.", flush=True)
