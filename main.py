"""
main.py — 核心逻辑
GitHub Actions 每天运行一次，完成：
1. 检查「待发名单」有无新的 ✅ → 发邮件
2. 检查无回复达人 → 发跟进邮件
3. 抓取新回复 → AI处理 → 写入「沟通管理」
4. 检查「沟通管理」有无新的 ✅ → 发回复邮件
"""

import base64
import json
import os
import random
import re
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import google.generativeai as genai
from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    NEGOTIATION_CARDS,
    REPLIES_SHEET,
    OUTBOX_SHEET,
    SEND_INTERVAL_MAX,
    SEND_INTERVAL_MIN,
    SHEET_ID,
)
from google_auth import get_creds
from template import (
    get_followup_body,
    get_followup_subject,
    get_outreach_body,
    get_outreach_subject,
)

# ── 列定义 ────────────────────────────────────────────────────────

# 待发名单列（A~H）
OUTBOX_COLS = ["达人名字", "邮箱", "TikTok链接", "发件邮箱", "发送", "状态", "发送时间", "跟进次数"]
# 沟通管理列（A~J）
REPLIES_COLS = ["日期", "达人名字", "邮箱", "回复摘要", "过往沟通", "当前阶段", "状态", "你的指令", "AI生成回复", "发送"]

# ── 邮件发送 ──────────────────────────────────────────────────────

def get_account_by_email(email: str) -> dict:
    """根据邮箱地址找到对应账号配置"""
    for acc in EMAIL_ACCOUNTS:
        if acc["email"].lower() == email.lower():
            return acc
    return EMAIL_ACCOUNTS[0]  # 默认第一个


def send_email(account: dict, to_email: str, subject: str, body: str) -> bool:
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
        return True
    except Exception as e:
        print(f"  ❌ 发送失败：{e}")
        return False

# ── Google Sheets 工具 ────────────────────────────────────────────

def get_sheet_data(sheets, sheet_name: str) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:Z",
    ).execute()
    return result.get("values", [])


def ensure_headers(sheets, sheet_name: str, headers: list):
    data = get_sheet_data(sheets, sheet_name)
    if not data or data[0] != headers:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()


def update_cell(sheets, sheet_name: str, row: int, col: int, value: str):
    """row/col 从1开始"""
    col_letter = chr(64 + col)
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!{col_letter}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def append_row(sheets, sheet_name: str, row: list):
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

# ── Gemini AI ─────────────────────────────────────────────────────

def load_negotiation_cards() -> str:
    if os.path.exists(NEGOTIATION_CARDS):
        with open(NEGOTIATION_CARDS, encoding="utf-8") as f:
            return f.read()
    return ""


def ai_process_reply(reply_body: str, history: str, cards: str) -> dict:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一个 TikTok 达人合作 BD 助手。
以下是达人发来的回复邮件，请完成三件事。

邮件内容：
{reply_body}

过往沟通记录：
{history if history else "无"}

请严格按以下 JSON 格式返回，不要加任何其他内容：
{{
  "summary": "用1-2句中文概括回复的核心内容",
  "stage": "从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他",
  "suggested_reply": "根据阶段和筹码库，用英文写一封完整的建议回复邮件"
}}

可用筹码库：
{cards}
"""
    response = model.generate_content(prompt)
    text = response.text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        return {"summary": "AI解析失败，请手动查看", "stage": "其他", "suggested_reply": ""}


def ai_regenerate_reply(instruction: str, history: str, stage: str, cards: str) -> str:
    """根据你的中文指令重新生成英文回复"""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一个 TikTok 达人合作 BD 助手。

当前谈判阶段：{stage}
过往沟通记录：{history if history else "无"}
我的指令（中文）：{instruction}

可用筹码库：
{cards}

请根据以上信息，用英文写一封完整的专业回复邮件。
只返回邮件正文，不要加任何说明。
"""
    response = model.generate_content(prompt)
    return response.text.strip()

# ── 抓取 Gmail 回复 ───────────────────────────────────────────────

def fetch_new_replies(gmail) -> list[dict]:
    since = (datetime.utcnow() - timedelta(days=1)).strftime("%Y/%m/%d")
    all_replies = []

    for account in EMAIL_ACCOUNTS:
        query = f"to:{account['email']} after:{since}"
        try:
            result = gmail.users().messages().list(
                userId="me", q=query, maxResults=50
            ).execute()
            messages = result.get("messages", [])
        except Exception as e:
            print(f"  ⚠️ 读取 {account['email']} 失败：{e}")
            continue

        for msg_ref in messages:
            msg = gmail.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", headers.get("From", ""))
            from_email = from_match.group(0) if from_match else ""
            date_str = headers.get("Date", "")

            body = ""
            payload = msg["payload"]
            if "parts" in payload:
                for part in payload["parts"]:
                    if part["mimeType"] == "text/plain":
                        data = part["body"].get("data", "")
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                        break
            elif "body" in payload:
                data = payload["body"].get("data", "")
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

            body = body[:1500].strip()
            if from_email and body:
                all_replies.append({
                    "from_email": from_email,
                    "date": date_str,
                    "body": body,
                })

    return all_replies


def get_history_for_email(sheets, email: str) -> str:
    data = get_sheet_data(sheets, REPLIES_SHEET)
    items = []
    for row in data[1:]:
        if len(row) > 2 and row[2].strip().lower() == email.strip().lower():
            if len(row) > 3:
                items.append(f"[{row[0]}] {row[3]}")
    return " | ".join(items[-3:])

# ── 主流程 ────────────────────────────────────────────────────────

def main():
    print(f"\n🚀 开始运行 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds = get_creds()
    gmail  = build("gmail",  "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards  = load_negotiation_cards()

    # 确保表头存在
    ensure_headers(sheets, OUTBOX_SHEET,  OUTBOX_COLS)
    ensure_headers(sheets, REPLIES_SHEET, REPLIES_COLS)

    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)

    # ── 1. 处理「待发名单」中标了 ✅ 的行 ────────────────────────
    print("\n📤 检查待发名单...")
    sent_count = 0

    for i, row in enumerate(outbox_data[1:], start=2):  # 从第2行开始，跳过表头
        if len(row) < 5:
            continue

        name        = row[0].strip() if len(row) > 0 else ""
        to_email    = row[1].strip() if len(row) > 1 else ""
        tiktok_url  = row[2].strip() if len(row) > 2 else ""
        sender_email = row[3].strip() if len(row) > 3 else ""
        send_flag   = row[4].strip() if len(row) > 4 else ""
        status      = row[5].strip() if len(row) > 5 else ""

        # 只处理标了 ✅ 且还没发送的
        if send_flag == "✅" and status != "已发送":
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

            if sent_count < len(outbox_data):
                wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
                print(f"  ⏱ 等待 {wait} 秒...")
                time.sleep(wait)

    # ── 2. 无回复达人发跟进邮件 ──────────────────────────────────
    print("\n📨 检查跟进邮件...")

    # 收集已回复的邮箱
    replied_emails = set()
    for row in replies_data[1:]:
        if len(row) > 2:
            replied_emails.add(row[2].strip().lower())

    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 7:
            continue

        name       = row[0].strip()
        to_email   = row[1].strip()
        tiktok_url = row[2].strip()
        sender_email = row[3].strip() if len(row) > 3 else ""
        status     = row[5].strip() if len(row) > 5 else ""
        sent_time  = row[6].strip() if len(row) > 6 else ""
        followup   = int(row[7].strip()) if len(row) > 7 and row[7].strip().isdigit() else 0

        # 条件：已发送 + 未回复 + 跟进次数<1 + 发出超过24小时
        if (
            status == "已发送"
            and to_email.lower() not in replied_emails
            and followup < 1
            and sent_time
        ):
            try:
                sent_dt = datetime.strptime(sent_time, "%Y-%m-%d %H:%M")
                if datetime.now() - sent_dt < timedelta(hours=24):
                    continue
            except Exception:
                continue

            account = get_account_by_email(sender_email) if sender_email else EMAIL_ACCOUNTS[0]
            subject = get_followup_subject(name)
            body    = get_followup_body(name, tiktok_url)

            print(f"  跟进 {name} <{to_email}>")
            ok = send_email(account, to_email, subject, body)

            if ok:
                update_cell(sheets, OUTBOX_SHEET, i, 8, str(followup + 1))
                print(f"  ✅ 跟进成功")

            wait = random.randint(SEND_INTERVAL_MIN, SEND_INTERVAL_MAX)
            time.sleep(wait)

    # ── 3. 抓取新回复，AI处理，写入「沟通管理」─────────────────
    print("\n📬 抓取新回复...")
    new_replies = fetch_new_replies(gmail)
    print(f"  找到 {len(new_replies)} 封新回复")

    # 已记录过的邮件（避免重复写入）
    recorded = set()
    for row in replies_data[1:]:
        if len(row) > 2:
            recorded.add(row[2].strip().lower())

    for reply in new_replies:
        if reply["from_email"].lower() in recorded:
            continue

        print(f"  AI处理：{reply['from_email']}")
        history = get_history_for_email(sheets, reply["from_email"])

        try:
            ai_result = ai_process_reply(reply["body"], history, cards)
        except Exception as e:
            print(f"  ⚠️ AI失败：{e}")
            ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

        # 从待发名单匹配达人名字
        name = ""
        for row in outbox_data[1:]:
            if len(row) > 1 and row[1].strip().lower() == reply["from_email"].lower():
                name = row[0].strip()
                break

        new_row = [
            datetime.now().strftime("%Y-%m-%d"),
            name,
            reply["from_email"],
            ai_result["summary"],
            history,
            ai_result["stage"],
            "待回复",
            "",
            ai_result["suggested_reply"],
            "",
        ]
        append_row(sheets, REPLIES_SHEET, new_row)
        recorded.add(reply["from_email"].lower())
        time.sleep(1)

    # ── 4. 处理「沟通管理」中标了 ✅ 的行 → 发回复邮件 ──────────
    print("\n💬 检查待发回复...")
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)  # 重新读取（刚写入了新数据）

    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) < 10:
            continue

        to_email    = row[2].strip() if len(row) > 2 else ""
        stage       = row[5].strip() if len(row) > 5 else ""
        instruction = row[7].strip() if len(row) > 7 else ""
        ai_reply    = row[8].strip() if len(row) > 8 else ""
        send_flag   = row[9].strip() if len(row) > 9 else ""
        history     = row[4].strip() if len(row) > 4 else ""

        if send_flag != "✅":
            continue

        # 如果有指令，重新生成回复
        final_reply = ai_reply
        if instruction:
            try:
                print(f"  根据指令重新生成回复：{instruction}")
                final_reply = ai_regenerate_reply(instruction, history, stage, cards)
                update_cell(sheets, REPLIES_SHEET, i, 9, final_reply)
            except Exception as e:
                print(f"  ⚠️ 重新生成失败：{e}")

        if not final_reply or not to_email:
            continue

        account = EMAIL_ACCOUNTS[0]  # 回复统一用第一个账号，可以改
        subject = "Re: Zdeer Collaboration"
        print(f"  回复 <{to_email}>")
        ok = send_email(account, to_email, subject, final_reply)

        if ok:
            update_cell(sheets, REPLIES_SHEET, i, 7, "已回复")
            update_cell(sheets, REPLIES_SHEET, i, 10, "")  # 清空✅防止重复发
            print(f"  ✅ 回复成功")

        wait = random.randint(15, 30)
        time.sleep(wait)

    print(f"\n🎉 运行完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
