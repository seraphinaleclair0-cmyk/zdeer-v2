"""
backfill_replies.py — 一次性补全历史达人回复

默认扫描最近 2 天作为 Gmail 候选范围；真正是否处理，靠 O 列 last_processed_internalDate 判断。
这样可以避免跨天/时区漏抓，也避免重复调用 AI 浪费 token。

规则：
- 沟通管理里已有这个达人，且 G 列为「已放弃」：整行跳过，不更新任何列
- 沟通管理里已有这个达人：处理同一 Gmail thread 中 O 列时间之后的新邮件
- 沟通管理里没有这个达人：只有达人 inbox 回复触发时才新建；纯我方开发信不新建
- E 列追加完整线程中新消息摘要；B/C 不动
- D 列更新为最新一封有效邮件摘要
- F/G 只根据最新一封「达人邮件」更新；我方邮件不会把状态改成待回复
- M/N 保存最新有效邮件的 Message-ID / Subject
- O 列保存该达人最后处理到的 Gmail internalDate
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import anthropic
import config
from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    NEGOTIATION_CARDS,
    OUTBOX_SHEET,
    REPLIES_SHEET,
    SHEET_ID,
)
from google_auth import get_creds

STATUS_OPTIONS = ["待回复", "已回复", "已放弃", "无需回复"]

ANTHROPIC_API_KEY = getattr(config, "ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")


def claude_generate_text(prompt: str, max_tokens: int = 800, json_mode: bool = False) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is empty. Please add it to GitHub Secrets and config.py/env.")

    if json_mode:
        prompt = f"{prompt}\n\nReturn only valid JSON. Do not wrap it in markdown fences."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in message.content
        if getattr(block, "type", "") == "text" and getattr(block, "text", "")
    ).strip()



def parse_input_date(value: str, fallback: str) -> datetime:
    value = (value or "").strip() or fallback
    return datetime.strptime(value, "%Y-%m-%d")


def get_date_range() -> tuple[datetime, datetime]:
    """
    Gmail after/before 只能按日期过滤，不按小时过滤。
    为避免跨天/时区漏抓，默认扫描最近 2 天作为候选范围；
    真正是否处理，后面用 O 列 internalDate 精确判断。
    """
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_env = os.environ.get("BACKFILL_START_DATE", "").strip()
    end_env = os.environ.get("BACKFILL_END_DATE", "").strip()

    if not start_env and not end_env:
        return today - timedelta(days=2), today

    start = parse_input_date(start_env, today.strftime("%Y-%m-%d"))
    end = parse_input_date(end_env, today.strftime("%Y-%m-%d"))
    if end < start:
        raise SystemExit("BACKFILL_END_DATE cannot be earlier than BACKFILL_START_DATE")
    return start, end


def build_gmail_date_query(start: datetime, end: datetime) -> str:
    # Gmail after/before 是日期级别。这里前后各扩一天，避免边界漏抓。
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


def col_to_letter(col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def update_cell(sheets, sheet_name: str, row: int, col: int, value: str):
    col_letter = col_to_letter(col)
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

    # 主去重依赖 O 列 internalDate；这里保留一层兜底，避免异常重复写入。
    if new_entry and new_entry in existing:
        return

    updated = f"{existing}\n{new_entry}" if existing else new_entry
    update_cell(sheets, REPLIES_SHEET, row_index, 5, updated)


def status_for_stage(stage: str) -> str:
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


def get_existing_status(replies_data: list, row_index: int) -> str:
    row = replies_data[row_index - 1]
    return row[6].strip() if len(row) > 6 else ""


def get_last_processed_internal_date(replies_data: list, row_index: int) -> int:
    """O列：最后处理到的 Gmail internalDate。"""
    if not row_index:
        return 0
    row = replies_data[row_index - 1]
    if len(row) > 14:
        try:
            return int(str(row[14]).strip())
        except Exception:
            return 0
    return 0


def get_existing_replies_emails(replies_data: list) -> set[str]:
    emails = set()
    for row in replies_data[1:]:
        if len(row) > 2 and row[2].strip():
            emails.add(row[2].strip().lower())
    return emails


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
    time.sleep(1)
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
    try:
        text = claude_generate_text(prompt, max_tokens=1000, json_mode=True)
        text = re.sub(r"```json|```", "", text).strip()
        result = json.loads(text)
        return {
            "summary": str(result.get("summary", "")).strip() or "AI解析失败，请手动查看",
            "stage": str(result.get("stage", "其他")).strip() or "其他",
            "suggested_reply": "",
        }
    except Exception as e:
        print(f"  ⚠️ Claude解析失败：{e}")
        return {"summary": "AI解析失败，请手动查看", "stage": "其他", "suggested_reply": ""}


def ai_summarize_history_message(body: str) -> str:
    time.sleep(1)
    prompt = f"""请用1句中文简洁总结以下邮件的核心内容，供内部沟通历史记录使用。
格式要求：直接写重点，不加任何前缀，不超过1句话。
不要写「达人：」「我方：」等角色前缀。

邮件内容：
{body[:1000]}
"""
    try:
        return claude_generate_text(prompt, max_tokens=300, json_mode=False)
    except Exception as e:
        print(f"  ⚠️ Claude摘要失败：{e}")
        return "（摘要生成失败）"


def message_sent_to_target(headers: dict, target_email: str) -> bool:
    recipients = set()
    for header_name in ("To", "Cc", "Bcc"):
        recipients.update(extract_emails(headers.get(header_name, "")))
    return target_email.lower() in recipients


def collect_thread_messages(gmail, thread_id: str, own_emails: set[str], target_email: str) -> list[dict]:
    """
    从同一个 Gmail thread 中提取与 target_email 相关的有效消息。
    包括：
    - 达人发来的邮件
    - 我方发给该达人的邮件

    注意：这不会单独因为我方开发信新建沟通管理行；
    是否新建由触发源 allow_create 控制。
    """
    thread = gmail.users().threads().get(
        userId="me",
        id=thread_id,
        format="full",
    ).execute()

    items = []
    target_email = target_email.lower()
    fallback_date = datetime.now().strftime("%Y-%m-%d")

    for msg in thread.get("messages", []):
        headers = parse_headers(msg)
        from_email = extract_email(headers.get("From", ""))
        body = extract_body(msg)
        if not body:
            continue

        if from_email == target_email:
            speaker = "达人"
        elif from_email in own_emails and message_sent_to_target(headers, target_email):
            speaker = "我方"
        else:
            continue

        internal_date = int(msg.get("internalDate", "0"))
        items.append({
            "msg": msg,
            "headers": headers,
            "from_email": from_email,
            "speaker": speaker,
            "body": body,
            "internal_date": internal_date,
            "date_str": parse_email_date(headers.get("Date", ""), fallback_date),
            "message_id": headers.get("Message-ID", ""),
            "subject": headers.get("Subject", ""),
        })

    return sorted(items, key=lambda item: item["internal_date"])


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


def read_message(gmail, msg_id: str) -> dict:
    return gmail.users().messages().get(
        userId="me",
        id=msg_id,
        format="full",
    ).execute()


def get_existing_history(sheets, row_index: int) -> str:
    if not row_index:
        return ""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!E{row_index}",
    ).execute()
    values = result.get("values", [])
    if values and values[0]:
        return values[0][0].strip()
    return ""


def process_thread_for_target(
    gmail,
    sheets,
    cards: str,
    outbox_data: list,
    own_emails: set[str],
    target_email: str,
    thread_id: str,
    account_email: str,
    allow_create: bool,
) -> tuple[bool, str]:
    """
    处理某个达人在某个 Gmail thread 里的新消息。

    allow_create=True：达人 inbox 回复触发，允许新建沟通管理行。
    allow_create=False：我方 sent 触发，只能更新已有达人，不能新建。
    """
    target_email = target_email.lower()
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    existing_row = find_in_replies(replies_data, target_email)

    if existing_row and get_existing_status(replies_data, existing_row) == "已放弃":
        return False, f"⏭️ 已放弃达人，整行跳过：{target_email}"

    if not existing_row and not allow_create:
        return False, f"⏭️ 纯我方发信且达人未进入沟通管理，不新建：{target_email}"

    last_processed = get_last_processed_internal_date(replies_data, existing_row) if existing_row else 0

    try:
        thread_items = collect_thread_messages(gmail, thread_id, own_emails, target_email)
    except Exception as e:
        return False, f"⚠️ 读取 thread 失败：{target_email} / {e}"

    if not thread_items:
        return False, f"⏭️ thread 内没有可处理消息：{target_email}"

    new_items = [item for item in thread_items if item["internal_date"] > last_processed]
    if not new_items:
        return False, f"⏭️ 没有 O 列之后的新消息：{target_email}"

    latest_influencer_item = None
    for item in reversed(new_items):
        if item["speaker"] == "达人":
            latest_influencer_item = item
            break

    existing_history = get_existing_history(sheets, existing_row) if existing_row else ""
    ai_reply_result = None
    if latest_influencer_item:
        try:
            ai_reply_result = ai_process_reply(
                latest_influencer_item["body"],
                existing_history,
                cards,
            )
        except Exception as e:
            print(f"  ⚠️ AI处理达人最新邮件失败：{target_email} / {e}")
            ai_reply_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

    summary_by_internal_date = {}
    for item in new_items:
        if (
            latest_influencer_item
            and item["internal_date"] == latest_influencer_item["internal_date"]
            and item["message_id"] == latest_influencer_item["message_id"]
            and ai_reply_result
        ):
            summary = ai_reply_result["summary"]
        else:
            summary = ai_summarize_history_message(item["body"])

        summary_entry = f"{item['date_str']} {item['speaker']}：{summary}"
        summary_by_internal_date[item["internal_date"]] = {
            "summary": summary,
            "entry": summary_entry,
            "item": item,
        }

    latest_item = max(new_items, key=lambda item: item["internal_date"])
    latest_summary_obj = summary_by_internal_date[latest_item["internal_date"]]
    latest_entry = latest_summary_obj["entry"]
    max_internal_date = max(item["internal_date"] for item in new_items)

    if existing_row:
        # 已有达人：追加 E，更新 D/M/N/O；只有有新达人邮件时才更新 F/G。
        for item in new_items:
            append_to_history(
                sheets,
                existing_row,
                summary_by_internal_date[item["internal_date"]]["entry"],
            )

        update_cell(sheets, REPLIES_SHEET, existing_row, 1, latest_item["date_str"])
        update_cell(sheets, REPLIES_SHEET, existing_row, 4, latest_entry)

        if ai_reply_result and latest_influencer_item:
            update_cell(sheets, REPLIES_SHEET, existing_row, 6, ai_reply_result["stage"])
            update_cell(sheets, REPLIES_SHEET, existing_row, 7, status_for_stage(ai_reply_result["stage"]))

        update_cell(sheets, REPLIES_SHEET, existing_row, 13, latest_item["message_id"])
        update_cell(sheets, REPLIES_SHEET, existing_row, 14, latest_item["subject"])
        update_cell(sheets, REPLIES_SHEET, existing_row, 15, str(max_internal_date))
        return True, f"🔧 已更新：{target_email}，新增 {len(new_items)} 条历史"

    # 没有已有行时，必须由达人回复触发，且 thread 里确实有达人邮件，才新建。
    if not allow_create or not latest_influencer_item:
        return False, f"⏭️ 没有达人回复触发，不新建：{target_email}"

    outbox_row_i, outbox_info = find_in_outbox(outbox_data, target_email)
    name = outbox_info.get("name", "")

    if outbox_row_i:
        update_cell(sheets, OUTBOX_SHEET, outbox_row_i, 6, "已回复")

    history_entries = [
        summary_by_internal_date[item["internal_date"]]["entry"]
        for item in new_items
    ]
    history_text = "\n".join(history_entries)

    new_row = [
        latest_item["date_str"],                      # A 日期
        name,                                         # B 达人名
        target_email,                                 # C 邮箱
        latest_entry,                                 # D 最新摘要
        history_text,                                 # E 历史
        ai_reply_result["stage"],                     # F 阶段
        status_for_stage(ai_reply_result["stage"]),   # G 状态
        "",                                           # H
        "",                                           # I
        "",                                           # J
        account_email,                                # K 收件账号
        "",                                           # L
        latest_item["message_id"],                    # M Message-ID
        latest_item["subject"],                       # N Subject
        str(max_internal_date),                       # O last_processed_internalDate
    ]
    append_row(sheets, REPLIES_SHEET, new_row)
    return True, f"✅ 已新建：{target_email}，写入 {len(new_items)} 条历史"


def main():
    print(f"\n🔁 开始补全历史回复 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    start_date, end_date = get_date_range()
    date_query = build_gmail_date_query(start_date, end_date)
    print(f"  候选范围：{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}")
    print("  规则：G=已放弃整行跳过；O列控制增量处理；thread补全我方/达人历史")

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards = load_negotiation_cards()

    ensure_status_dropdown(sheets)

    own_emails = {acc["email"].lower() for acc in EMAIL_ACCOUNTS}
    outbox_data = get_sheet_data(sheets, OUTBOX_SHEET)

    processed_keys = set()
    added_or_updated = 0
    skipped_own = 0
    skipped_empty = 0

    # 1) inbox 中的达人回复作为新建/更新触发源
    for account in EMAIL_ACCOUNTS:
        query = f"in:inbox to:{account['email']} {date_query}"
        print(f"\n📥 扫描 inbox：{account['email']}...")

        try:
            messages = list_all_messages(gmail, query)
        except Exception as e:
            print(f"  ⚠️ inbox扫描失败：{e}")
            continue

        print(f"  找到 {len(messages)} 封候选邮件")

        for msg_ref in messages:
            try:
                msg = read_message(gmail, msg_ref["id"])
            except Exception as e:
                print(f"  ⚠️ 读取邮件失败：{msg_ref.get('id')} / {e}")
                continue

            label_ids = set(msg.get("labelIds", []))
            if "SENT" in label_ids or "DRAFT" in label_ids:
                continue

            headers = parse_headers(msg)
            from_email = extract_email(headers.get("From", ""))
            if not from_email or from_email in own_emails:
                skipped_own += 1
                continue

            body = extract_body(msg)
            if not body:
                skipped_empty += 1
                print(f"  ⏭️ 邮件正文为空，跳过：{from_email}")
                continue

            thread_id = msg.get("threadId", "")
            if not thread_id:
                continue

            key = (from_email, thread_id)
            if key in processed_keys:
                continue
            processed_keys.add(key)

            ok, message = process_thread_for_target(
                gmail=gmail,
                sheets=sheets,
                cards=cards,
                outbox_data=outbox_data,
                own_emails=own_emails,
                target_email=from_email,
                thread_id=thread_id,
                account_email=account["email"],
                allow_create=True,
            )
            print(f"  {message}")
            if ok:
                added_or_updated += 1
            time.sleep(1)

    # 2) sent 中的我方邮件只更新「已经在沟通管理表里」的达人，不新建纯开发信
    print(f"\n📤 扫描 sent：只补已有沟通管理达人的我方新邮件...")
    try:
        sent_messages = list_all_messages(gmail, f"in:sent {date_query}")
    except Exception as e:
        print(f"  ⚠️ sent扫描失败：{e}")
        sent_messages = []

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    existing_replies_emails = get_existing_replies_emails(replies_data)
    print(f"  找到 {len(sent_messages)} 封 sent 候选邮件；沟通管理已有 {len(existing_replies_emails)} 个邮箱")

    for msg_ref in sent_messages:
        try:
            msg = read_message(gmail, msg_ref["id"])
        except Exception as e:
            print(f"  ⚠️ 读取sent邮件失败：{msg_ref.get('id')} / {e}")
            continue

        headers = parse_headers(msg)
        recipients = set()
        for header_name in ("To", "Cc", "Bcc"):
            recipients.update(extract_emails(headers.get(header_name, "")))

        matched_targets = recipients & existing_replies_emails
        if not matched_targets:
            continue

        thread_id = msg.get("threadId", "")
        if not thread_id:
            continue

        from_email = extract_email(headers.get("From", ""))
        account_email = from_email if from_email in own_emails else ""

        for target_email in matched_targets:
            key = (target_email, thread_id)
            if key in processed_keys:
                continue
            processed_keys.add(key)

            ok, message = process_thread_for_target(
                gmail=gmail,
                sheets=sheets,
                cards=cards,
                outbox_data=outbox_data,
                own_emails=own_emails,
                target_email=target_email,
                thread_id=thread_id,
                account_email=account_email,
                allow_create=False,
            )
            print(f"  {message}")
            if ok:
                added_or_updated += 1
            time.sleep(1)

    print(f"""
🎉 补全完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  新建或更新：{added_or_updated} 个 thread/达人
  自己邮箱或无发件人跳过：{skipped_own} 封
  正文为空跳过：{skipped_empty} 封
""")


if __name__ == "__main__":
    main()
