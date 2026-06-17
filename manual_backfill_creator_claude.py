"""
manual_backfill_creator_claude.py — 一次性手动补全沟通管理历史工具

使用场景：
你在「沟通管理」表里手动新增一行，只填：
- B列：达人名字（可填）
- C列：达人邮箱（必填）

运行本脚本后，它会按达人邮箱去 Gmail 搜索历史沟通，并补齐：
- A 日期
- D 回复摘要（最新一封达人邮件摘要）
- E 过往沟通（整段历史的阶段性总结，不做逐封流水账）
- F 当前阶段
- G 状态
- K 收件邮箱
- L 备注
- M Message-ID
- N Subject
- O 最后处理邮件时间 internalDate

特点：
- 使用 Claude Haiku 4.5，不使用 Gemini
- 只有达人有过回复才补全；只有我方开发信不会补进沟通管理
- G列=已放弃 默认跳过
- 默认只处理 C列有邮箱，并且 D/E/M/O 不完整的行
- 如果要强制重建已有行：BACKFILL_FORCE_REBUILD=1 python manual_backfill_creator_claude.py
- 如果只处理某个邮箱：TARGET_EMAIL=xxx@example.com python manual_backfill_creator_claude.py

依赖：
pip install anthropic google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

需要 GitHub Secret / 环境变量：
ANTHROPIC_API_KEY
GOOGLE_TOKEN_JSON
"""

import base64
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import config
import anthropic
from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    OUTBOX_SHEET,
    REPLIES_SHEET,
    SHEET_ID,
)
from google_auth import get_creds

STATUS_OPTIONS = ["待回复", "已回复", "已放弃", "无需回复"]

ANTHROPIC_API_KEY = getattr(config, "ANTHROPIC_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL", "").strip().lower()
FORCE_REBUILD = os.environ.get("BACKFILL_FORCE_REBUILD", "").strip() == "1"
INCLUDE_ABANDONED = os.environ.get("BACKFILL_INCLUDE_ABANDONED", "").strip() == "1"


def claude_generate_text(prompt: str, max_tokens: int = 900) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is empty. 请在 GitHub Secrets 或环境变量里配置 ANTHROPIC_API_KEY。")

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


def get_sheet_data(sheets, sheet_name: str) -> list:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{sheet_name}!A:O",
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


def batch_update_row_cells(sheets, sheet_name: str, row: int, updates: dict[int, str]):
    data = []
    for col, value in updates.items():
        col_letter = chr(64 + col)
        data.append({
            "range": f"{sheet_name}!{col_letter}{row}",
            "values": [[value]],
        })
    if not data:
        return
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


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
                            "condition": {"type": "ONE_OF_LIST", "values": values},
                            "inputMessage": "请选择状态",
                            "strict": True,
                            "showCustomUi": True,
                        },
                    }
                }
            ]
        },
    ).execute()


def decode_body_data(data: str) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")


def walk_parts(payload: dict):
    for part in payload.get("parts", []):
        yield part
        yield from walk_parts(part)


def strip_html(html: str) -> str:
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</p>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return html


def extract_body(msg: dict) -> str:
    payload = msg.get("payload", {})
    candidates = [payload, *walk_parts(payload)]

    for part in candidates:
        if part.get("mimeType") == "text/plain":
            body = decode_body_data(part.get("body", {}).get("data", ""))
            if body.strip():
                return body.strip()

    for part in candidates:
        if part.get("mimeType") == "text/html":
            body = decode_body_data(part.get("body", {}).get("data", ""))
            body = strip_html(body)
            if body.strip():
                return body.strip()

    return ""


def clean_email_body(body: str, limit: int = 1800) -> str:
    if not body:
        return ""

    text = body.replace("\r", "\n")
    # 去掉常见引用历史，避免 Claude 被老邮件干扰
    cut_patterns = [
        r"\nOn .+?wrote:\n",
        r"\n在 .+?写道：\n",
        r"\n-----Original Message-----\n",
        r"\nFrom:\s.*\nSent:\s.*\nTo:\s.*\nSubject:\s.*",
        r"\n发件人[:：].*\n发送时间[:：].*\n收件人[:：].*",
    ]
    for pat in cut_patterns:
        m = re.search(pat, text, flags=re.I | re.S)
        if m:
            text = text[:m.start()]

    # 去掉无意义 footer / 退订
    remove_lines = []
    for line in text.split("\n"):
        raw = line.strip()
        low = raw.lower()
        if not raw:
            remove_lines.append("")
            continue
        if any(key in low for key in [
            "unsubscribe", "manage preferences", "view in browser", "sent from my iphone",
            "confidentiality notice", "this email and any attachments",
        ]):
            continue
        if raw.startswith(">"):
            continue
        remove_lines.append(raw)

    text = "\n".join(remove_lines)
    text = re.sub(r"https?://\S+", "[link]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:limit]


def parse_headers(msg: dict) -> dict:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    return match.group(0).lower() if match else ""


def extract_emails(value: str) -> set[str]:
    return {m.lower() for m in re.findall(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")}


def parse_email_date(date_header: str, fallback: str = "") -> str:
    try:
        return parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
    except Exception:
        return fallback or datetime.now().strftime("%Y-%m-%d")


def status_for_stage(stage: str) -> str:
    if stage == "已成交":
        return "已回复"
    if stage in {"已拒绝可挽回", "其他"}:
        return "待回复"
    return "待回复"


def fallback_stage(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in ["sounds good", "works for me", "let's do", "lets do", "agree", "accepted", "i'm in", "im in"]):
        return "已成交"
    if any(k in low for k in ["rate", "price", "fee", "budget", "$", "usd", "charge", "报价", "价格", "预算"]):
        return "价格谈判中"
    if any(k in low for k in ["interested", "tell me more", "details", "信息", "资料", "感兴趣"]):
        return "初次感兴趣"
    if any(k in low for k in ["not interested", "pass", "decline", "不考虑", "拒绝"]):
        return "已拒绝可挽回"
    return "其他"


def extract_labeled_value(text: str, label: str) -> str:
    m = re.search(rf"{label}\s*[:：]\s*(.+)", text or "", flags=re.I)
    if not m:
        return ""
    value = m.group(1).strip()
    value = re.split(r"\s+(?:LATEST_SUMMARY|HISTORY|STAGE)\s*[:：]", value, flags=re.I)[0].strip()
    return value.strip(' "“”')


def extract_history_block(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"HISTORY\s*[:：]\s*([\s\S]*?)(?:\n\s*LATEST_SUMMARY\s*[:：]|\n\s*STAGE\s*[:：]|$)", text, flags=re.I)
    if not m:
        return ""
    history = m.group(1).strip()
    history = re.sub(r"^[-•]\s*", "", history, flags=re.M)
    history = re.sub(r"\n{3,}", "\n", history).strip()
    return history


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


def build_gmail_for_account(account: dict, account_index: int, default_creds):
    token_env_var = f"GOOGLE_TOKEN_JSON_{account_index}"
    if os.environ.get(token_env_var, "").strip():
        creds = get_creds(token_env_var=token_env_var, allow_service_account=False)
        token_source = token_env_var
    else:
        creds = default_creds
        token_source = "GOOGLE_TOKEN_JSON"

    gmail = build("gmail", "v1", credentials=creds)
    configured_email = account["email"].lower()
    try:
        profile_email = gmail.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
    except Exception as e:
        print(f"  ⚠️ 无法读取 Gmail 登录账号：{configured_email} / {e}")
        profile_email = ""

    if profile_email and profile_email != configured_email:
        print(
            f"  ⚠️ {configured_email} 使用的是 {token_source}，"
            f"Gmail API 实际登录账号为 {profile_email}；如果它不是别名/转发邮箱，需要配置 {token_env_var}"
        )
    else:
        print(f"  Gmail token：{configured_email} <- {token_source}")

    return gmail


def build_gmail_accounts(default_creds):
    gmail_accounts = []
    for account_index, account in enumerate(EMAIL_ACCOUNTS, start=1):
        token_env_var = f"GOOGLE_TOKEN_JSON_{account_index}"
        if not os.environ.get(token_env_var, "").strip() and default_creds is None:
            print(f"  ⚠️ 跳过 {account['email']}：缺少 {token_env_var}，且 GOOGLE_TOKEN_JSON 不可用")
            continue
        try:
            gmail_accounts.append((account, build_gmail_for_account(account, account_index, default_creds)))
        except Exception as e:
            print(f"  ⚠️ 跳过 {account['email']}：{token_env_var} 不可用 / {e}")
    return gmail_accounts


def message_sent_to_target(headers: dict, target_email: str) -> bool:
    recipients = set()
    for header_name in ("To", "Cc", "Bcc", "Delivered-To"):
        recipients.update(extract_emails(headers.get(header_name, "")))
    return target_email.lower() in recipients


def collect_thread_messages(gmail, thread_id: str, own_emails: set[str], target_email: str) -> list[dict]:
    thread = gmail.users().threads().get(userId="me", id=thread_id, format="full").execute()
    items = []
    target_email = target_email.lower()

    for msg in thread.get("messages", []):
        headers = parse_headers(msg)
        from_email = extract_email(headers.get("From", ""))
        body = clean_email_body(extract_body(msg), limit=1800)
        if not body:
            continue

        if from_email == target_email:
            speaker = "达人"
        elif from_email in own_emails and message_sent_to_target(headers, target_email):
            speaker = "我方"
        else:
            continue

        items.append({
            "msg": msg,
            "headers": headers,
            "from_email": from_email,
            "speaker": speaker,
            "body": body,
            "internal_date": int(msg.get("internalDate", "0")),
            "date_str": parse_email_date(headers.get("Date", "")),
            "message_id": headers.get("Message-ID", ""),
            "subject": headers.get("Subject", ""),
            "thread_id": thread_id,
        })

    return sorted(items, key=lambda x: x["internal_date"])


def collect_all_items_for_creator(gmail, own_emails: set[str], target_email: str) -> list[dict]:
    query = f"from:{target_email} OR to:{target_email}"
    refs = list_all_messages(gmail, query)
    thread_ids = []
    seen_threads = set()
    for ref in refs:
        tid = ref.get("threadId")
        if tid and tid not in seen_threads:
            seen_threads.add(tid)
            thread_ids.append(tid)

    all_items = []
    for tid in thread_ids:
        try:
            all_items.extend(collect_thread_messages(gmail, tid, own_emails, target_email))
        except Exception as e:
            print(f"  ⚠️ 读取 thread 失败：{tid} / {e}")

    dedup = {}
    for item in all_items:
        key = item.get("message_id") or f"{item['internal_date']}-{item['speaker']}-{item['subject']}"
        dedup[key] = item

    return sorted(dedup.values(), key=lambda x: x["internal_date"])


def collect_all_items_for_creator_across_accounts(gmail_accounts: list, own_emails: set[str], target_email: str) -> list[dict]:
    all_items = []
    for account, gmail in gmail_accounts:
        try:
            account_items = collect_all_items_for_creator(gmail, own_emails, target_email)
            print(f"  {account['email']} 找到 {len(account_items)} 条相关邮件")
            all_items.extend(account_items)
        except Exception as e:
            print(f"  ⚠️ 扫描 {account['email']} 失败：{e}")

    dedup = {}
    for item in all_items:
        key = item.get("message_id") or f"{item['internal_date']}-{item['speaker']}-{item['subject']}"
        dedup[key] = item
    return sorted(dedup.values(), key=lambda x: x["internal_date"])


def build_conversation_digest_input(items: list[dict], max_messages: int = 25) -> str:
    # 历史太长时，保留最近 max_messages 封，通常后续续聊更需要近况。
    selected = items[-max_messages:]
    lines = []
    for item in selected:
        body = item["body"][:900]
        lines.append(f"日期：{item['date_str']}\n角色：{item['speaker']}\n内容：{body}")
    return "\n\n---\n\n".join(lines)


def analyze_conversation_with_claude(items: list[dict]) -> dict:
    influencer_items = [x for x in items if x["speaker"] == "达人"]
    latest_influencer = influencer_items[-1] if influencer_items else None
    latest_valid = items[-1]
    convo = build_conversation_digest_input(items)

    prompt = f"""你是TikTok达人合作CRM整理助手。请根据以下邮件往来，整理成适合后续AI继续沟通的中文记录。

输出格式必须包含以下三个标签：
HISTORY: 过往沟通总结，3-6行，每行一句中文，按时间/谈判进展概括，不要逐封流水账。重点写：报价、合作条件、达人态度、我方让步、当前卡点。
LATEST_SUMMARY: 最新一封达人邮件的核心意思，不超过45个中文字。如果没有达人邮件，写“无达人回复”。
STAGE: 从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他

不要编造邮件里没有的金额、时间、条数、承诺。
不要输出英文解释。

邮件往来：
{convo}
"""
    try:
        text = claude_generate_text(prompt, max_tokens=900)
        text = re.sub(r"```.*?```", "", text, flags=re.S).strip()
        history = extract_history_block(text)
        latest_summary = extract_labeled_value(text, "LATEST_SUMMARY")
        stage = extract_labeled_value(text, "STAGE")
    except Exception as e:
        print(f"  ⚠️ Claude 总结失败：{e}")
        history = "需手动查看：AI未成功生成过往沟通总结。"
        latest_summary = "需手动查看：AI未成功生成最新回复摘要。"
        stage = fallback_stage(latest_influencer["body"] if latest_influencer else "")

    if not latest_summary and latest_influencer:
        latest_summary = "需手动查看：AI未成功生成最新回复摘要。"
    if not history:
        history = "需手动查看：AI未成功生成过往沟通总结。"
    if not stage:
        stage = fallback_stage(latest_influencer["body"] if latest_influencer else "")

    return {
        "history": history,
        "latest_summary": latest_summary,
        "stage": stage if stage in ["初次感兴趣", "价格谈判中", "犹豫不决", "已拒绝可挽回", "已成交", "其他"] else fallback_stage(latest_influencer["body"] if latest_influencer else ""),
        "latest_influencer": latest_influencer,
        "latest_valid": latest_valid,
    }


def row_value(row: list, index: int) -> str:
    return row[index].strip() if len(row) > index and row[index] else ""


def find_in_outbox(outbox_data: list, email: str) -> tuple[int, dict]:
    email = email.strip().lower()
    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) > 1 and row[1].strip().lower() == email:
            return i, {
                "name": row_value(row, 0),
                "email": row_value(row, 1),
                "status": row_value(row, 5),
            }
    return 0, {}


def mark_outbox_replied(sheets, outbox_data: list, email: str) -> tuple[int, str]:
    outbox_row_i, outbox_info = find_in_outbox(outbox_data, email)
    if not outbox_row_i:
        return 0, ""

    name = outbox_info.get("name", "")
    if outbox_info.get("status") != "已回复":
        update_cell(sheets, OUTBOX_SHEET, outbox_row_i, 6, "已回复")

    return outbox_row_i, name


def should_process_row(row: list) -> bool:
    email = row_value(row, 2).lower()
    if not email:
        return False
    if TARGET_EMAIL and email != TARGET_EMAIL:
        return False
    if FORCE_REBUILD:
        return True
    # 默认只处理还没补完整的行，避免误覆盖已有人工记录。
    d = row_value(row, 3)
    e = row_value(row, 4)
    m = row_value(row, 12)
    n = row_value(row, 13)
    o = row_value(row, 14)
    return not (d and e and m and n and o)


def account_email_for_item(item: dict, own_emails: set[str]) -> str:
    if not item:
        return ""
    headers = item.get("headers", {})
    if item.get("speaker") == "我方":
        return item.get("from_email", "")
    for h in ("To", "Delivered-To", "Cc"):
        candidates = extract_emails(headers.get(h, ""))
        for email in candidates:
            if email in own_emails:
                return email
    return ""


def main():
    print(f"\n🧩 开始手动补全沟通管理 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  表：{REPLIES_SHEET}")
    print(f"  Claude模型：{CLAUDE_MODEL}")
    print("  规则：B/C由你填写；脚本补 A/D/E/F/G/K/L/M/N/O；只有达人有回复才补。")

    sheets_creds = get_creds()
    gmail_default_creds = None
    if os.environ.get("GOOGLE_TOKEN_JSON", "").strip():
        try:
            gmail_default_creds = get_creds(allow_service_account=False)
        except Exception as e:
            print(f"  ⚠️ GOOGLE_TOKEN_JSON 不可用，将只使用 GOOGLE_TOKEN_JSON_1~4：{e}")

    sheets = build("sheets", "v4", credentials=sheets_creds)
    own_emails = {acc["email"].lower() for acc in EMAIL_ACCOUNTS}
    gmail_accounts = build_gmail_accounts(gmail_default_creds)
    if not gmail_accounts:
        raise RuntimeError("没有可用的 Gmail token。请配置 GOOGLE_TOKEN_JSON_1~4，或重新生成 GOOGLE_TOKEN_JSON。")

    ensure_status_dropdown(sheets)
    data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data = get_sheet_data(sheets, OUTBOX_SHEET)
    if len(data) <= 1:
        print("  没有可处理行。")
        return

    processed = 0
    skipped = 0
    failed = 0

    for row_index, row in enumerate(data[1:], start=2):
        if not should_process_row(row):
            continue

        name = row_value(row, 1)
        target_email = row_value(row, 2).lower()
        status = row_value(row, 6)

        print(f"\n🔎 处理第 {row_index} 行：{name or '(未填名)'} <{target_email}>")

        if status == "已放弃" and not INCLUDE_ABANDONED:
            update_cell(sheets, REPLIES_SHEET, row_index, 12, "手动补充跳过：G列为已放弃")
            print("  ⏭️ G列为已放弃，跳过")
            skipped += 1
            continue

        try:
            items = collect_all_items_for_creator_across_accounts(gmail_accounts, own_emails, target_email)
            influencer_count = sum(1 for x in items if x["speaker"] == "达人")
            if not items:
                update_cell(sheets, REPLIES_SHEET, row_index, 12, "手动补充跳过：Gmail未找到相关邮件")
                print("  ⏭️ Gmail未找到相关邮件")
                skipped += 1
                continue
            if influencer_count == 0:
                update_cell(sheets, REPLIES_SHEET, row_index, 12, "手动补充跳过：只有我方开发信，达人未回复")
                print("  ⏭️ 只有我方开发信，达人未回复")
                skipped += 1
                continue

            result = analyze_conversation_with_claude(items)
            latest_influencer = result["latest_influencer"]
            latest_valid = result["latest_valid"]
            latest_date = latest_influencer["date_str"] if latest_influencer else latest_valid["date_str"]
            latest_message_id = latest_influencer["message_id"] if latest_influencer else latest_valid["message_id"]
            latest_subject = latest_influencer["subject"] if latest_influencer else latest_valid["subject"]
            max_internal_date = max(x["internal_date"] for x in items)
            receive_account = account_email_for_item(latest_influencer or latest_valid, own_emails)
            d_summary = f"{latest_date} 达人：{result['latest_summary']}"

            updates = {
                1: latest_date,                                # A 日期
                4: d_summary,                                  # D 回复摘要
                5: result["history"],                         # E 过往沟通
                6: result["stage"],                           # F 当前阶段
                7: status_for_stage(result["stage"]),         # G 状态
                11: receive_account,                           # K 收件邮箱
                12: f"手动补充自动填充：读取 {len(items)} 封邮件，达人回复 {influencer_count} 封",  # L 备注
                13: latest_message_id,                         # M Message-ID
                14: latest_subject,                            # N Subject
                15: str(max_internal_date),                    # O last_processed_internalDate
            }
            batch_update_row_cells(sheets, REPLIES_SHEET, row_index, updates)
            mark_outbox_replied(sheets, outbox_data, target_email)
            print(f"  ✅ 已补全：共 {len(items)} 封，达人回复 {influencer_count} 封")
            processed += 1
            time.sleep(1)
        except Exception as e:
            msg = f"手动补充失败：{e}"
            update_cell(sheets, REPLIES_SHEET, row_index, 12, msg[:500])
            print(f"  ⚠️ {msg}")
            failed += 1

    print(f"""
🎉 手动补全完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  成功：{processed}
  跳过：{skipped}
  失败：{failed}
""")


if __name__ == "__main__":
    main()
