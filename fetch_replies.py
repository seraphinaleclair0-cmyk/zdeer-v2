"""
fetch_replies.py — 只负责收邮件、更新「沟通管理」
自动触发：每天北京时间 8:00 / 12:00 / 17:00
手动触发：随时可以点 Run workflow

完成：
1. 扫描4个Gmail收件箱，抓取新回复
2. 新达人 → 待开发名单改状态「已回复」→ 沟通管理新建一行
3. 已有达人再次回复 → 更新D/E/F/G/I列
4. 扫描4个Gmail已发邮件 → 追加E列过往沟通
5. 更新时间戳
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta

import google.generativeai as genai
from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    NEGOTIATION_CARDS,
    REPLIES_SHEET,
    OUTBOX_SHEET,
    SHEET_ID,
)
from google_auth import get_creds

# ── 常量 ──────────────────────────────────────────────────────────
REPLIES_COLS = [
    "日期", "达人名字", "邮箱", "回复摘要", "过往沟通", "当前阶段", "状态",
    "你的指令", "AI生成回复", "发送", "收件邮箱", "备注",
    "Message-ID", "Subject",
]
CONFIG_SHEET = "系统配置"

# ── 时间戳 ────────────────────────────────────────────────────────

def get_last_run_time(sheets) -> datetime:
    try:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{CONFIG_SHEET}!A2",
        ).execute()
        values = result.get("values", [])
        if values and values[0]:
            return datetime.strptime(values[0][0], "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return datetime.utcnow() - timedelta(hours=24)


def save_last_run_time(sheets):
    try:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CONFIG_SHEET}!A1:B1",
            valueInputOption="RAW",
            body={"values": [["上次收邮件时间（UTC）", "上次发邮件时间（UTC）"]]},
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CONFIG_SHEET}!A2",
            valueInputOption="RAW",
            body={"values": [[datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")]]},
        ).execute()
    except Exception as e:
        print(f"  ⚠️ 保存时间戳失败：{e}")

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


def append_row(sheets, sheet_name: str, row: list):
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def ensure_headers(sheets, sheet_name: str, headers: list):
    data = get_sheet_data(sheets, sheet_name)
    existing = data[0] if data else []
    merged = headers[:]
    for index, value in enumerate(existing):
        if index < len(merged) and value:
            merged[index] = value
    if existing[:len(headers)] != merged:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{sheet_name}!A1:N1",
            valueInputOption="RAW",
            body={"values": [merged]},
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

# ── Gemini ────────────────────────────────────────────────────────

def load_negotiation_cards() -> str:
    if os.path.exists(NEGOTIATION_CARDS):
        with open(NEGOTIATION_CARDS, encoding="utf-8") as f:
            return f.read()
    return ""


def ai_process_reply(reply_body: str, history: str, cards: str) -> dict:
    time.sleep(4)
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = f"""你是一个 TikTok 达人合作 BD 助手。
以下是达人发来的回复邮件，请完成三件事。

语言规则：
- summary 必须写中文，方便团队内部阅读。
- stage 必须写中文，只能从给定选项中选择。
- suggested_reply 必须写自然、专业、简洁的美式英语。
- suggested_reply 不要出现中文，不要中英混杂。
- 不要编造预算、物流、合同、付款、库存等未提供的信息。

邮件内容：
{reply_body}

过往沟通记录：
{history if history else "无"}

请严格按以下 JSON 格式返回，不要加任何其他内容：
{{
  "summary": "用1-2句中文概括回复的核心内容",
  "stage": "从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他",
  "suggested_reply": "根据阶段和筹码库，用美式英语写一封完整的建议回复邮件"
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


def ai_summarize_sent(body: str) -> str:
    time.sleep(4)
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = f"""请用1句中文简洁总结以下邮件的核心内容，供内部记录使用。
只返回总结内容，不要加任何前缀或说明。

邮件内容：
{body[:1000]}
"""
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        return "（摘要生成失败）"

# ── Gmail 工具 ────────────────────────────────────────────────────

def _extract_body(msg: dict) -> str:
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
    return body[:1500].strip()


def fetch_new_replies(gmail, last_run: datetime) -> list[dict]:
    since = last_run.strftime("%Y/%m/%d")
    all_replies = []
    own_emails = [acc["email"].lower() for acc in EMAIL_ACCOUNTS]

    for account in EMAIL_ACCOUNTS:
        query = f"to:{account['email']} after:{since}"
        try:
            result = gmail.users().messages().list(
                userId="me", q=query, maxResults=50
            ).execute()
            messages = result.get("messages", [])
        except Exception as e:
            print(f"  ⚠️ 读取 {account['email']} 收件箱失败：{e}")
            continue

        for msg_ref in messages:
            msg = gmail.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", headers.get("From", ""))
            from_email = from_match.group(0) if from_match else ""

            if from_email.lower() in own_emails:
                continue

            body = _extract_body(msg)
            if from_email and body:
                all_replies.append({
                    "from_email": from_email,
                    "to_email": account["email"],
                    "date": headers.get("Date", ""),
                    "body": body,
                    "message_id": headers.get("Message-ID", ""),
                    "subject": headers.get("Subject", ""),
                })

    return all_replies


def fetch_sent_emails(gmail, last_run: datetime) -> list[dict]:
    since = last_run.strftime("%Y/%m/%d")
    all_sent = []
    own_emails = [acc["email"].lower() for acc in EMAIL_ACCOUNTS]

    for account in EMAIL_ACCOUNTS:
        query = f"from:{account['email']} after:{since} in:sent"
        try:
            result = gmail.users().messages().list(
                userId="me", q=query, maxResults=50
            ).execute()
            messages = result.get("messages", [])
        except Exception as e:
            print(f"  ⚠️ 读取 {account['email']} 已发邮件失败：{e}")
            continue

        for msg_ref in messages:
            msg = gmail.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            to_match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", headers.get("To", ""))
            to_email = to_match.group(0) if to_match else ""

            if to_email.lower() in own_emails:
                continue

            body = _extract_body(msg)
            if to_email and body:
                all_sent.append({
                    "to_email": to_email,
                    "from_email": account["email"],
                    "date": headers.get("Date", ""),
                    "body": body,
                })

    return all_sent

# ── 查找工具 ──────────────────────────────────────────────────────

def find_in_outbox(outbox_data: list, email: str) -> tuple:
    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) > 1 and row[1].strip().lower() == email.strip().lower():
            return i, {
                "name": row[0].strip() if len(row) > 0 else "",
                "email": row[1].strip(),
                "sender_email": row[3].strip() if len(row) > 3 else "",
            }
    return 0, {}


def find_in_replies(replies_data: list, email: str) -> int:
    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email.strip().lower():
            return i
    return 0

# ── 主流程 ────────────────────────────────────────────────────────

def main():
    print(f"\n📬 开始收邮件 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = get_creds()
    gmail  = build("gmail",  "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards  = load_negotiation_cards()

    last_run = get_last_run_time(sheets)
    print(f"  上次收邮件时间（UTC）：{last_run.strftime('%Y-%m-%d %H:%M:%S')}")

    ensure_headers(sheets, REPLIES_SHEET, REPLIES_COLS)

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    today = datetime.now().strftime("%Y-%m-%d")

    # ── 1. 抓取收件箱新回复 ───────────────────────────────────────
    print("\n📥 扫描收件箱...")
    new_replies = fetch_new_replies(gmail, last_run)
    print(f"  找到 {len(new_replies)} 封新邮件")

    for reply in new_replies:
        from_email = reply["from_email"].lower()
        existing_row = find_in_replies(replies_data, from_email)

        # 获取过往沟通
        history = ""
        if existing_row and len(replies_data[existing_row - 1]) > 4:
            history = replies_data[existing_row - 1][4].strip()

        # Gemini 处理
        try:
            ai_result = ai_process_reply(reply["body"], history, cards)
        except Exception as e:
            print(f"  ⚠️ AI失败：{e}")
            ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

        summary_entry = f"{today} 达人回复：{ai_result['summary']}"

        if existing_row:
            # 已有达人 → 更新 D/E/F/G/I/M/N 列，不动 H/J/K/L
            print(f"  更新已有达人：{from_email}")
            update_cell(sheets, REPLIES_SHEET, existing_row, 4, summary_entry)       # D 回复摘要
            append_to_history(sheets, existing_row, summary_entry)                    # E 过往沟通追加
            update_cell(sheets, REPLIES_SHEET, existing_row, 6, ai_result["stage"])  # F 当前阶段
            update_cell(sheets, REPLIES_SHEET, existing_row, 7, "待回复")             # G 状态
            if ai_result["suggested_reply"]:
                update_cell(sheets, REPLIES_SHEET, existing_row, 9, ai_result["suggested_reply"])  # I AI回复
            update_cell(sheets, REPLIES_SHEET, existing_row, 13, reply.get("message_id", ""))  # M Message-ID
            update_cell(sheets, REPLIES_SHEET, existing_row, 14, reply.get("subject", ""))     # N Subject
        else:
            # 新达人 → 查待开发名单
            outbox_row_i, outbox_info = find_in_outbox(outbox_data, from_email)
            name = outbox_info.get("name", "")
            receiver_email = reply.get("to_email", "")

            if outbox_row_i:
                print(f"  待开发名单找到 {name}，改状态为已回复")
                update_cell(sheets, OUTBOX_SHEET, outbox_row_i, 6, "已回复")

            new_row = [
                today,                           # A 日期
                name,                            # B 达人名字
                reply["from_email"],              # C 邮箱
                summary_entry,                   # D 回复摘要
                summary_entry,                   # E 过往沟通
                ai_result["stage"],               # F 当前阶段
                "待回复",                          # G 状态
                "",                               # H 你的指令
                ai_result["suggested_reply"],     # I AI生成回复
                "",                               # J 发送
                receiver_email,                  # K 收件邮箱
                "",                               # L 用户自用
                reply.get("message_id", ""),      # M 原邮件 Message-ID
                reply.get("subject", ""),         # N 原邮件 Subject
            ]
            append_row(sheets, REPLIES_SHEET, new_row)
            print(f"  ✅ 新建行：{reply['from_email']}")

        replies_data = get_sheet_data(sheets, REPLIES_SHEET)
        time.sleep(1)

    # ── 2. 扫描已发邮件，追加E列 ─────────────────────────────────
    print("\n📤 扫描已发邮件...")
    sent_emails = fetch_sent_emails(gmail, last_run)
    print(f"  找到 {len(sent_emails)} 封已发邮件")

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)

    for sent in sent_emails:
        to_email = sent["to_email"].lower()
        print(f"  处理已发邮件 → {to_email}")
        summary = ai_summarize_sent(sent["body"])
        entry = f"{today} 我回复：{summary}"

        existing_row = find_in_replies(replies_data, to_email)
        if existing_row:
            append_to_history(sheets, existing_row, entry)
            print(f"  ✅ 追加到E列")
            replies_data = get_sheet_data(sheets, REPLIES_SHEET)
            continue

        # 两边都没有 → 新建一行
        outbox_row_i, outbox_info = find_in_outbox(outbox_data, to_email)
        name = outbox_info.get("name", "")
        new_row = [
            today,
            name,
            sent["to_email"],
            f"{today} 我发邮：{summary}",
            f"{today} 我发邮：{summary}",
            "",
            "待回复",
            "",
            "",
            "",
            sent.get("from_email", ""),
        ]
        append_row(sheets, REPLIES_SHEET, new_row)
        print(f"  ✅ 新建行（主动发出）：{sent['to_email']}")
        replies_data = get_sheet_data(sheets, REPLIES_SHEET)
        time.sleep(1)

    # ── 保存时间戳 ────────────────────────────────────────────────
    save_last_run_time(sheets)
    print(f"\n🎉 收邮件完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
