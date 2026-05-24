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


MAX_MESSAGES_PER_CREATOR = 12
MAX_BODY_CHARS = 1200


def extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    return match.group(0).lower() if match else ""


def extract_body(msg: dict) -> str:
    def decode_part(part: dict) -> str:
        data = part.get("body", {}).get("data", "")
        if not data:
            return ""
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    payload = msg.get("payload", {})
    parts = payload.get("parts", [])

    for part in parts:
        if part.get("mimeType") == "text/plain":
            body = decode_part(part)
            if body:
                return body[:MAX_BODY_CHARS].strip()

    for part in parts:
        for sub_part in part.get("parts", []):
            if sub_part.get("mimeType") == "text/plain":
                body = decode_part(sub_part)
                if body:
                    return body[:MAX_BODY_CHARS].strip()

    body = decode_part(payload)
    return body[:MAX_BODY_CHARS].strip()


def parse_message_datetime(date_header: str) -> datetime:
    try:
        return parsedate_to_datetime(date_header).replace(tzinfo=None)
    except Exception:
        return datetime.min


def fetch_thread_messages(gmail, creator_email: str) -> list[dict]:
    own_emails = [account["email"].lower() for account in EMAIL_ACCOUNTS]
    collected = {}

    for account in own_emails:
        queries = [
            f"from:{creator_email} to:{account}",
            f"from:{account} to:{creator_email}",
        ]
        for query in queries:
            try:
                result = gmail.users().messages().list(
                    userId="me",
                    q=query,
                    maxResults=MAX_MESSAGES_PER_CREATOR,
                ).execute()
            except Exception as e:
                print(f"  ⚠️ Gmail搜索失败 {creator_email} / {account}: {e}")
                continue

            for msg_ref in result.get("messages", []):
                msg_id = msg_ref["id"]
                if msg_id in collected:
                    continue

                try:
                    msg = gmail.users().messages().get(
                        userId="me",
                        id=msg_id,
                        format="full",
                    ).execute()
                except Exception as e:
                    print(f"  ⚠️ Gmail读取失败 {msg_id}: {e}")
                    continue

                headers = {
                    item["name"].lower(): item["value"]
                    for item in msg.get("payload", {}).get("headers", [])
                }
                from_email = extract_email(headers.get("from", ""))
                to_email = extract_email(headers.get("to", ""))
                date_header = headers.get("date", "")
                body = extract_body(msg)

                if not body:
                    continue

                if from_email == creator_email:
                    direction = "达人发来"
                elif to_email == creator_email:
                    direction = "我方发出"
                else:
                    direction = "相关邮件"

                collected[msg_id] = {
                    "date": parse_message_datetime(date_header),
                    "date_text": date_header,
                    "from": from_email,
                    "to": to_email,
                    "direction": direction,
                    "body": body,
                }

    messages = sorted(collected.values(), key=lambda item: item["date"])
    return messages[-MAX_MESSAGES_PER_CREATOR:]


def build_ai_prompt(creator_email: str, messages: list[dict]) -> str:
    blocks = []
    for index, msg in enumerate(messages, start=1):
        blocks.append(
            f"""[{index}]
时间：{msg['date_text']}
方向：{msg['direction']}
From：{msg['from']}
To：{msg['to']}
正文：
{msg['body']}
"""
        )

    return f"""你是 Zdeer TikTok 达人合作 BD 助手。
下面是同一个达人邮箱 {creator_email} 与我方品牌邮箱之间的原始邮件往来。

请重新生成 Google Sheet 内部用的中文记录。

要求：
- 只基于原始邮件内容，不要编造预算、物流、合同、付款、库存、样品状态。
- 如果是系统通知、退信、Google推广邮件等，不要当成达人回复。
- D列 summary：用1-2句中文总结最近一次有效达人回复；如果没有达人有效回复，就总结当前沟通状态。
- E列 history：按时间顺序写中文过往沟通时间线，保留金额、佣金、WhatsApp、合同、地址、产品链接等关键信息。
- 语言必须是中文，供内部团队阅读。
- 输出严格 JSON，不要加 Markdown。

JSON格式：
{{
  "summary": "写入D列的中文摘要",
  "history": "写入E列的中文过往沟通"
}}

原始邮件：
{chr(10).join(blocks)}
"""


def rebuild_notes(model, creator_email: str, messages: list[dict]) -> dict:
    prompt = build_ai_prompt(creator_email, messages)
    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r"```json|```", "", text).strip()
        data = json.loads(text)
        return {
            "summary": str(data.get("summary", "")).strip(),
            "history": str(data.get("history", "")).strip(),
        }
    except Exception as e:
        print(f"  ⚠️ Gemini重建失败 {creator_email}: {e}")
        return {"summary": "", "history": ""}


def main():
    if not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is required")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    rows = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A:K",
    ).execute().get("values", [])

    updates = []
    rebuilt = 0
    skipped = 0

    for row_index, row in enumerate(rows[1:], start=2):
        creator_email = row[2].strip().lower() if len(row) > 2 else ""
        if not creator_email:
            skipped += 1
            continue

        print(f"  行{row_index} 重新抓取邮件：{creator_email}")
        messages = fetch_thread_messages(gmail, creator_email)
        if not messages:
            print("    未找到原始邮件，跳过")
            skipped += 1
            continue

        time.sleep(4)
        notes = rebuild_notes(model, creator_email, messages)
        if not notes["summary"] and not notes["history"]:
            skipped += 1
            continue

        if notes["summary"]:
            updates.append({
                "range": f"{REPLIES_SHEET}!D{row_index}",
                "values": [[notes["summary"]]],
            })
        if notes["history"]:
            updates.append({
                "range": f"{REPLIES_SHEET}!E{row_index}",
                "values": [[notes["history"]]],
            })

        rebuilt += 1

        if len(updates) >= 20:
            print(f"  批量写入 {len(updates)} 个单元格...")
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            updates = []
            time.sleep(2)

    if updates:
        print(f"  最终写入 {len(updates)} 个单元格...")
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    print(json.dumps({"rebuilt_rows": rebuilt, "skipped_rows": skipped}, ensure_ascii=False))
    print("✅ 历史沟通内容重建完成")


if __name__ == "__main__":
    main()
