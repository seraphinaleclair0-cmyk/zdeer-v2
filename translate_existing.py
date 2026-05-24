"""
Rebuild "沟通管理" from unreplied inbound Gmail messages since May 21, 2026.

This is not a translator. It reads original emails sent to the four configured
brand inboxes, excludes bounces/system mail, keeps only conversations that have
not been replied to after the latest inbound message, asks AI to produce
Chinese internal notes, then rewrites the communication sheet. It never sends
email.
"""

import base64
import json
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import google.generativeai as genai
from googleapiclient.discovery import build

from config import EMAIL_ACCOUNTS, GEMINI_API_KEY, GEMINI_MODEL, REPLIES_SHEET, SHEET_ID
from google_auth import get_creds


REPLIES_COLS = ["日期", "达人名字", "邮箱", "回复摘要", "过往沟通", "当前阶段", "状态", "你的指令", "AI生成回复", "发送", "收件邮箱"]
GMAIL_DATE_QUERY = "after:2026/5/20"
MAX_MESSAGES_PER_ACCOUNT = 200
MAX_BODY_CHARS = 1800


def own_emails() -> set[str]:
    return {account["email"].lower() for account in EMAIL_ACCOUNTS}


def extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    return match.group(0).lower() if match else ""


def is_noise_email(email: str) -> bool:
    if not email:
        return True
    local = email.split("@", 1)[0].lower()
    return local in {"no-reply", "noreply", "mailer-daemon", "postmaster"} or local.startswith("no-reply")


def is_noise_message(subject: str, body: str) -> bool:
    text = f"{subject}\n{body}".lower()
    noise_patterns = [
        "delivery status notification",
        "message not delivered",
        "mail delivery failed",
        "undeliverable",
        "address not found",
        "mailer-daemon",
        "google workspace",
        "security alert",
        "verification code",
        "out of office",
        "automatic reply",
        "auto-reply",
    ]
    return any(pattern in text for pattern in noise_patterns)


def decode_body_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")


def extract_body(msg: dict) -> str:
    payload = msg.get("payload", {})
    parts = payload.get("parts", [])

    for part in parts:
        if part.get("mimeType") == "text/plain":
            body = decode_body_part(part)
            if body:
                return body[:MAX_BODY_CHARS].strip()

    for part in parts:
        for sub_part in part.get("parts", []):
            if sub_part.get("mimeType") == "text/plain":
                body = decode_body_part(sub_part)
                if body:
                    return body[:MAX_BODY_CHARS].strip()

    return decode_body_part(payload)[:MAX_BODY_CHARS].strip()


def gmail_headers(msg: dict) -> dict:
    return {
        item["name"].lower(): item["value"]
        for item in msg.get("payload", {}).get("headers", [])
    }


def parse_message_datetime(date_header: str) -> datetime:
    try:
        return parsedate_to_datetime(date_header).replace(tzinfo=None)
    except Exception:
        return datetime.min


def fetch_inbound_messages(gmail) -> dict[str, list[dict]]:
    grouped = {}
    seen_ids = set()
    accounts = own_emails()

    for account in sorted(accounts):
        query = f"to:{account} {GMAIL_DATE_QUERY}"
        print(f"抓取收件箱：{query}")
        try:
            result = gmail.users().messages().list(
                userId="me",
                q=query,
                maxResults=MAX_MESSAGES_PER_ACCOUNT,
            ).execute()
        except Exception as e:
            print(f"  ⚠️ 搜索失败：{e}")
            continue

        for msg_ref in result.get("messages", []):
            msg_id = msg_ref["id"]
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            try:
                msg = gmail.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="full",
                ).execute()
            except Exception as e:
                print(f"  ⚠️ 读取失败 {msg_id}: {e}")
                continue

            headers = gmail_headers(msg)
            from_email = extract_email(headers.get("from", ""))
            to_email = extract_email(headers.get("to", "")) or account
            if from_email in accounts or is_noise_email(from_email):
                continue

            body = extract_body(msg)
            if not body:
                continue
            if is_noise_message(headers.get("subject", ""), body):
                continue

            grouped.setdefault(from_email, []).append({
                "date": parse_message_datetime(headers.get("date", "")),
                "date_text": headers.get("date", ""),
                "from": from_email,
                "to": to_email,
                "body": body,
            })

    for messages in grouped.values():
        messages.sort(key=lambda item: item["date"])

    return grouped


def has_replied_after(gmail, creator_email: str, latest_inbound: datetime) -> bool:
    accounts = own_emails()
    after_query = latest_inbound.strftime("%Y/%m/%d") if latest_inbound != datetime.min else "2026/5/20"

    for account in sorted(accounts):
        query = f"from:{account} to:{creator_email} after:{after_query}"
        try:
            result = gmail.users().messages().list(
                userId="me",
                q=query,
                maxResults=20,
            ).execute()
        except Exception as e:
            print(f"  ⚠️ 检查已回复失败 {query}: {e}")
            continue

        for msg_ref in result.get("messages", []):
            try:
                msg = gmail.users().messages().get(
                    userId="me",
                    id=msg_ref["id"],
                    format="metadata",
                    metadataHeaders=["Date", "From", "To", "Subject"],
                ).execute()
            except Exception:
                continue

            headers = gmail_headers(msg)
            sent_at = parse_message_datetime(headers.get("date", ""))
            to_email = extract_email(headers.get("to", ""))
            if to_email == creator_email and sent_at > latest_inbound:
                return True

    return False


def keep_unreplied_threads(gmail, grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    unreplied = {}
    for creator_email, messages in grouped.items():
        latest_inbound = messages[-1]["date"] if messages else datetime.min
        if has_replied_after(gmail, creator_email, latest_inbound):
            print(f"已回复，跳过：{creator_email}")
            continue
        unreplied[creator_email] = messages
    return unreplied


def build_ai_prompt(creator_email: str, messages: list[dict]) -> str:
    blocks = []
    for index, msg in enumerate(messages, start=1):
        blocks.append(
            f"""[{index}]
时间：{msg['date_text']}
From：{msg['from']}
To：{msg['to']}
正文：
{msg['body']}
"""
        )

    return f"""你是 Zdeer TikTok 达人合作 BD 助手。
下面是达人邮箱 {creator_email} 从 2026年5月21日到现在发给我方品牌邮箱、且我方尚未回复的原始来信。

请基于这些原始来信，重建 Google Sheet「沟通管理」的一行内容。

要求：
- 只基于原始邮件内容，不要编造预算、物流、合同、付款、库存、样品状态。
- 如果是系统通知、退信、Google推广邮件等，不要当成达人回复，status 设为「无需回复」。
- summary：写 D 列，用1-2句中文总结最近一次有效达人回复。
- history：写 E 列，按时间顺序写中文沟通记录，保留金额、佣金、WhatsApp、合同、地址、产品链接等关键信息。
- stage：写 F 列，只能选「初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他」。
- status：写 G 列，只能选「待回复 / 已回复 / 已成交 / 已放弃 / 无需回复」。
- suggested_reply：写 I 列；如果达人需要我方回复，用自然、专业、简洁的美式英语写回复草稿；否则留空。
- 中文内部记录必须是中文；给达人看的 suggested_reply 必须是英文。
- 输出严格 JSON，不要加 Markdown。

JSON格式：
{{
  "summary": "中文摘要",
  "history": "中文过往沟通",
  "stage": "阶段",
  "status": "状态",
  "suggested_reply": "英文回复草稿或空字符串"
}}

原始来信：
{chr(10).join(blocks)}
"""


def latest_date(messages: list[dict]) -> str:
    if not messages or messages[-1]["date"] == datetime.min:
        return datetime.now().strftime("%Y-%m-%d")
    return messages[-1]["date"].strftime("%Y-%m-%d")


def latest_receiver(messages: list[dict]) -> str:
    return messages[-1]["to"] if messages else ""


def rebuild_row(model, creator_email: str, messages: list[dict]) -> list[str]:
    try:
        response = model.generate_content(build_ai_prompt(creator_email, messages))
        text = re.sub(r"```json|```", "", response.text.strip()).strip()
        data = json.loads(text)
    except Exception as e:
        print(f"  ⚠️ AI重建失败 {creator_email}: {e}")
        data = {}

    summary = str(data.get("summary", "")).strip() or "未能自动生成摘要，请人工查看原始邮件。"
    history = str(data.get("history", "")).strip() or summary
    stage = str(data.get("stage", "")).strip() or "其他"
    status = str(data.get("status", "")).strip() or "待回复"
    suggested_reply = str(data.get("suggested_reply", "")).strip()

    return [
        latest_date(messages),
        "",
        creator_email,
        summary,
        history,
        stage,
        status,
        "",
        suggested_reply,
        "",
        latest_receiver(messages),
    ]


def write_rebuilt_sheet(sheets, rows: list[list[str]]):
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A1:K1",
        valueInputOption="RAW",
        body={"values": [REPLIES_COLS]},
    ).execute()
    sheets.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A2:K",
        body={},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A2",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def main():
    if not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is required")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    grouped = fetch_inbound_messages(gmail)
    print(f"共找到 {len(grouped)} 个有效发件人邮箱")
    grouped = keep_unreplied_threads(gmail, grouped)
    print(f"其中未回复邮箱 {len(grouped)} 个")

    rows = []
    for index, (creator_email, messages) in enumerate(sorted(grouped.items()), start=1):
        print(f"[{index}/{len(grouped)}] 重建：{creator_email}（{len(messages)}封）")
        time.sleep(4)
        rows.append(rebuild_row(model, creator_email, messages))

    if not rows:
        raise SystemExit("没有抓到 2026-05-21 到现在仍未回复的新邮件，为避免清空表格，本次不写入。")

    rows.sort(key=lambda row: row[0], reverse=True)
    print(f"准备重写沟通管理：{len(rows)} 行")
    write_rebuilt_sheet(sheets, rows)

    print(json.dumps({"rebuilt_rows": len(rows)}, ensure_ascii=False))
    print("✅ 已按 2026-05-21 到现在仍未回复的新邮件重建沟通管理")


if __name__ == "__main__":
    main()
