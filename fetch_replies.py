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
from email.utils import parsedate_to_datetime

import anthropic
import google.generativeai as genai
from googleapiclient.discovery import build

from config import (
    ANTHROPIC_API_KEY,
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
STATUS_OPTIONS = ["待回复", "已回复", "已放弃", "无需回复"]

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


def append_to_history(sheets, row_index: int, new_entry: str):
    """往沟通管理E列追加一条记录，用换行分隔，仍保留在同一个单元格"""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!E{row_index}",
    ).execute()
    existing = ""
    values = result.get("values", [])
    if values and values[0]:
        existing = values[0][0].strip()
    updated = f"{existing}\n{new_entry}" if existing else new_entry
    update_cell(sheets, REPLIES_SHEET, row_index, 5, updated)

# ── Gemini ────────────────────────────────────────────────────────

def load_negotiation_cards() -> str:
    if os.path.exists(NEGOTIATION_CARDS):
        with open(NEGOTIATION_CARDS, encoding="utf-8") as f:
            return f.read()
    return ""


def ai_process_reply(
    reply_body: str,
    latest_summary: str,
    history: str,
    user_instruction: str,
    cards: str,
) -> dict:
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
- 不要编造任何数据，例如播放量、点赞数、销售额等，筹码库里没有提到的数字一律不写。

筹码库使用规则（重要）：
- 筹码库里的所有条件都是真实存在、可以直接用的，不需要用户在指令里重复提醒。
- 写回复时，根据当前阶段主动把相关筹码自然地融入邮件，不要遗漏。
- 转化激励金额固定是：带货达 $5,000，额外奖励 $300，不得改成其他数字。

上下文优先级规则（非常重要）：

信息优先级：

1. H列「本次人工指令」决定这封邮件的核心目标，是最高优先级。
2. D列「最新沟通摘要」代表当前合作状态，必须优先参考。
3. E列「过往沟通记录」用于理解历史上下文、双方关系、达人顾虑、已确认条款和沟通语气。
4. 当前邮件是本轮需要回应的对象。

写 suggested_reply 前，必须先判断：

* 双方当前已经推进到哪个阶段
* 哪些问题已经解决
* 哪些条件已经确认
* 对方此刻最在意什么
* 本次人工指令真正希望推进什么

suggested_reply 写作规则：

1. 邮件必须先自然回应达人最新邮件，再推进本次人工指令。
2. 即使 H列 有明确任务，也不能机械执行，要保持真人沟通感。
3. 可以自然加入：

* 感谢回复
* 回应确认
* 承接情绪
* 简短寒暄
  但这些只能作为“承接”，不能抢走邮件主目标。

4. suggested_reply 的核心目标必须围绕 H列人工指令推进。
5. 不要擅自新增新的谈判目标、报价、合作条件或产品介绍。
6. 如果当前阶段已经进入：

* 合同确认
* 付款
* 账号确认
* 发布时间
* 内容执行
  则禁止重新写：
* 初次合作邀约
* 产品介绍
* creative freedom
* bonus机制
* ad support
* 案例视频
* 旧报价

除非：

* 达人最新邮件重新提到
* 或 H列人工指令明确要求重新讨论

E列使用规则：

E列不是“参考资料”，而是长期上下文。

写邮件时，应参考 E列 来保持：

* 语气连续性
* 谈判逻辑连续性
* 双方熟悉程度
* 沟通节奏
* 已确认事实一致性

但不要重复：

* 已确认报价
* 初次合作邀约
* 已解决的问题
* 已谈妥的条款
  除非当前邮件或 H列再次提及。

特殊规则：

如果 H列只是：

* 确认账号
* 确认付款
* 确认合同
* 确认发布时间
* 确认物流
  等具体事项，

则邮件应简洁自然，
只完成当前确认动作，
不要为了“显得完整”而额外加入：

* 报价
* bonus
* ad support
* creative freedom
* 产品卖点
* 案例链接
* 合作介绍

suggested_reply 理想逻辑：

1. 自然回应当前邮件
2. 承接当前沟通氛围
3. 推进 H列核心目标
4. 简洁结束

目标：
让邮件像一个真实、有上下文记忆、长期跟进达人合作的 BD 在写，而不是像 AI 模板回复。

写邮件前必须先做以下分析，再动笔：
1. 对方现在最担心什么？从邮件和过往沟通里找，如果有顾虑，第一优先级是解决它。
2. 当前谈判处于什么阶段？是推进、守价、消除顾虑、还是确认合作？
3. 这封邮件的核心目标只有一个是什么？
4. 用最少的话达到这个目标，不要同时塞多个目的进一封邮件。

邮件结构根据情景灵活调整，但必须遵守以下原则：
1. "Hey [达人名字]," 开头，名字从邮件或过往沟通里提取，提取不到就用 "Hey there,"
2. 如果对方有顾虑，第一段必须先回应顾虑，让她放心，再进入正题
3. 如果是报价场景，顺序是：先说合作轻松有保障（ad support/创意自由）→ 再说报价 → 最后用转化激励收尾
4. 结尾用开放式问句或轻推进，不施压，不提合同/样品/下一步等跳步内容
5. 全程用「I」不用「We」，这是Eloise个人在写邮件

绝对禁止：
- 使用「We」作为主语
- 使用夸张词汇：fantastic、amazing、incredible、great results、惊人、极具吸引力等
- 编造任何未在筹码库或指令中出现的数字
- 开头超过1句话的客套
- 结尾跳步提到合同、样品、下一步流程

suggested_reply 写作风格：
- 口语化但专业，像真人写的，不像模板、不像广告、不像AI写的
- 简洁，每个句子都要有用，去掉所有废话
- 语气要匹配情景：消除顾虑场景要耐心诚实；降价场景要共情但有立场；对方接受时才可以用积极语气
- 在邮件正文中自然加入 1-2 个 emoji，符合语境，不堆砌
- 落款统一用 "Best, Eloise"

风格示例一（消除顾虑场景，模仿这个语气和逻辑）：
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

风格示例二（报价/守价场景，模仿这个语气和逻辑）：
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

最新沟通摘要（D列）：
{latest_summary if latest_summary else "无"}

过往沟通记录（E列）：
{history if history else "无"}

本次人工指令（H列，最高优先级）：
{user_instruction if user_instruction else "无"}

请严格按以下 JSON 格式返回，不要加任何其他内容：
{{
  "summary": "用1-2句中文概括回复的核心内容",
  "stage": "从以下选一个：初次感兴趣 / 价格谈判中 / 犹豫不决 / 已拒绝可挽回 / 已成交 / 其他",
  "suggested_reply": "根据阶段和筹码库，用美式英语写一封完整的建议回复邮件"
}}

可用筹码库：
{cards}
"""
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text.strip()
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
格式要求：直接写重点，不加任何前缀，不超过1句话。
例如：提议$400/条，附转化激励$300，给创意自由和ad support

只返回总结内容，不要加任何前缀或说明。

邮件内容：
{body[:1000]}
"""
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        return "（摘要生成失败）"


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


def _headers(msg: dict) -> dict:
    return {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


def _extract_email(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    return match.group(0).lower() if match else ""


def _extract_emails(value: str) -> set[str]:
    return {
        match.lower()
        for match in re.findall(r"[\w.+-]+@[\w.+-]+\.\w+", value or "")
    }


def _parse_email_date(date_header: str, fallback: str) -> str:
    try:
        return parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
    except Exception:
        return fallback


def build_thread_history(
    gmail,
    thread_id: str,
    own_emails: list[str],
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
    own_email_set = {email.lower() for email in own_emails}
    target_email = target_email.lower()
    thread_msgs = sorted(
        thread.get("messages", []),
        key=lambda item: int(item.get("internalDate", "0")),
    )

    for thread_msg in thread_msgs:
        headers = _headers(thread_msg)
        message_id = headers.get("Message-ID", "")
        if message_id and message_id in seen_message_ids:
            continue
        if message_id:
            seen_message_ids.add(message_id)

        body = _extract_body(thread_msg)
        if not body:
            continue

        from_email = _extract_email(headers.get("From", ""))
        if from_email in own_email_set:
            recipients = set()
            for header_name in ("To", "Cc", "Bcc", "Delivered-To"):
                recipients.update(_extract_emails(headers.get(header_name, "")))
            if target_email not in recipients and message_id != current_message_id:
                continue
            speaker = "我方"
        elif from_email == target_email:
            speaker = "达人"
        else:
            continue
        date_str = _parse_email_date(headers.get("Date", ""), fallback_date)
        if message_id and current_message_id and message_id == current_message_id:
            summary = current_summary
        else:
            summary = ai_summarize_history_message(body)
        entries.append(f"{date_str} {speaker}：{summary}")

    return "\n".join(entries) if entries else f"{fallback_date} 达人：{current_summary}"


def fetch_new_replies(gmail, last_run: datetime) -> list[dict]:
    since = last_run.strftime("%Y/%m/%d")
    all_replies = []
    own_emails = [acc["email"].lower() for acc in EMAIL_ACCOUNTS]

    for account in EMAIL_ACCOUNTS:
        query = f"in:inbox to:{account['email']} after:{since}"
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
            label_ids = set(msg.get("labelIds", []))
            if "INBOX" not in label_ids or "SENT" in label_ids or "DRAFT" in label_ids:
                print(f"  ⏭️ 跳过非收件箱邮件：{msg_ref['id']}")
                continue

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            from_match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", headers.get("From", ""))
            from_email = from_match.group(0) if from_match else ""

            if from_email.lower() in own_emails:
                continue

            # ── 新增：检查 thread 里是否真的有外部人回复 ──────────
            thread_id = msg.get("threadId", "")
            if thread_id:
                try:
                    thread = gmail.users().threads().get(
                        userId="me", id=thread_id, format="metadata",
                        metadataHeaders=["From"]
                    ).execute()
                    thread_msgs = thread.get("messages", [])
                    has_external = False
                    for tm in thread_msgs:
                        tm_from = next(
                            (h["value"] for h in tm["payload"]["headers"] if h["name"] == "From"),
                            ""
                        )
                        tm_email_match = re.search(r"[\w.+-]+@[\w.+-]+\.\w+", tm_from)
                        if tm_email_match and tm_email_match.group(0).lower() not in own_emails:
                            has_external = True
                            break
                    if not has_external:
                        print(f"  ⏭️ 跳过无回复线程：{from_email}")
                        continue
                except Exception as e:
                    print(f"  ⚠️ 检查线程失败，默认保留：{e}")
            # ── 新增结束 ───────────────────────────────────────────

            body = _extract_body(msg)
            if from_email and body:
                all_replies.append({
                    "from_email": from_email,
                    "to_email": account["email"],
                    "date": headers.get("Date", ""),
                    "body": body,
                    "thread_id": msg.get("threadId", ""),
                    "internal_date": int(msg.get("internalDate", "0")),
                    "message_id": headers.get("Message-ID", ""),
                    "subject": headers.get("Subject", ""),
                })

    latest_by_email = {}
    for reply in all_replies:
        email = reply["from_email"].lower()
        if email not in latest_by_email or reply["internal_date"] > latest_by_email[email]["internal_date"]:
            latest_by_email[email] = reply

    return list(latest_by_email.values())


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
    ensure_status_dropdown(sheets)

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    today = datetime.now().strftime("%Y-%m-%d")

    # ── 1. 抓取收件箱新回复 ───────────────────────────────────────
    print("\n📥 扫描收件箱...")
    new_replies = fetch_new_replies(gmail, last_run)
    print(f"  找到 {len(new_replies)} 封新邮件")

    for reply in new_replies:
        from_email = reply["from_email"].lower()
        reply_date = _parse_email_date(reply.get("date", ""), today)
        existing_row = find_in_replies(replies_data, from_email)

        # 获取最新摘要、过往沟通和人工指令
        latest_summary = ""
        history = ""
        user_instruction = ""
        if existing_row:
            row = replies_data[existing_row - 1]
            if len(row) > 3:
                latest_summary = row[3].strip()
            if len(row) > 4:
                history = row[4].strip()
            if len(row) > 7:
                user_instruction = row[7].strip()

        # 把当前这封新邮件也拼进 history，让 AI 看到完整上下文
        current_email_entry = f"{reply_date} 达人来信：{reply['body'][:500]}"
        full_history = f"{history} | {current_email_entry}" if history else current_email_entry

        # AI 处理
        try:
            ai_result = ai_process_reply(
                reply["body"],
                latest_summary,
                full_history,
                user_instruction,
                cards,
            )
        except Exception as e:
            print(f"  ⚠️ AI失败：{e}")
            ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

        summary_entry = f"{reply_date} 达人：{ai_result['summary']}"

        if existing_row:
            # 已有达人 → 更新 A/D/E/F/G/I/M/N 列，不动 H/J/K/L
            print(f"  更新已有达人：{from_email}")
            update_cell(sheets, REPLIES_SHEET, existing_row, 1, reply_date)         # A 日期
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

            thread_history = build_thread_history(
                gmail,
                reply.get("thread_id", ""),
                [acc["email"] for acc in EMAIL_ACCOUNTS],
                from_email,
                reply.get("message_id", ""),
                ai_result["summary"],
                reply_date,
            )

            new_row = [
                reply_date,                      # A 日期
                name,                            # B 达人名字
                reply["from_email"],              # C 邮箱
                summary_entry,                   # D 回复摘要
                thread_history,                  # E 过往沟通
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

        existing_row = find_in_replies(replies_data, to_email)
        if not existing_row:
            print(f"  跳过（达人未回复）：{to_email}")
            continue

        summary = ai_summarize_sent(sent["body"])
        entry = f"{today} 我方：{summary}"
        append_to_history(sheets, existing_row, entry)
        print(f"  ✅ 追加到E列")
        replies_data = get_sheet_data(sheets, REPLIES_SHEET)
        time.sleep(1)

    # ── 保存时间戳 ────────────────────────────────────────────────
    save_last_run_time(sheets)
    print(f"\n🎉 收邮件完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
