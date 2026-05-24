"""
send_outreach.py — 发开发邮件 + 跟进邮件
自动触发：每天北京时间 00:00
手动触发：随时可以点 Run workflow

完成：
1. 待发名单有 ✅ 且未发送 → 发开发邮件
2. 已发送 + 未回复 + 跟进次数 < 3 + 超过24小时 → 发跟进邮件
"""

import random
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    OUTBOX_SHEET,
    REPLIES_SHEET,
    SEND_INTERVAL_MIN,
    SEND_INTERVAL_MAX,
    SHEET_ID,
)
from google_auth import get_creds
from template import (
    get_followup_body,
    get_followup_subject,
    get_outreach_body,
    get_outreach_subject,
)

# ── Sheets 工具 ───────────────────────────────────────────────────

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

# ── 邮件发送 ──────────────────────────────────────────────────────

def get_account_by_email(email: str) -> dict:
    for acc in EMAIL_ACCOUNTS:
        if acc["email"].lower() == email.lower():
            return acc
    return EMAIL_ACCOUNTS[0]


def send_email(account: dict, to_email: str, subject: str, body: str) -> bool:
    try:
        msg = MIMEMultipart()
        msg["From"]    = account["email"]
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(account["smtp_host"], account["smtp_port"]) as server:
            server.starttls()
            server.login(account["email"], account["password"])
            server.sendmail(account["email"], to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"  ❌ 发送失败：{e}")
        return False

# ── 主流程 ────────────────────────────────────────────────────────

def main():
    print(f"\n📤 开始发开发邮件 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = get_creds()
    sheets = build("sheets", "v4", credentials=creds)

    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)

    # 收集已回复的邮箱，用于判断是否需要跟进
    replied_emails = set()
    for row in replies_data[1:]:
        if len(row) > 2:
            replied_emails.add(row[2].strip().lower())

    # ── 1. 发开发邮件 ─────────────────────────────────────────────
    print("\n📧 检查待发名单...")
    sent_count = 0

    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 5:
            continue

        name         = row[0].strip() if len(row) > 0 else ""
        to_email     = row[1].strip() if len(row) > 1 else ""
        tiktok_url   = row[2].strip() if len(row) > 2 else ""
        sender_email = row[3].strip() if len(row) > 3 else ""
        send_flag    = row[4].strip() if len(row) > 4 else ""
        status       = row[5].strip() if len(row) > 5 else ""

        if send_flag != "✅" or status == "已发送":
            continue

        account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[sent_count % len(EMAIL_ACCOUNTS)]
        subject = get_outreach_subject(name)
        body    = get_outreach_body(name, tiktok_url)

        print(f"  发送给 {name} <{to_email}>")
        ok = send_email(account, to_email, subject, body)

        if ok:
            update_cell(sheets, OUTBOX_SHEET, i, 6, "已发送")
            update_cell(sheets, OUTBOX_SHEET, i, 7, datetime.now().strftime("%Y-%m-%d %H:%M"))
            update_cell(sheets, OUTBOX_SHEET, i, 8, "0")
            sent_count += 1
            print(f"  ✅ 成功")
        else:
            update_cell(sheets, OUTBOX_SHEET, i, 6, "发送失败")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  共发送 {sent_count} 封开发邮件")

    # ── 2. 发跟进邮件 ─────────────────────────────────────────────
    print("\n📨 检查跟进邮件...")
    followup_count = 0

    # 重新读取（因为上面可能更新了状态）
    outbox_data = get_sheet_data(sheets, OUTBOX_SHEET)

    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 7:
            continue

        name         = row[0].strip() if len(row) > 0 else ""
        to_email     = row[1].strip() if len(row) > 1 else ""
        tiktok_url   = row[2].strip() if len(row) > 2 else ""
        sender_email = row[3].strip() if len(row) > 3 else ""
        status       = row[5].strip() if len(row) > 5 else ""
        sent_time    = row[6].strip() if len(row) > 6 else ""
        followup     = int(row[7].strip()) if len(row) > 7 and row[7].strip().isdigit() else 0

        # 条件：已发送 + 未回复 + 跟进次数 < 3 + 发出超过24小时
        if not (
            status == "已发送"
            and to_email.lower() not in replied_emails
            and followup < 3
            and sent_time
        ):
            continue

        try:
            sent_dt = datetime.strptime(sent_time, "%Y-%m-%d %H:%M")
            if datetime.now() - sent_dt < timedelta(hours=24):
                continue
        except Exception:
            continue

        account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[0]
        subject = get_followup_subject(name)
        body    = get_followup_body(name, tiktok_url)

        print(f"  跟进第{followup + 1}次：{name} <{to_email}>")
        ok = send_email(account, to_email, subject, body)

        if ok:
            update_cell(sheets, OUTBOX_SHEET, i, 8, str(followup + 1))
            followup_count += 1
            print(f"  ✅ 跟进成功")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  共发送 {followup_count} 封跟进邮件")
    print(f"\n🎉 完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
