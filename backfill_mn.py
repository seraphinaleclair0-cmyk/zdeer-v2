"""
backfill_mn.py — 一次性补全「沟通管理」M/N 列
只补 M列为空 且 G列不是「已放弃」/「无需回复」的行
不动其他任何列
跑一次就够了
"""

import base64
import re
import time
from datetime import datetime

from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    REPLIES_SHEET,
    SHEET_ID,
)
from google_auth import get_creds

# 跳过这些状态
SKIP_STATUSES = {"已放弃", "无需回复"}


def get_sheet_data(sheets) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A:N",
    ).execute()
    return result.get("values", [])


def update_cell(sheets, row: int, col: int, value: str):
    col_letter = chr(64 + col)
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!{col_letter}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def search_message_id_and_subject(gmail, from_email: str) -> tuple:
    """
    在4个Gmail收件箱里搜索这个达人发来的最新一封邮件
    返回 (message_id, subject)，找不到返回 ("", "")
    """
    for account in EMAIL_ACCOUNTS:
        query = f"from:{from_email}"
        try:
            result = gmail.users().messages().list(
                userId="me",
                q=query,
                maxResults=1,  # 只要最新一封
            ).execute()
            messages = result.get("messages", [])
            if not messages:
                continue

            msg = gmail.users().messages().get(
                userId="me",
                id=messages[0]["id"],
                format="full",
            ).execute()

            headers = {
                h["name"].lower(): h["value"]
                for h in msg["payload"]["headers"]
            }
            message_id = headers.get("message-id", "")
            subject    = headers.get("subject", "")

            if message_id:
                return message_id, subject

        except Exception as e:
            print(f"    ⚠️ 搜索 {account['email']} 失败：{e}")
            continue

    return "", ""


def main():
    print(f"\n🔧 开始补全 M/N 列 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = get_creds()
    gmail  = build("gmail",  "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    data = get_sheet_data(sheets)
    print(f"  共 {len(data) - 1} 行")

    updated = 0
    skipped_status = 0
    skipped_has_data = 0
    not_found = 0

    for i, row in enumerate(data[1:], start=2):
        # C列邮箱
        email  = row[2].strip() if len(row) > 2 else ""
        # G列状态
        status = row[6].strip() if len(row) > 6 else ""
        # M列 Message-ID
        msg_id = row[12].strip() if len(row) > 12 else ""

        # 跳过已放弃/无需回复
        if status in SKIP_STATUSES:
            skipped_status += 1
            continue

        # 跳过已有 Message-ID 的行
        if msg_id:
            skipped_has_data += 1
            continue

        if not email:
            print(f"  ⚠️ 行{i} 邮箱为空，跳过")
            continue

        print(f"  行{i} 补全中：{email}")
        message_id, subject = search_message_id_and_subject(gmail, email)

        if message_id:
            update_cell(sheets, i, 13, message_id)  # M列
            update_cell(sheets, i, 14, subject)      # N列
            updated += 1
            print(f"  ✅ 已补全：{message_id[:40]}...")
        else:
            not_found += 1
            print(f"  ⚠️ 找不到邮件：{email}")

        time.sleep(1)  # 避免 Gmail API 限流

    print(f"""
🎉 补全完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  成功补全：{updated} 行
  已有数据跳过：{skipped_has_data} 行
  状态跳过：{skipped_status} 行
  找不到邮件：{not_found} 行
""")


if __name__ == "__main__":
    main()
