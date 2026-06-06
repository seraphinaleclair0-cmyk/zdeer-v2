"""
sync_ig_outbox_replied.py — 一次性同步「ig开发名单1」回复状态

把「沟通管理」中已经有回复记录的达人，按邮箱回写到「ig开发名单1」：
- 匹配「沟通管理」C列邮箱。
- 匹配到后把「ig开发名单1」F列状态改为「已回复」。
"""

from googleapiclient.discovery import build

from config import REPLIES_SHEET, SHEET_ID
from google_auth import get_creds
from send_ig_outreach import IG_HEADERS, IG_OUTBOX_SHEET, ensure_ig_sheet


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
        if email:
            emails.add(email)
    return emails


def main():
    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds)
    ensure_ig_sheet(sheets)

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data = get_sheet_data(sheets, IG_OUTBOX_SHEET)
    emails = replied_emails(replies_data)

    updated = 0
    skipped = 0

    if outbox_data and outbox_data[0][: len(IG_HEADERS)] != IG_HEADERS:
        print(f"  ⚠️ {IG_OUTBOX_SHEET} 表头和默认结构不完全一致，仍继续按 B/F 列同步")

    for row_index, row in enumerate(outbox_data[1:], start=2):
        email = row_value(row, 1).lower()
        status = row_value(row, 5)
        if not email or email not in emails:
            skipped += 1
            continue
        if status == "已回复":
            skipped += 1
            continue

        update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, "已回复")
        updated += 1
        name = row_value(row, 0) or email
        print(f"  ✅ 行{row_index} 已改为已回复：{name} <{email}>")

    print(f"\n完成：更新 {updated} 行，跳过 {skipped} 行")


if __name__ == "__main__":
    main()
