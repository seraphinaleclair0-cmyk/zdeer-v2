"""
backfill_replies.py — 一次性脚本

默认只扫描今天的达人回复；也可以通过环境变量指定日期范围。

规则：
- 沟通管理里已有这个达人的行：更新 A/D/F/G/M/N，B/C 不动，E 只追加不覆盖
- 沟通管理里没有这个达人的行：从 Gmail 抓指定日期范围内的邮件，新建一行
- 跑完可以删掉这个文件，不影响主流程
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import anthropic
from googleapiclient.discovery import build

from config import (
    ANTHROPIC_API_KEY,
    EMAIL_ACCOUNTS,
    NEGOTIATION_CARDS,
    OUTBOX_SHEET,
    REPLIES_SHEET,
    SHEET_ID,
)
from google_auth import get_creds

STATUS_OPTIONS = ["待回复", "已回复", "已放弃", "无需回复"]


def parse_input_date(value: str, fallback: str) -> datetime:
    value = (value or "").strip() or fallback
    return datetime.strptime(value, "%Y-%m-%d")


def get_date_range() -> tuple[datetime, datetime]:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = parse_input_date(os.environ.get("BACKFILL_START_DATE", ""), today.strftime("%Y-%m-%d"))
    end = parse_input_date(os.environ.get("BACKFILL_END_DATE", ""), today.strftime("%Y-%m-%d"))
    if end < start:
        raise SystemExit("BACKFILL_END_DATE cannot be earlier than BACKFILL_START_DATE")
    return start, end


def build_gmail_date_query(start: datetime, end: datetime) -> str:
    # Gmail after/before dates are easiest to use as an inclusive local date range
    # by expanding to the previous and next day.
    after_date = (start - timedelta(days=1)).strftime("%Y/%m/%d")
    before_date = (end + timedelta(days=1)).strftime("%Y/%m/%d")
    return f"after:{after_date} before:{before_date}"


def get_sheet_data(sheets, sheet_name: str) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:Z",
    ).execute()
    return result.get("values", [])


def append_row(sheets, sheet_name: str, row: list):
    sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def update_cell(sheets, sheet_name: str, row: int, col: int, value: str):
    col_letter = chr(64 + col)
    sheets.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!{col_letter}{row}",
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def append_to_history(sheets, row_index: int, new_entry: str):
    """Append to E column without replacing existing manual history."""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!E{row_index}",
    ).execute()
    existing = ""
    values = result.get("values", [])
    if values and values[0]:
        existing = values[0][0].strip()
    if new_entry and new_entry in existing:
        return
    updated = f"{existing}\n{new_entry}" if existing else new_entry
    update_cell(sheets, REPLIES_SHEET, row_index, 5, updated)


def status_for_stage(stage: str) -> str:
    if stage == "已成交":
        return "已回复"
    return "待回复"


def get_sheet_id(sheets, sheet_name: str):
    spreadsheet = sheets.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == sheet_name:
            return props.get("sheetId")
    return None


def ensure_status_dropdown(sheets):
    sheet_id = get_sheet_id(sheets, REPLIES_SHEET)
    if sheet_id is None:
        print(f"  ⚠️ 找不到工作表：{REPLIES_SHEET}，无法设置G列状态下拉")
        return

    values = [{"userEnteredValue": value} for value in STATUS_OPTIONS]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={
            "requests": [
                {
                    "setDataValidation": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": 6,
                            "endColumnIndex": 7,
                        },
                        "rule": {
                            "condition": {
                                "type": "ONE_OF_LIST",
                                "values": values,
                            },
                            "inputMessage": "请选择状态",
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                }
            ]
        },
    ).execute()


def find_in_replies(replies_data: list, email: str) -> int:
    email = email.strip().lower()
    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email:
            return i
    return 0


def find_in_outbox(outbox_data: list, email: str) -> tuple[int, dict]:
    email = email.strip().lower()
    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) > 1 and row[1].strip().lower() == email:
            return i, {
                "name": row[0].strip() if len(row) > 0 else "",
                "email": row[1].strip(),
            }
    return 0, {}


def decode_body_data(data: str) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")


def walk_parts(payload: dict):
    for part in payload.get("parts", []):
        yield part
        yield from walk_parts(part)


def extract_body(msg: dict) -> str:
    payload = msg.get("payload", {})
    candidates = [payload, *walk_parts(payload)]

    for part in candidates:
        if part.get("mimeType") == "text/plain":
            body = decode_body_data(part.get("body", {}).get("data", ""))
            if body.strip():
                return body[:1500].strip()

    for part in candidates:
        if part.get("mimeType") == "text/html":
            body = decode_body_data(part.get("body", {}).get("data", ""))
            body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body)
            if body.strip():
                return body[:1500].strip()

    return ""


def parse_headers(msg: dict) -> dict:
    return {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


def extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    return match.group(0).lower() if match else ""


def extract_emails(value: str) -> set[str]:
    return {
        match.lower()
        for match in re.findall(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    }


def parse_email_date(date_header: str, fallback: str) -> str:
    try:
        return parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
    except Exception:
        return fallback


def load_negotiation_cards() -> str:
    if os.path.exists(NEGOTIATION_CARDS):
        with open(NEGOTIATION_CARDS, encoding="utf-8") as f:
            return f.read()
    return ""


def ai_process_reply(reply_body: str, history: str, cards: str) -> dict:
    time.sleep(2)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""你是一个有经验的 TikTok 红人营销 BD，正在代表品牌与达人沟通合作。
以下是达人发来的回复邮件，请完成三件事。

语言规则：
- summary 必须写中文，方便团队内部阅读。
- summary 必须写中文，格式固定为：[邮件重点1]，[邮件重点2]，不超过2句话，不加「达人：」前缀
- stage 必须写中文，只能从给定选项中选择。
- suggested_reply 必须写自然、专业的美式英语。
- suggested_reply 不要出现中文，不要中英混杂。
- 这个脚本只补历史记录，不生成回复邮件，suggested_reply 必须返回空字符串。
- 不要编造任何数据，筹码库里没有提到的数字一律不写。

筹码库使用规则（重要）：
- 筹码库里的所有条件都是真实存在、可以直接用的。
- 转化激励金额固定是：带货达 $5,000，额外奖励 $300，不得改成其他数字。

写邮件前必须先做以下分析，再动笔：
1. 对方现在最担心什么？从邮件和过往沟通里找，如果有顾虑，第一优先级是解决它。
2. 当前谈判处于什么阶段？是推进、守价、消除顾虑、还是确认合作？
3. 这封邮件的核心目标只有一个是什么？
4. 用最少的话达到这个目标，不要同时塞多个目的进一封邮件。

邮件结构原则：
1. "Hey [达人名字]," 开头，提取不到就用 "Hey there,"
2. 如果对方有顾虑，第一段必须先回应顾虑
3. 报价场景顺序：ad support/创意自由 → 报价 → 转化激励
4. 结尾用开放式问句，不施压，不提合同/样品/下一步
5. 全程用「I」不用「We」

绝对禁止：
- 使用「We」作为主语
- 使用夸张词汇：fantastic、amazing、incredible 等
- 编造任何未在筹码库或指令中出现的数字
- 开头超过1句话的客套
- 结尾跳步提到合同、样品、下一步流程

写作风格：
- 口语化但专业，像真人写的
- 简洁，每个句子都要有用
- 语气匹配情景
- 自然加入 1-2 个 emoji
- 落款统一用 "Best, Eloise"

风格示例一（消除顾虑场景）：
---
Hey Andres,

Totally understand the concern — the design does look similar to a vape, so that's a fair question.

To clarify: as long as the video doesn't mention anything vape-related and the product is used correctly, it's completely fine on TikTok. Zdeer is an electronic mouth spray — no nicotine, no inhalable substances.

Here's the product deck so Daniela can get a better feel for what it is:
https://docs.google.com/presentation/d/1nvNYj8gOcfQo_pW518gN-eoCt3YuRGMP7TvB5-e4p5Y/edit?slide=id.g3b2b8717fe0_1_213#slide=id.g3b2b8717fe0_1_213

We've also had other creators posting with it recently with no issues — here's one that's been doing well:
https://www.tiktok.com/@mcamila0301/video/7640311080094403854

Let me know if you have any other questions! 😊

Best,
Eloise
---

风格示例二（报价/守价场景）：
---
Hey Sylvia,

Talked to my team — we can do $500 for 1 video.

I know it's lower than your rate, so here's what else is on the table: I put ad spend behind every video to boost reach, and if the content hits $5,000 in sales, there's an extra $300 bonus for you — no cap on that.

Creative is fully yours, no approval process. Here's a recent video that did well for context:
https://www.tiktok.com/@mcamila0301/video/7640311080094403854

Would this work for you? 🙌

Best,
Eloise
---

邮件内容：
{reply_body}

过往沟通记录：
{history if history else "无"}

请严格按以下 JSON 格式返回，不要加任何其他内容：
{{
  "summary": "用1-2句中文概括回复的核心内容",
  "stage": "从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他",
  "suggested_reply": ""
}}

可用筹码库：
{cards}
"""
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        return {"summary": "AI解析失败，请手动查看", "stage": "其他", "suggested_reply": ""}


def ai_summarize_history_message(body: str) -> str:
    time.sleep(2)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""请用1句中文简洁总结以下邮件的核心内容，供内部沟通历史记录使用。
格式要求：直接写重点，不加任何前缀，不超过1句话。
不要写「达人：」「我方：」等角色前缀。

邮件内容：
{body[:1000]}
"""
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception:
        return "（摘要生成失败）"


def build_thread_history(
    gmail,
    thread_id: str,
    own_emails: set[str],
    target_email: str,
    current_message_id: str,
    current_summary: str,
    fallback_date: str,
) -> str:
    if not thread_id:
        return f"{fallback_date} 达人：{current_summary}"

    try:
        thread = gmail.users().threads().get(
            userId="me",
            id=thread_id,
            format="full",
        ).execute()
    except Exception as e:
        print(f"  ⚠️ 读取线程历史失败，只保留当前摘要：{e}")
        return f"{fallback_date} 达人：{current_summary}"

    entries = []
    seen_message_ids = set()
    target_email = target_email.lower()
    thread_msgs = sorted(
        thread.get("messages", []),
        key=lambda item: int(item.get("internalDate", "0")),
    )

    for thread_msg in thread_msgs:
        headers = parse_headers(thread_msg)
        message_id = headers.get("Message-ID", "")
        if message_id and message_id in seen_message_ids:
            continue
        if message_id:
            seen_message_ids.add(message_id)

        body = extract_body(thread_msg)
        if not body:
            continue

        from_email = extract_email(headers.get("From", ""))
        if from_email in own_emails:
            recipients = set()
            for header_name in ("To", "Cc", "Bcc", "Delivered-To"):
                recipients.update(extract_emails(headers.get(header_name, "")))
            if target_email not in recipients and message_id != current_message_id:
                continue
            speaker = "我方"
        elif from_email == target_email:
            speaker = "达人"
        else:
            continue
        date_str = parse_email_date(headers.get("Date", ""), fallback_date)
        if message_id and current_message_id and message_id == current_message_id:
            summary = current_summary
        else:
            summary = ai_summarize_history_message(body)
        entries.append(f"{date_str} {speaker}：{summary}")

    return "\n".join(entries) if entries else f"{fallback_date} 达人：{current_summary}"


def list_all_messages(gmail, query: str) -> list:
    messages = []
    page_token = None

    while True:
        result = gmail.users().messages().list(
            userId="me",
            q=query,
            maxResults=100,
            pageToken=page_token,
        ).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def latest_inbound_by_sender(gmail, messages: list, own_emails: set[str]) -> tuple[list, dict]:
    counts = {"skipped_own": 0, "skipped_empty": 0}
    latest_by_email = {}

    for msg_ref in messages:
        msg = gmail.users().messages().get(
            userId="me",
            id=msg_ref["id"],
            format="full",
        ).execute()
        label_ids = set(msg.get("labelIds", []))
        if "SENT" in label_ids or "DRAFT" in label_ids:
            continue

        headers = parse_headers(msg)
        from_email = extract_email(headers.get("From", ""))
        if not from_email or from_email in own_emails:
            counts["skipped_own"] += 1
            continue

        body = extract_body(msg)
        if not body:
            counts["skipped_empty"] += 1
            print(f"  ⏭️ 邮件正文为空，跳过：{from_email}")
            continue

        internal_date = int(msg.get("internalDate", "0"))
        current = latest_by_email.get(from_email)
        if not current or internal_date > current["internal_date"]:
            latest_by_email[from_email] = {
                "msg": msg,
                "headers": headers,
                "from_email": from_email,
                "body": body,
                "internal_date": internal_date,
            }

    return (
        sorted(
            latest_by_email.values(),
            key=lambda item: item["internal_date"],
            reverse=True,
        ),
        counts,
    )


def main():
    print(f"\n🔁 开始补全历史回复 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    start_date, end_date = get_date_range()
    date_query = build_gmail_date_query(start_date, end_date)
    print(f"  范围：{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
    print("  规则：已有达人更新 A/D/F/G/M/N、E追加；B/C不动；没有则新增")

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards = load_negotiation_cards()

    ensure_status_dropdown(sheets)

    own_emails = {acc["email"].lower() for acc in EMAIL_ACCOUNTS}
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data = get_sheet_data(sheets, OUTBOX_SHEET)
    today = datetime.now().strftime("%Y-%m-%d")
    seen_emails = set()

    added = 0
    repaired_existing = 0
    skipped_own = 0
    skipped_empty = 0

    for account in EMAIL_ACCOUNTS:
        query = f"in:inbox to:{account['email']} {date_query}"
        print(f"\n📥 扫描 {account['email']}...")

        try:
            messages = list_all_messages(gmail, query)
        except Exception as e:
            print(f"  ⚠️ 失败：{e}")
            continue

        print(f"  找到 {len(messages)} 封")

        latest_records, counts = latest_inbound_by_sender(gmail, messages, own_emails)
        skipped_own += counts["skipped_own"]
        skipped_empty += counts["skipped_empty"]
        print(f"  按达人邮箱去重后 {len(latest_records)} 封最新回复")

        for record in latest_records:
            msg = record["msg"]
            headers = record["headers"]
            from_email = record["from_email"]
            body = record["body"]
            if from_email in seen_emails:
                print(f"  ⏭️ 本次已处理过，跳过：{from_email}")
                continue

            replies_data = get_sheet_data(sheets, REPLIES_SHEET)
            existing_row = find_in_replies(replies_data, from_email)
            date_str = parse_email_date(headers.get("Date", ""), today)

            try:
                ai_result = ai_process_reply(body, "", cards)
            except Exception as e:
                print(f"  ⚠️ AI失败：{e}")
                ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

            summary_entry = f"{date_str} 达人：{ai_result['summary']}"

            if existing_row:
                repaired_existing += 1
                seen_emails.add(from_email)
                print(f"  🔧 更新已有记录：{from_email}")
                update_cell(sheets, REPLIES_SHEET, existing_row, 1, date_str)
                update_cell(sheets, REPLIES_SHEET, existing_row, 4, summary_entry)
                append_to_history(sheets, existing_row, summary_entry)
                update_cell(sheets, REPLIES_SHEET, existing_row, 6, ai_result["stage"])
                update_cell(sheets, REPLIES_SHEET, existing_row, 7, status_for_stage(ai_result["stage"]))
                update_cell(sheets, REPLIES_SHEET, existing_row, 13, headers.get("Message-ID", ""))
                update_cell(sheets, REPLIES_SHEET, existing_row, 14, headers.get("Subject", ""))
                time.sleep(2)
                continue

            print(f"  ✅ 新建：{from_email}")
            outbox_row_i, outbox_info = find_in_outbox(outbox_data, from_email)
            name = outbox_info.get("name", "")

            if outbox_row_i:
                update_cell(sheets, OUTBOX_SHEET, outbox_row_i, 6, "已回复")

            new_row = [
                date_str,
                name,
                from_email,
                summary_entry,
                summary_entry,
                ai_result["stage"],
                status_for_stage(ai_result["stage"]),
                "",
                "",
                "",
                account["email"],
                "",
                headers.get("Message-ID", ""),
                headers.get("Subject", ""),
            ]
            append_row(sheets, REPLIES_SHEET, new_row)
            seen_emails.add(from_email)
            added += 1
            replies_data = get_sheet_data(sheets, REPLIES_SHEET)
            time.sleep(2)

    print(f"""
🎉 补全完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  新增：{added} 行
  修复已有：{repaired_existing} 行
  自己邮箱或无发件人跳过：{skipped_own} 封
  正文为空跳过：{skipped_empty} 封
""")


if __name__ == "__main__":
    main()
