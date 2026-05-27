"""
send_replies.py — 只处理「沟通管理」中打了 ✅ 的行
在原邮件线程里回复达人，不碰待发名单
手动触发：打了 ✅ 后去 GitHub Actions 点 Run workflow
"""

import re
import smtplib
import time
import random
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    REPLIES_SHEET,
    SHEET_ID,
    SEND_INTERVAL_MIN,
    SEND_INTERVAL_MAX,
)
from google_auth import get_creds

# ── Sheets 工具 ───────────────────────────────────────────────────

def get_sheet_data(sheets, sheet_name: str) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:N",
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


def append_to_history(sheets, row_index: int, new_entry: str):
    """往沟通管理E列追加一条记录，用 | 分隔"""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!E{row_index}",
    ).execute()
    existing = ""
    values = result.get("values", [])
    if values and values[0]:
        existing = values[0][0].strip()
    updated = f"{existing} | {new_entry}" if existing else new_entry
    update_cell(sheets, REPLIES_SHEET, row_index, 5, updated)

# ── 邮件发送 ──────────────────────────────────────────────────────

def get_account_by_email(email: str) -> dict:
    for acc in EMAIL_ACCOUNTS:
        if acc["email"].lower() == email.lower():
            return acc
    return EMAIL_ACCOUNTS[0]


def send_reply_in_thread(
    account: dict,
    to_email: str,
    original_subject: str,
    original_message_id: str,
    body: str,
) -> bool:
    """在原邮件线程里回复，带上 In-Reply-To 和 References 头"""
    try:
        msg = MIMEMultipart()
        msg["From"]    = account["email"]
        msg["To"]      = to_email
        msg["Subject"] = original_subject if original_subject.startswith("Re:") \
                         else f"Re: {original_subject}"

        # 关键：让邮件客户端把这封归入同一线程
        if original_message_id:
            msg["In-Reply-To"] = original_message_id
            msg["References"]  = original_message_id

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
    print(f"\n💬 开始发回复邮件 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = get_creds()
    sheets = build("sheets", "v4", credentials=creds)

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    sent_count   = 0

    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) < 10:
            continue

        to_email    = row[2].strip()  if len(row) > 2  else ""
        ai_reply    = row[8].strip()  if len(row) > 8  else ""
        send_flag   = row[9].strip()  if len(row) > 9  else ""
        receiver    = row[10].strip() if len(row) > 10 else ""
        message_id  = row[12].strip() if len(row) > 12 else ""  # M列
        subject     = row[13].strip() if len(row) > 13 else ""  # N列

        if send_flag != "✅":
            continue

        if not ai_reply:
            print(f"  ⚠️ 行{i} 打了✅但I列没有草稿，跳过")
            continue

        if not to_email:
            print(f"  ⚠️ 行{i} 邮箱为空，跳过")
            continue

        # 用收件邮箱对应的账号回复
        account = get_account_by_email(receiver) if receiver else EMAIL_ACCOUNTS[0]

        # subject 兜底
        if not subject:
            subject = "Zdeer Collaboration"

        print(f"  回复 <{to_email}> via {account['email']}")
        print(f"  主题：Re: {subject}")
        print(f"  线程ID：{message_id[:40]}..." if message_id else "  ⚠️ 无 Message-ID，将作为新邮件发送")

        ok = send_reply_in_thread(
            account      = account,
            to_email     = to_email,
            original_subject    = subject,
            original_message_id = message_id,
            body         = ai_reply,
        )

        if ok:
            today = datetime.now().strftime("%Y-%m-%d")
            update_cell(sheets, REPLIES_SHEET, i, 7,  "已回复")  # G 状态
            update_cell(sheets, REPLIES_SHEET, i, 10, "")         # J 清空✅防止重复发
            append_to_history(sheets, i, f"{today} 我回复：（已发送）")
            sent_count += 1
            print(f"  ✅ 发送成功")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"\n🎉 完成 — 共发送 {sent_count} 封 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
