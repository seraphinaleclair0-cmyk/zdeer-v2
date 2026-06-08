"""
sync_outbox_replied.py — 一次性同步「待发名单」回复状态

把「沟通管理」中已经有回复记录的达人，按邮箱回写到「待发名单」：
- 只处理「待发名单」F列仍为「已发送」的行
- 匹配到「沟通管理」C列邮箱，且沟通管理G列不是「已放弃 / 无需回复」时，改为「已回复」
"""

from googleapiclient.discovery import build

from config import OUTBOX_SHEET, REPLIES_SHEET, SHEET_ID
from google_auth import get_creds

SKIP_REPLY_STATUSES = {"已放弃", "无需回复"}


def get_sheet_data(sheets, sheet_name: str) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:Z",
    ).execute()
    return result.get("values", [])


def update_cell(sheets, sheet_name: str, row: int, col: int, value: str):
    col_letter = chr(64 + col)
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!{col_letter}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def row_value(row: list, index: int) -> str:
    return row[index].strip() if len(row) > index and row[index] else ""


def replied_emails(replies_data: list) -> set[str]:
    emails = set()
    for row in replies_data[1:]:
        email = row_value(row, 2).lower()
        status = row_value(row, 6)
        if email and status not in SKIP_REPLY_STATUSES:
            emails.add(email)
    return emails


def main():
    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds)

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data = get_sheet_data(sheets, OUTBOX_SHEET)
    emails = replied_emails(replies_data)

    updated = 0
    skipped = 0

    for row_index, row in enumerate(outbox_data[1:], start=2):
        email = row_value(row, 1).lower()
        status = row_value(row, 5)
        if not email or email not in emails:
            skipped += 1
            continue
        if status != "已发送":
            skipped += 1
            continue

        update_cell(sheets, OUTBOX_SHEET, row_index, 6, "已回复")
        updated += 1
        name = row_value(row, 0) or email
        print(f"  ✅ 行{row_index} 已改为已回复：{name} <{email}>")

    print(f"\n完成：更新 {updated} 行，跳过 {skipped} 行")


if __name__ == "__main__":
    main()
