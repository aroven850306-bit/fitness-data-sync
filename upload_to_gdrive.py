#!/usr/bin/env python3
"""
Google Drive 스프레드시트 자동 동기화 스크립트
- 기존 Google 스프레드시트 파일을 files().update()로 직접 덮어쓰기
- unified_coaching_state.json은 403 저장소 할당량 오류 방지를 위해
  'unified_coaching_state' 스프레드시트로 변환해 덮어쓰기
"""

import csv
import io
import json
import os
from typing import Optional

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
import pandas as pd


SCOPES = ["https://www.googleapis.com/auth/drive"]


def _load_service_account():
    raw = os.environ.get("GDRIVE_CREDENTIALS", "").strip()
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if not raw or not folder_id:
        raise ValueError("GDRIVE_CREDENTIALS 또는 GDRIVE_FOLDER_ID 환경변수가 비어 있습니다.")
    creds_json = json.loads(raw)
    creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
    service = build("drive", "v3", credentials=creds)
    return service, folder_id, creds_json.get("client_email", "unknown")


def find_existing_file(service, folder_id: str, filename: str, mime_type: Optional[str] = None) -> Optional[str]:
    safe_name = filename.replace("'", "\\'")
    query = f"'{folder_id}' in parents and name = '{safe_name}' and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    res = (
        service.files()
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


def overwrite_google_sheet_via_drive(service, folder_id: str, csv_bytes: bytes, sheet_name: str) -> None:
    sheet_id = find_existing_file(
        service,
        folder_id=folder_id,
        filename=sheet_name,
        mime_type="application/vnd.google-apps.spreadsheet",
    )
    if not sheet_id:
        print(f"⚠️ Google Drive 폴더에 '{sheet_name}' 스프레드시트가 없습니다. 사용자 계정으로 빈 시트를 먼저 생성해 주세요.")
        return

    try:
        media = MediaIoBaseUpload(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            resumable=True,
        )
        service.files().update(
            fileId=sheet_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"🔄 Google 스프레드시트 덮어쓰기 완료: {sheet_name}")
    except HttpError as e:
        print(f"❌ 스프레드시트 업데이트 오류 ({sheet_name}): {e}")


def update_google_sheet_from_csv(service, folder_id: str, csv_path: str, sheet_name: str) -> None:
    if not os.path.exists(csv_path):
        print(f"⚠️ 로컬 CSV 파일 없음 (건너뜀): {csv_path}")
        return

    df = pd.read_csv(csv_path, dtype=str).fillna("")
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8")
    overwrite_google_sheet_via_drive(service, folder_id, buf.getvalue().encode("utf-8-sig"), sheet_name)


def update_google_sheet_from_txt(service, folder_id: str, txt_path: str, sheet_name: str) -> None:
    if not os.path.exists(txt_path):
        print(f"⚠️ 로컬 요약 TXT 파일 없음 (건너뜀): {txt_path}")
        return

    with open(txt_path, "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f.readlines()]

    clean_lines = [ln.replace("#ERROR!", "ERR") for ln in lines if ln.strip()]
    if not clean_lines:
        clean_lines = ["데이터 없음"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["summary_line"])
    for line in clean_lines:
        writer.writerow([line])

    overwrite_google_sheet_via_drive(service, folder_id, buf.getvalue().encode("utf-8-sig"), sheet_name)


def update_google_sheet_from_json(service, folder_id: str, json_path: str, sheet_name: str) -> None:
    """JSON 파일을 일반 파일 업로드(403 할당량 오류) 대신 Google 스프레드시트 덮어쓰기로 동기화"""
    if not os.path.exists(json_path):
        print(f"⚠️ 로컬 JSON 파일 없음 (건너뜀): {json_path}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        raw_text = f.read()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["json_content"])
    writer.writerow([raw_text])

    overwrite_google_sheet_via_drive(service, folder_id, buf.getvalue().encode("utf-8-sig"), sheet_name)


def main() -> None:
    print("🚀 Google Drive 자동 동기화를 시작합니다...")
    service, folder_id, client_email = _load_service_account()
    print(f"🔑 인증된 서비스 계정 이메일: {client_email}")

    csv_targets = [
        ("fit_analysis_report.csv", "fit_analysis_report"),
        ("garmin_activities.csv", "garmin_activities"),
        ("garmin_health.csv", "garmin_health"),
        ("strava_activities.csv", "strava_activities"),
        ("readiness_v2_summary.csv", "readiness_v2_summary"),
        ("strava_segments.csv", "strava_segments"),
        ("unified_daily_metrics.csv", "unified_daily_metrics"),
    ]
    for csv_file, sheet_name in csv_targets:
        update_google_sheet_from_csv(service, folder_id, csv_file, sheet_name)

    txt_targets = [
        ("latest_ride_1s_summary.txt", "latest_ride_1s_summary"),
        ("unified_coaching_summary.txt", "unified_coaching_summary"),
    ]
    for txt_file, sheet_name in txt_targets:
        update_google_sheet_from_txt(service, folder_id, txt_file, sheet_name)

    update_google_sheet_from_json(service, folder_id, "unified_coaching_state.json", "unified_coaching_state")

    print("🎉 Google Drive 동기화 작업이 완료되었습니다.")


if __name__ == "__main__":
    main()
