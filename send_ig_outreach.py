"""
send_ig_outreach.py — 发 IG 开发邮件 + 跟进邮件

完成：
1. 自动确认当前 Spreadsheet 有「ig开发名单1」页签，没有就创建。
2. 「ig开发名单1」有 ✅ 且未发送/未回复 → 发 IG 开发邮件。
3. 如果达人邮箱已出现在「沟通管理」，把 IG 表 F 列改为「已回复」并停止发送。
4. 已发送 + 未回复 + 跟进次数 < 3 + 超过24小时 → 发跟进邮件。
"""

import mimetypes
import os
import random
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    REPLIES_SHEET,
    SEND_INTERVAL_MAX,
    SEND_INTERVAL_MIN,
    SHEET_ID,
)
from google_auth import get_creds
from template_ig import (
    IMAGE_CID,
    get_followup_body,
    get_followup_subject,
    get_outreach_html_body,
    get_outreach_plain_body,
    get_outreach_subject,
)

IG_OUTBOX_SHEET = "ig开发名单1"
IG_HEADERS = ["达人名字", "邮箱", "IG链接", "发件邮箱", "发送", "状态", "发送时间", "跟进次数"]
IMAGE_PATH = Path(__file__).resolve().parent / "assets" / "ig_product.jpg"

MAX_OUTREACH_PER_RUN = int(os.environ.get("MAX_IG_OUTREACH_PER_RUN", os.environ.get("MAX_OUTREACH_PER_RUN", "0")))
MAX_FOLLOWUPS_PER_RUN = int(os.environ.get("MAX_IG_FOLLOWUPS_PER_RUN", os.environ.get("MAX_FOLLOWUPS_PER_RUN", "0")))
MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT = int(
    os.environ.get("MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT", "3")
)
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def is_daily_limit_error(error: str) -> bool:
    error_lower = error.lower()
    return (
        "daily user sending limit exceeded" in error_lower
        or "5.4.5" in error_lower
        or "daily sending limit" in error_lower
    )


def row_value(row: list, index: int) -> str:
    return row[index].strip() if len(row) > index and row[index] else ""


def ensure_ig_sheet(sheets):
    spreadsheet = sheets.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(title))",
    ).execute()
    titles = {item["properties"]["title"] for item in spreadsheet.get("sheets", [])}
    if IG_OUTBOX_SHEET in titles:
        return

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": IG_OUTBOX_SHEET}}}]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{IG_OUTBOX_SHEET}!A1:H1",
        valueInputOption="RAW",
        body={"values": [IG_HEADERS]},
    ).execute()
    print(f"  ✅ 已创建页签：{IG_OUTBOX_SHEET}")


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


def get_account_by_email(email: str) -> dict:
    for acc in EMAIL_ACCOUNTS:
        if acc["email"].lower() == email.lower():
            return acc
    return EMAIL_ACCOUNTS[0]


def normalize_recipient(raw_email: str) -> tuple[str, str]:
    candidates = [part.strip() for part in re.split(r"[,;]", raw_email) if part.strip()]
    for candidate in candidates:
        if EMAIL_RE.match(candidate):
            if candidate != raw_email.strip():
                return candidate, f"邮箱包含多个值，使用第一个合法邮箱：{candidate}"
            return candidate, ""
    return "", f"邮箱格式无效：{raw_email[:80]}"


def replied_emails(replies_data: list) -> set[str]:
    emails = set()
    for row in replies_data[1:]:
        email = row_value(row, 2).lower()
        if email:
            emails.add(email)
    return emails


def mark_replied_if_needed(sheets, row_index: int, email: str, status: str, replied: set[str]) -> bool:
    if email.lower() not in replied:
        return False
    if status != "已回复":
        update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, "已回复")
        print(f"  ✅ 行{row_index} 已在沟通管理中，状态改为已回复：<{email}>")
    return True


def attach_inline_image(msg: MIMEMultipart, image_path: Path, content_id: str):
    with image_path.open("rb") as image_file:
        image_data = image_file.read()

    mime_type, _ = mimetypes.guess_type(str(image_path))
    if mime_type and mime_type.startswith("image/"):
        maintype, subtype = mime_type.split("/", 1)
        image_part = MIMEImage(image_data, _subtype=subtype)
    else:
        image_part = MIMEApplication(image_data)

    image_part.add_header("Content-ID", f"<{content_id}>")
    image_part.add_header("Content-Disposition", "inline", filename=image_path.name)
    msg.attach(image_part)


def send_html_email(
    account: dict,
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    image_path: Path,
) -> tuple[bool, str]:
    try:
        msg = MIMEMultipart("related")
        msg["From"] = account["email"]
        msg["To"] = to_email
        msg["Subject"] = subject

        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(plain_body, "plain", "utf-8"))
        alternative.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alternative)
        attach_inline_image(msg, image_path, IMAGE_CID)

        with smtplib.SMTP(account["smtp_host"], account["smtp_port"]) as server:
            server.starttls()
            server.login(account["email"], account["password"])
            server.sendmail(account["email"], to_email, msg.as_string())
        return True, ""
    except Exception as e:
        error = str(e)
        print(f"  ❌ 发送失败：{error}")
        return False, error


def send_plain_email(account: dict, to_email: str, subject: str, body: str) -> tuple[bool, str]:
    try:
        msg = MIMEMultipart()
        msg["From"] = account["email"]
        msg["To"] = to_email
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


def main():
    print(f"\n📤 开始发 IG 开发邮件 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds)
    ensure_ig_sheet(sheets)

    outbox_data = get_sheet_data(sheets, IG_OUTBOX_SHEET)
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    replied = replied_emails(replies_data)

    print(f"\n📧 检查 {IG_OUTBOX_SHEET}...")
    sent_count = 0
    attempted_count = 0
    consecutive_failures = {account["email"]: 0 for account in EMAIL_ACCOUNTS}
    blocked_accounts: set[str] = set()

    for row_index, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 5:
            continue

        name = row_value(row, 0)
        to_email = row_value(row, 1)
        ig_url = row_value(row, 2)
        sender_email = row_value(row, 3)
        send_flag = row_value(row, 4)
        status = row_value(row, 5)

        normalized_email, email_warning = normalize_recipient(to_email)
        if not normalized_email:
            if send_flag == "✅":
                print(f"  ⚠️ 第{row_index}行 {email_warning}")
                update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, email_warning)
            continue
        if email_warning:
            print(f"  ⚠️ 第{row_index}行 {email_warning}")
        to_email = normalized_email

        if mark_replied_if_needed(sheets, row_index, to_email, status, replied):
            continue
        if send_flag != "✅" or status in {"已发送", "已回复"}:
            continue
        if not IMAGE_PATH.exists():
            message = f"未发送：缺少图片文件 {IMAGE_PATH.relative_to(Path(__file__).resolve().parent)}"
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, message)
            print(f"  ⚠️ 第{row_index}行 {message}")
            continue

        if MAX_OUTREACH_PER_RUN > 0 and sent_count >= MAX_OUTREACH_PER_RUN:
            print(f"  已达到本次 IG 开发邮件上限 {MAX_OUTREACH_PER_RUN}，剩余下次继续")
            break

        account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[sent_count % len(EMAIL_ACCOUNTS)]
        if account["email"] in blocked_accounts:
            print(f"  ⚠️ {account['email']} 今日额度已满，跳过第{row_index}行 {name} <{to_email}>")
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, "未发送：发件账号今日额度已满")
            continue
        if consecutive_failures.get(account["email"], 0) >= MAX_CONSECUTIVE_FAILURES_PER_ACCOUNT:
            print(f"  ⚠️ {account['email']} 连续失败过多，本次暂停该账号，跳过第{row_index}行")
            continue

        subject = get_outreach_subject(name)
        plain_body = get_outreach_plain_body(name, ig_url)
        html_body = get_outreach_html_body(name, ig_url)

        print(f"  使用账号 {account['email']} 发送 IG 开发信给 {name} <{to_email}>")
        ok, error = send_html_email(account, to_email, subject, plain_body, html_body, IMAGE_PATH)
        attempted_count += 1

        if ok:
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, "已发送")
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 7, datetime.now().strftime("%Y-%m-%d %H:%M"))
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 8, "0")
            consecutive_failures[account["email"]] = 0
            sent_count += 1
            print("  ✅ 成功")
        else:
            consecutive_failures[account["email"]] = consecutive_failures.get(account["email"], 0) + 1
            if is_daily_limit_error(error):
                blocked_accounts.add(account["email"])
                update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, "未发送：发件账号今日额度已满")
                print(f"  ⚠️ {account['email']} 已达到 Gmail 当日发送上限，本次运行将跳过该账号")
            else:
                update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, f"发送失败：{error[:80]}")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  本次尝试 {attempted_count} 封，成功发送 {sent_count} 封 IG 开发邮件")
    print(f"  本次 blocked_accounts={sorted(blocked_accounts)}")

    print("\n📨 检查 IG 跟进邮件...")
    followup_count = 0
    outbox_data = get_sheet_data(sheets, IG_OUTBOX_SHEET)

    for row_index, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 7:
            continue

        name = row_value(row, 0)
        to_email = row_value(row, 1)
        ig_url = row_value(row, 2)
        sender_email = row_value(row, 3)
        status = row_value(row, 5)
        sent_time = row_value(row, 6)
        followup = int(row_value(row, 7)) if row_value(row, 7).isdigit() else 0

        normalized_email, email_warning = normalize_recipient(to_email)
        if not normalized_email:
            print(f"  ⚠️ 第{row_index}行 {email_warning}")
            continue
        if email_warning:
            print(f"  ⚠️ 第{row_index}行 {email_warning}")
        to_email = normalized_email

        if mark_replied_if_needed(sheets, row_index, to_email, status, replied):
            continue
        if MAX_FOLLOWUPS_PER_RUN > 0 and followup_count >= MAX_FOLLOWUPS_PER_RUN:
            print(f"  已达到本次 IG 跟进邮件上限 {MAX_FOLLOWUPS_PER_RUN}，剩余下次继续")
            break
        if not (status == "已发送" and followup < 3 and sent_time):
            continue

        try:
            sent_dt = datetime.strptime(sent_time, "%Y-%m-%d %H:%M")
            if datetime.now() - sent_dt < timedelta(hours=24):
                continue
        except Exception:
            continue

        account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[0]
        subject = get_followup_subject(name)
        body = get_followup_body(name, ig_url)

        print(f"  IG 跟进第{followup + 1}次：{name} <{to_email}>")
        ok, error = send_plain_email(account, to_email, subject, body)

        if ok:
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 8, str(followup + 1))
            followup_count += 1
            print("  ✅ 跟进成功")
        elif is_daily_limit_error(error):
            update_cell(sheets, IG_OUTBOX_SHEET, row_index, 6, f"额度已满 {datetime.now().strftime('%Y-%m-%d')}：待明日重试或换发件邮箱")

        wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
        print(f"  ⏱ 等待 {wait} 秒...")
        time.sleep(wait)

    print(f"  共发送 {followup_count} 封 IG 跟进邮件")
    print(f"\n🎉 完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
