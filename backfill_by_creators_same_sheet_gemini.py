"""
backfill_by_creators_same_sheet_gemini.py — 手动补充达人后自动回填邮件信息

用法：
- 在「沟通管理」Sheet 手动填写 B 列达人名字、C 列邮箱
- 运行：python backfill_by_creators_same_sheet_gemini.py

脚本会补：
A 日期
D 回复摘要
E 过往沟通
F 当前阶段
G 状态
K 收件邮箱
L 备注
M Message-ID
N Subject
O 最后处理邮件时间

说明：
- B/C 不会被脚本覆盖
- G 列为「已放弃」或「无需回复」的行会跳过
- O 列记录 Gmail internalDate，用于下次增量处理
- 默认按邮箱搜索全部相关邮件；可用 BACKFILL_START_DATE / BACKFILL_END_DATE 限定候选日期
"""

from datetime import datetime, timedelta
import os
import time
from typing import Optional

from googleapiclient.discovery import build

from config import EMAIL_ACCOUNTS, REPLIES_SHEET, SHEET_ID
from google_auth import get_creds

from backfill_replies import (
    ai_process_reply,
    ai_summarize_history_message,
    append_to_history,
    build_gmail_date_query,
    collect_thread_messages,
    ensure_status_dropdown,
    extract_email,
    extract_emails,
    get_existing_history,
    get_last_processed_internal_date,
    get_sheet_data,
    list_all_messages,
    load_negotiation_cards,
    parse_headers,
    read_message,
    status_for_stage,
    update_cell,
)

SKIP_STATUSES = {"已放弃", "无需回复"}


def parse_date(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y-%m-%d")


def get_optional_date_query() -> str:
    start_env = os.environ.get("BACKFILL_START_DATE", "").strip()
    end_env = os.environ.get("BACKFILL_END_DATE", "").strip()
    if not start_env and not end_env:
        return ""

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = parse_date(start_env) if start_env else today - timedelta(days=30)
    end = parse_date(end_env) if end_env else today
    if end < start:
        raise SystemExit("BACKFILL_END_DATE cannot be earlier than BACKFILL_START_DATE")
    return build_gmail_date_query(start, end)


def row_value(row: list, index: int) -> str:
    return row[index].strip() if len(row) > index and row[index] else ""


def gmail_query_for_creator(email: str, date_query: str) -> str:
    # Gmail 花括号表示 OR：匹配达人发来的邮件或我方发给该达人的邮件。
    parts = [f"{{from:{email} to:{email}}}"]
    if date_query:
        parts.append(date_query)
    return " ".join(parts)


def collect_thread_ids_for_creator(gmail, target_email: str, date_query: str) -> set[str]:
    messages = list_all_messages(gmail, gmail_query_for_creator(target_email, date_query))
    return {msg["threadId"] for msg in messages if msg.get("threadId")}


def account_email_for_thread_items(thread_items: list[dict], own_emails: set[str]) -> str:
    for item in reversed(thread_items):
        if item["speaker"] != "达人":
            continue
        recipients = set()
        headers = item["headers"]
        for header_name in ("To", "Cc", "Bcc"):
            recipients.update(extract_emails(headers.get(header_name, "")))
        matched = recipients & own_emails
        if matched:
            return sorted(matched)[0]
    for item in reversed(thread_items):
        from_email = extract_email(item["headers"].get("From", ""))
        if from_email in own_emails:
            return from_email
    return ""


def summarize_new_items(new_items: list[dict], latest_influencer_item: Optional[dict], existing_history: str, cards: str):
    ai_reply_result = None
    if latest_influencer_item:
        ai_reply_result = ai_process_reply(
            latest_influencer_item["body"],
            existing_history,
            cards,
        )

    summary_by_internal_date = {}
    for item in new_items:
        if (
            latest_influencer_item
            and ai_reply_result
            and item["internal_date"] == latest_influencer_item["internal_date"]
            and item["message_id"] == latest_influencer_item["message_id"]
        ):
            summary = ai_reply_result["summary"]
        else:
            summary = ai_summarize_history_message(item["body"])

        summary_by_internal_date[item["internal_date"]] = {
            "summary": summary,
            "entry": f"{item['date_str']} {item['speaker']}：{summary}",
            "item": item,
        }

    return ai_reply_result, summary_by_internal_date


def process_sheet_row(gmail, sheets, cards: str, row_index: int, row: list, own_emails: set[str], date_query: str) -> tuple[bool, str]:
    name = row_value(row, 1)
    target_email = row_value(row, 2).lower()
    status = row_value(row, 6)

    if not name and not target_email:
        return False, "空行"
    if not target_email:
        update_cell(sheets, REPLIES_SHEET, row_index, 12, "缺少邮箱，无法搜索邮件")
        return False, f"⚠️ 行{row_index} 缺少邮箱"
    if status in SKIP_STATUSES:
        return False, f"⏭️ 行{row_index} 状态为{status}，跳过：{target_email}"

    last_processed = get_last_processed_internal_date(get_sheet_data(sheets, REPLIES_SHEET), row_index)

    try:
        thread_ids = collect_thread_ids_for_creator(gmail, target_email, date_query)
    except Exception as e:
        update_cell(sheets, REPLIES_SHEET, row_index, 12, f"搜索邮件失败：{e}")
        return False, f"⚠️ 行{row_index} 搜索失败：{target_email} / {e}"

    if not thread_ids:
        update_cell(sheets, REPLIES_SHEET, row_index, 12, "未找到相关邮件")
        return False, f"⏭️ 行{row_index} 未找到相关邮件：{target_email}"

    all_items = []
    for thread_id in sorted(thread_ids):
        try:
            all_items.extend(collect_thread_messages(gmail, thread_id, own_emails, target_email))
        except Exception as e:
            print(f"  ⚠️ 行{row_index} 读取 thread 失败：{thread_id} / {e}")
        time.sleep(0.5)

    all_items = sorted(
        {f"{item['internal_date']}|{item['message_id']}": item for item in all_items}.values(),
        key=lambda item: item["internal_date"],
    )
    new_items = [item for item in all_items if item["internal_date"] > last_processed]

    if not new_items:
        update_cell(sheets, REPLIES_SHEET, row_index, 12, "没有 O 列之后的新邮件")
        return False, f"⏭️ 行{row_index} 无新增邮件：{target_email}"

    latest_influencer_item = None
    for item in reversed(new_items):
        if item["speaker"] == "达人":
            latest_influencer_item = item
            break

    existing_history = get_existing_history(sheets, row_index)
    ai_reply_result, summary_by_internal_date = summarize_new_items(
        new_items,
        latest_influencer_item,
        existing_history,
        cards,
    )

    for item in new_items:
        append_to_history(
            sheets,
            row_index,
            summary_by_internal_date[item["internal_date"]]["entry"],
        )

    latest_item = max(new_items, key=lambda item: item["internal_date"])
    latest_entry = summary_by_internal_date[latest_item["internal_date"]]["entry"]
    account_email = account_email_for_thread_items(new_items, own_emails)
    max_internal_date = max(item["internal_date"] for item in new_items)

    update_cell(sheets, REPLIES_SHEET, row_index, 1, latest_item["date_str"])
    update_cell(sheets, REPLIES_SHEET, row_index, 4, latest_entry)
    if ai_reply_result and latest_influencer_item:
        update_cell(sheets, REPLIES_SHEET, row_index, 6, ai_reply_result["stage"])
        update_cell(sheets, REPLIES_SHEET, row_index, 7, status_for_stage(ai_reply_result["stage"]))
    update_cell(sheets, REPLIES_SHEET, row_index, 11, account_email)
    update_cell(sheets, REPLIES_SHEET, row_index, 12, f"手动补充自动填充：新增 {len(new_items)} 封邮件")
    update_cell(sheets, REPLIES_SHEET, row_index, 13, latest_item["message_id"])
    update_cell(sheets, REPLIES_SHEET, row_index, 14, latest_item["subject"])
    update_cell(sheets, REPLIES_SHEET, row_index, 15, str(max_internal_date))

    return True, f"✅ 行{row_index} 已更新：{target_email}，新增 {len(new_items)} 封邮件"


def main():
    print(f"\n🔁 开始手动补充达人邮件回填 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    date_query = get_optional_date_query()
    if date_query:
        print(f"  候选日期过滤：{date_query}")
    else:
        print("  候选日期过滤：不限日期，按 C 列邮箱搜索全部相关邮件")

    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards = load_negotiation_cards()
    own_emails = {acc["email"].lower() for acc in EMAIL_ACCOUNTS}

    ensure_status_dropdown(sheets)
    data = get_sheet_data(sheets, REPLIES_SHEET)

    updated = 0
    skipped = 0

    for row_index, row in enumerate(data[1:], start=2):
        ok, message = process_sheet_row(
            gmail=gmail,
            sheets=sheets,
            cards=cards,
            row_index=row_index,
            row=row,
            own_emails=own_emails,
            date_query=date_query,
        )
        if message != "空行":
            print(f"  {message}")
        if ok:
            updated += 1
        else:
            skipped += 1
        time.sleep(1)

    print(f"""
🎉 手动补充回填完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  更新：{updated} 行
  跳过/无新增：{skipped} 行
""")


if __name__ == "__main__":
    main()
