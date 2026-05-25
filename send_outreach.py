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
import os
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

MAX_OUTREACH_PER_RUN = int(os.environ.get("MAX_OUTREACH_PER_RUN", "40"))
MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT = int(
    os.environ.get("MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT", "3")
)

def is_daily_limit_error(error: str) -> bool:
    lowered = error.lower()
    return "daily user sending limit exceeded" in lowered or "5.4.5" in lowered

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


def send_email(account: dict, to_email: str, subject: str, body: str) -> tuple[bool, str]:
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
        return True, ""
    except Exception as e:
        error = str(e)
        print(f"  ❌ 发送失败：{error}")
        return False, error

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
    attempted_count = 0
    consecutive_failures = {account["email"]: 0 for account in EMAIL_ACCOUNTS}
    quota_limited_accounts: set[str] = set()
    today = datetime.now().strftime("%Y-%m-%d")

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
        if "额度已满" in status and today in status:
            continue

        if sent_count >= MAX_OUTREACH_PER_RUN:
            print(f"  已达到本次开发邮件上限 {MAX_OUTREACH_PER_RUN}，剩余下次继续")
            break

        account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[sent_count % len(EMAIL_ACCOUNTS)]
        if account["email"] in quota_limited_accounts:
            print(f"  ⚠️ {account['email']} 今日额度已满，跳过第{i}行")
            update_cell(sheets, OUTBOX_SHEET, i, 6, f"额度已满 {today}：待明日重试或换发件邮箱")
            continue

        if consecutive_failures.get(account["email"], 0) >= MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT:
            print(f"  ⚠️ {account['email']} 连续失败过多，本次暂停该账号，跳过第{i}行")
            continue

        subject = get_outreach_subject(name)
        body    = get_outreach_body(name, tiktok_url)

        print(f"  发送给 {name} <{to_email}>")
        ok, error = send_email(account, to_email, subject, body)
        attempted_count += 1

        if ok:
            update_cell(sheets, OUTBOX_SHEET, i, 6, "已发送")
            update_cell(sheets, OUTBOX_SHEET, i, 7, datetime.now().strftime("%Y-%m-%d %H:%M"))
            update_cell(sheets, OUTBOX_SHEET, i, 8, "0")
            consecutive_failures[account["email"]] = 0
            sent_count += 1
            print(f"  ✅ 成功")
        else:
            consecutive_failures[account["email"]] = consecutive_failures.get(account["email"], 0) + 1
            if is_daily_limit_error(error):
                quota_limited_accounts.add(account["email"])
                update_cell(sheets, OUTBOX_SHEET, i, 6, f"额度已满 {today}：待明日重试或换发件邮箱")
            else:
                update_cell(sheets, OUTBOX_SHEET, i, 6, f"发送失败：{error[:80]}")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  本次尝试 {attempted_count} 封，成功发送 {sent_count} 封开发邮件")

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
        ok, error = send_email(account, to_email, subject, body)

        if ok:
            update_cell(sheets, OUTBOX_SHEET, i, 8, str(followup + 1))
            followup_count += 1
            print(f"  ✅ 跟进成功")
        elif is_daily_limit_error(error):
            update_cell(sheets, OUTBOX_SHEET, i, 6, f"额度已满 {datetime.now().strftime('%Y-%m-%d')}：待明日重试或换发件邮箱")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  共发送 {followup_count} 封跟进邮件")
    print(f"\n🎉 完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
