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
- 可设置 BACKFILL_FORCE_REBUILD=1 强制重建 E 列历史
"""

from datetime import datetime, timedelta
import os
import re
import time
from typing import Optional

from google import genai
from google.genai import types
from googleapiclient.discovery import build

import config
from config import EMAIL_ACCOUNTS, REPLIES_SHEET, SHEET_ID
from google_auth import get_creds

from backfill_replies import (
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
STAGE_OPTIONS = {"初次感兴趣", "价格谈判中", "犹豫不决", "已拒绝可挽回", "已成交", "其他"}

GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def gemini_generate_text(prompt: str, max_tokens: int = 500) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is empty. Please add it to GitHub Secrets and config.py/env.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=max_tokens,
        ),
    )
    return (response.text or "").strip()


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


def ai_summarize_message(body: str, speaker: str) -> str:
    prompt = f"""请把下面这封邮件总结成一句中文语义摘要，用于达人沟通历史。

要求：
- 只输出一句中文，不要加前缀
- 不要复制原文长句，不要截取正文
- 说明邮件的真实意思、诉求或进展
- 不超过45个中文字符
- 如果信息很少，也要概括成内部能看懂的一句话

邮件角色：{speaker}
邮件内容：
{body[:2500]}
"""
    try:
        text = gemini_generate_text(prompt, max_tokens=180)
        return re.sub(r"\s+", " ", text).strip() or "摘要生成失败，请手动查看"
    except Exception as e:
        print(f"  ⚠️ Gemini摘要失败：{e}")
        return "摘要生成失败，请手动查看"


def parse_summary_stage(text: str) -> tuple[str, str]:
    summary = ""
    stage = "其他"

    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif line.upper().startswith("STAGE:"):
            stage = line.split(":", 1)[1].strip()

    if stage not in STAGE_OPTIONS:
        stage = "其他"
    return summary or "摘要生成失败，请手动查看", stage


def ai_analyze_latest_influencer_message(body: str, history: str, cards: str) -> dict:
    prompt = f"""你是一个有经验的 TikTok 红人营销 BD，正在代表品牌与达人沟通合作。
请分析下面这封最新达人邮件，只输出两行：

SUMMARY: 一句中文摘要，不超过45个中文字符
STAGE: 从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他

规则：
- 不要输出 JSON
- 不要加代码块
- SUMMARY 必须是语义摘要，不要截取原文
- STAGE 必须完全等于给定选项之一
- 不要编造邮件中没有的信息

最新达人邮件：
{body[:2500]}

过往沟通：
{history if history else "无"}

可用筹码库：
{cards}
"""
    try:
        text = gemini_generate_text(prompt, max_tokens=260)
        summary, stage = parse_summary_stage(text)
        return {"summary": summary, "stage": stage}
    except Exception as e:
        print(f"  ⚠️ Gemini分析最新达人邮件失败：{e}")
        return {"summary": "摘要生成失败，请手动查看", "stage": "其他"}


def summarize_new_items(new_items: list[dict], latest_influencer_item: Optional[dict], existing_history: str, cards: str):
    ai_reply_result = None
    if latest_influencer_item:
        ai_reply_result = ai_analyze_latest_influencer_message(
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
            summary = ai_summarize_message(item["body"], item["speaker"])

        summary_by_internal_date[item["internal_date"]] = {
            "summary": summary,
            "entry": f"{item['date_str']} {item['speaker']}：{summary}",
            "item": item,
        }

    return ai_reply_result, summary_by_internal_date


def process_sheet_row(gmail, sheets, cards: str, row_index: int, row: list, own_emails: set[str], date_query: str, force_rebuild: bool) -> tuple[bool, str]:
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

    last_processed = 0 if force_rebuild else get_last_processed_internal_date(get_sheet_data(sheets, REPLIES_SHEET), row_index)

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
    new_items = all_items if force_rebuild else [item for item in all_items if item["internal_date"] > last_processed]

    if not new_items:
        update_cell(sheets, REPLIES_SHEET, row_index, 12, "没有 O 列之后的新邮件")
        return False, f"⏭️ 行{row_index} 无新增邮件：{target_email}"

    latest_influencer_item = None
    for item in reversed(new_items):
        if item["speaker"] == "达人":
            latest_influencer_item = item
            break

    existing_history = get_existing_history(sheets, row_index)
    if force_rebuild:
        update_cell(sheets, REPLIES_SHEET, row_index, 5, "")
        existing_history = ""

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
    if ai_reply_result and latest_influencer_item:
        latest_influencer_entry = summary_by_internal_date[latest_influencer_item["internal_date"]]["entry"]
        update_cell(sheets, REPLIES_SHEET, row_index, 4, latest_influencer_entry)
        update_cell(sheets, REPLIES_SHEET, row_index, 6, ai_reply_result["stage"])
        update_cell(sheets, REPLIES_SHEET, row_index, 7, status_for_stage(ai_reply_result["stage"]))
    update_cell(sheets, REPLIES_SHEET, row_index, 11, account_email)
    note_prefix = "强制重建" if force_rebuild else "手动补充自动填充"
    update_cell(sheets, REPLIES_SHEET, row_index, 12, f"{note_prefix}：处理 {len(new_items)} 封邮件")
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
    force_rebuild = os.environ.get("BACKFILL_FORCE_REBUILD", "").strip() == "1"
    print(f"  强制重建 E 列历史：{'是' if force_rebuild else '否'}")

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
            force_rebuild=force_rebuild,
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
