"""
main.py — 核心逻辑
GitHub Actions 每天 8:00 / 12:00 / 17:00 运行，完成：
1. 检查「待发名单」有无新的 ✅ → 发邮件
2. 检查无回复达人 → 发跟进邮件
3. 抓取新回复 → 匹配待开发名单 → AI处理 → 写入「沟通管理」
4. 扫描已发邮件 → 更新E列过往沟通
5. 检查「沟通管理」有无新的 ✅ → 发回复邮件
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
# 沟通管理列（A~K）
REPLIES_COLS = ["日期", "达人名字", "邮箱", "回复摘要", "过往沟通", "当前阶段", "状态", "你的指令", "AI生成回复", "发送", "收件邮箱"]
# 系统配置sheet名
CONFIG_SHEET = "系统配置"

# ── 时间戳管理 ────────────────────────────────────────────────────

def get_last_run_time(sheets) -> datetime:
    """从系统配置sheet读取上次运行时间，默认返回24小时前"""
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
    """把当前时间写入系统配置sheet"""
    try:
        # 确保表头存在
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CONFIG_SHEET}!A1",
            valueInputOption="RAW",
            body={"values": [["上次运行时间（UTC）"]]},
        ).execute()
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{CONFIG_SHEET}!A2",
            valueInputOption="RAW",
            body={"values": [[datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")]]},
        ).execute()
    except Exception as e:
        print(f"  ⚠️ 保存时间戳失败：{e}")

# ── 邮件发送 ──────────────────────────────────────────────────────

def get_account_by_email(email: str) -> dict:
    """根据邮箱地址找到对应账号配置"""
    for acc in EMAIL_ACCOUNTS:
        if acc["email"].lower() == email.lower():
            return acc
    return EMAIL_ACCOUNTS[0]


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

# ── Gemini AI ─────────────────────────────────────────────────────

def load_negotiation_cards() -> str:
    if os.path.exists(NEGOTIATION_CARDS):
        with open(NEGOTIATION_CARDS, encoding="utf-8") as f:
            return f.read()
    return ""


def ai_summarize_sent(body: str) -> str:
    """把我发出的邮件正文总结成中文摘要（用于E列记录）"""
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


def ai_process_reply(reply_body: str, history: str, cards: str) -> dict:
    time.sleep(4)
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一个有经验的 TikTok 红人营销 BD，正在代表品牌与达人沟通合作。
以下是达人发来的回复邮件，请完成三件事。

语言规则：
- summary 必须写中文，方便团队内部阅读。
- stage 必须写中文，只能从给定选项中选择。
- suggested_reply 必须写自然、专业的美式英语。
- suggested_reply 不要出现中文，不要中英混杂。
- 不要编造预算、物流、合同、付款、库存等未提供的信息。

谈判思路（写邮件前先想清楚这几点，再动笔）：
1. 达人最在意什么？钱、曝光、还是合作体验？从她的回复里判断，针对性回应。
2. 先共情，再给信息。不要上来就报价或列条件，先让她感觉被理解。
3. 用「我为你争取到了」而不是「我们提供」——前者有温度，后者像报价单。
4. 把亮点按说服力排序：先说最打动人的（比如无审核、ad support），再说报价，最后用转化激励做收尾甜头。
5. 结尾要推进行动，但不施压——用开放式问句邀请她表态，而不是催促。
6. 如果对方报价高，不要直接拒绝，而是重新框定价值：我们给的不只是钱，还有流量放大和长期合作机会。

suggested_reply 写作风格要求：
- 像一个有经验的红人营销人写的，口语化但专业
- 委婉礼貌，但有立场，不轻易让步
- 不用模板化开头，例如"I hope this email finds you well"之类
- 简洁，去掉一切废话和过度客套
- 在邮件正文中自然地加入 1-2 个 emoji，友好但不过度，符合语境
- 邮件落款统一用 "Best, Eloise"，不用其他签名格式

风格示例（模仿这个语气、节奏和结构，不要照抄内容）：
---
Hey Sylvia,

Great to hear back from you!

I went back to my team and pushed for the budget, and we've managed to secure $500 for 1 video. Would you be open to this?

I know this is a bit lower than what you were expecting, so I also added a bonus perk: for every $5,000 in sales (about 125 orders), we'll pay you an extra $500 — there's no limit to this bonus.

Actually, this product gets really solid organic traffic — you can check out this viral video, which already has 330K likes!
https://www.tiktok.com/@maximumboom/video/7460634333234302254

The viral video from a few days ago:
https://www.tiktok.com/@mcamila0301/video/7640311080094403854

Plus, for your videos, you only need to mention the core selling points; the rest is completely up to your creativity, and there's no need to send videos for approval — should make things a lot easier for you. 🙌

I really feel that with your base traffic plus our ad support, these videos could perform really well. If you're interested, we can start with this as our first collab and explore more ways to work together down the line!

Do you think this would work for you? I'd love to hear your thoughts — I truly believe in you and would be so excited to collaborate! 🎉

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


def ai_regenerate_reply(instruction: str, history: str, stage: str, cards: str) -> str:
    """根据你的中文指令重新生成英文回复"""
    time.sleep(4)
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    prompt = f"""你是一个有经验的 TikTok 红人营销 BD，正在代表品牌与达人沟通合作。

当前谈判阶段：{stage}
过往沟通记录（中文内部记录）：{history if history else "无"}
我的指令（中文）：{instruction}

可用筹码库：
{cards}

请根据以上信息，用自然、专业的美式英语写一封完整回复邮件。
要求：
- 只返回英文邮件正文，不要加任何说明。
- 不要出现中文，不要中英混杂。
- 不要直译中文指令，要写成美国达人能自然理解的表达。
- 不要编造预算、物流、合同、付款、库存等未提供的信息。
- 像一个有经验的红人营销人写的，口语化但专业。
- 委婉礼貌，但有立场，不轻易让步。
- 不用模板化开头，例如"I hope this email finds you well"之类。
- 简洁，去掉一切废话和过度客套。
- 在邮件正文中自然地加入 1-2 个 emoji，友好但不过度，符合语境。
- 邮件落款统一用 "Best, Eloise"，不用其他签名格式。
"""
    response = model.generate_content(prompt)
    return response.text.strip()

# ── 抓取 Gmail 收件箱回复 ─────────────────────────────────────────

def fetch_new_replies(gmail, last_run: datetime) -> list[dict]:
    """抓取上次运行时间之后收到的新邮件"""
    since = last_run.strftime("%Y/%m/%d")
    all_replies = []

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
            date_str = headers.get("Date", "")

            # 过滤掉自己发给自己的
            own_emails = [acc["email"].lower() for acc in EMAIL_ACCOUNTS]
            if from_email.lower() in own_emails:
                continue

            body = _extract_body(msg)
            if from_email and body:
                all_replies.append({
                    "from_email": from_email,
                    "to_email": account["email"],
                    "date": date_str,
                    "body": body,
                })

    return all_replies


def fetch_sent_emails(gmail, last_run: datetime) -> list[dict]:
    """抓取上次运行时间之后我发出的邮件"""
    since = last_run.strftime("%Y/%m/%d")
    all_sent = []

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
            date_str = headers.get("Date", "")

            # 过滤掉发给自己的
            own_emails = [acc["email"].lower() for acc in EMAIL_ACCOUNTS]
            if to_email.lower() in own_emails:
                continue

            body = _extract_body(msg)
            if to_email and body:
                all_sent.append({
                    "to_email": to_email,
                    "from_email": account["email"],
                    "date": date_str,
                    "body": body,
                })

    return all_sent


def _extract_body(msg: dict) -> str:
    """从Gmail消息中提取纯文本正文"""
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


def get_history_for_email(sheets, email: str) -> str:
    data = get_sheet_data(sheets, REPLIES_SHEET)
    items = []
    for row in data[1:]:
        if len(row) > 2 and row[2].strip().lower() == email.strip().lower():
            if len(row) > 4 and row[4]:
                items.append(row[4])
    return items[-1] if items else ""

# ── 待开发名单操作 ────────────────────────────────────────────────

def find_in_outbox(outbox_data: list, email: str) -> tuple[int, dict]:
    """在待开发名单中查找邮箱，返回(行号从2开始, 行数据dict)，找不到返回(0, {})"""
    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) > 1 and row[1].strip().lower() == email.strip().lower():
            return i, {
                "name": row[0].strip() if len(row) > 0 else "",
                "email": row[1].strip() if len(row) > 1 else "",
                "sender_email": row[3].strip() if len(row) > 3 else "",
                "status": row[5].strip() if len(row) > 5 else "",
            }
    return 0, {}


def find_in_replies(replies_data: list, email: str) -> int:
    """在沟通管理中查找邮箱，返回行号（从2开始），找不到返回0"""
    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) > 2 and row[2].strip().lower() == email.strip().lower():
            return i
    return 0

# ── 主流程 ────────────────────────────────────────────────────────

def main():
    print(f"\n🚀 开始运行 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds = get_creds()
    gmail  = build("gmail",  "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    cards  = load_negotiation_cards()

    # 读取上次运行时间
    last_run = get_last_run_time(sheets)
    print(f"  上次运行时间（UTC）：{last_run.strftime('%Y-%m-%d %H:%M:%S')}")

    # 确保表头存在
    ensure_headers(sheets, OUTBOX_SHEET,  OUTBOX_COLS)
    ensure_headers(sheets, REPLIES_SHEET, REPLIES_COLS)

    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)

    # ── 1. 处理「待发名单」中标了 ✅ 的行 ────────────────────────
    print("\n📤 检查待发名单...")
    sent_count = 0

    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 5:
            continue

        name         = row[0].strip() if len(row) > 0 else ""
        to_email     = row[1].strip() if len(row) > 1 else ""
        tiktok_url   = row[2].strip() if len(row) > 2 else ""
        sender_email = row[3].strip() if len(row) > 3 else ""
        send_flag    = row[4].strip() if len(row) > 4 else ""
        status       = row[5].strip() if len(row) > 5 else ""

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

    replied_emails = set()
    for row in replies_data[1:]:
        if len(row) > 2:
            replied_emails.add(row[2].strip().lower())

    for i, row in enumerate(outbox_data[1:], start=2):
        if len(row) < 7:
            continue

        name         = row[0].strip()
        to_email     = row[1].strip()
        tiktok_url   = row[2].strip()
        sender_email = row[3].strip() if len(row) > 3 else ""
        status       = row[5].strip() if len(row) > 5 else ""
        sent_time    = row[6].strip() if len(row) > 6 else ""
        followup     = int(row[7].strip()) if len(row) > 7 and row[7].strip().isdigit() else 0

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
    new_replies = fetch_new_replies(gmail, last_run)
    print(f"  找到 {len(new_replies)} 封新回复")

    # 重新读取最新数据
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)

    for reply in new_replies:
        from_email = reply["from_email"].lower()
        today = datetime.now().strftime("%Y-%m-%d")

        # 检查沟通管理是否已有这个达人
        existing_row = find_in_replies(replies_data, from_email)
        if existing_row:
            # 已有 → 只追加D列摘要和E列过往沟通
            print(f"  已有记录，追加回复摘要：{from_email}")
            history = get_history_for_email(sheets, from_email)
            try:
                ai_result = ai_process_reply(reply["body"], history, cards)
            except Exception as e:
                print(f"  ⚠️ AI失败：{e}")
                ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

            # 更新D列回复摘要
            update_cell(sheets, REPLIES_SHEET, existing_row, 4,
                        f"{today} 达人回复：{ai_result['summary']}")
            # 追加E列过往沟通
            append_to_history(sheets, existing_row,
                              f"{today} 达人回复：{ai_result['summary']}")
            # 更新AI生成回复
            if ai_result["suggested_reply"]:
                update_cell(sheets, REPLIES_SHEET, existing_row, 9, ai_result["suggested_reply"])
            continue

        # 沟通管理没有 → 去待开发名单查
        outbox_row_i, outbox_info = find_in_outbox(outbox_data, from_email)

        history = get_history_for_email(sheets, from_email)
        try:
            ai_result = ai_process_reply(reply["body"], history, cards)
        except Exception as e:
            print(f"  ⚠️ AI失败：{e}")
            ai_result = {"summary": "AI处理失败", "stage": "其他", "suggested_reply": ""}

        name = outbox_info.get("name", "")
        receiver_email = reply.get("to_email", "")

        if outbox_row_i:
            # 待开发名单找到 → 改状态为「已回复」
            print(f"  待开发名单找到 {name}，改状态为已回复")
            update_cell(sheets, OUTBOX_SHEET, outbox_row_i, 6, "已回复")

        # 写入沟通管理新行
        new_row = [
            today,
            name,
            reply["from_email"],
            f"{today} 达人回复：{ai_result['summary']}",
            f"{today} 达人回复：{ai_result['summary']}",
            ai_result["stage"],
            "待回复",
            "",
            ai_result["suggested_reply"],
            "",
            receiver_email,
        ]
        append_row(sheets, REPLIES_SHEET, new_row)
        print(f"  ✅ 写入沟通管理：{reply['from_email']}")

        # 更新本地缓存，避免同一批次重复写入
        replies_data = get_sheet_data(sheets, REPLIES_SHEET)
        time.sleep(1)

    # ── 4. 扫描已发邮件，更新E列过往沟通 ────────────────────────
    print("\n📤 扫描已发邮件...")
    sent_emails = fetch_sent_emails(gmail, last_run)
    print(f"  找到 {len(sent_emails)} 封已发邮件")

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    outbox_data  = get_sheet_data(sheets, OUTBOX_SHEET)
    today = datetime.now().strftime("%Y-%m-%d")

    for sent in sent_emails:
        to_email = sent["to_email"].lower()

        # 生成中文摘要
        print(f"  处理已发邮件 → {to_email}")
        summary = ai_summarize_sent(sent["body"])
        entry = f"{today} 我回复：{summary}"

        # 查沟通管理
        existing_row = find_in_replies(replies_data, to_email)
        if existing_row:
            append_to_history(sheets, existing_row, entry)
            print(f"  ✅ 追加到沟通管理E列")
            replies_data = get_sheet_data(sheets, REPLIES_SHEET)
            continue

        # 查待开发名单
        outbox_row_i, outbox_info = find_in_outbox(outbox_data, to_email)
        name = outbox_info.get("name", "")

        # 两边都没有 → 沟通管理新建一行
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

    # ── 5. 处理「沟通管理」中的 🔄 / ✅ ────────────────────────
    print("\n💬 检查待发回复...")
    replies_data = get_sheet_data(sheets, REPLIES_SHEET)

    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) < 10:
            continue

        to_email    = row[2].strip() if len(row) > 2 else ""
        stage       = row[5].strip() if len(row) > 5 else ""
        instruction = row[7].strip() if len(row) > 7 else ""
        ai_reply    = row[8].strip() if len(row) > 8 else ""
        send_flag   = row[9].strip() if len(row) > 9 else ""
        history     = row[4].strip() if len(row) > 4 else ""
        receiver    = row[10].strip() if len(row) > 10 else ""

        # J列填 🔄：只根据H列中文指令重新生成I列草稿，不发邮件。
        if send_flag == "🔄":
            if not instruction:
                print(f"  ⚠️ 第{i}行标了🔄但H列没有指令，跳过")
                continue
            try:
                print(f"  根据指令重新生成回复：{instruction}")
                new_reply = ai_regenerate_reply(instruction, history, stage, cards)
                update_cell(sheets, REPLIES_SHEET, i, 9, new_reply)
                update_cell(sheets, REPLIES_SHEET, i, 10, "")  # 清空🔄防止重复生成
                print("  ✅ 草稿已更新到I列")
            except Exception as e:
                print(f"  ⚠️ 重新生成失败：{e}")
            continue

        if send_flag != "✅":
            continue

        if not ai_reply or not to_email:
            continue

        # 用收件邮箱对应的发件账号回复
        account = get_account_by_email(receiver) if receiver else EMAIL_ACCOUNTS[0]
        subject = "Re: Zdeer Collaboration"
        print(f"  回复 <{to_email}> via {account['email']}")
        ok = send_email(account, to_email, subject, ai_reply)

        if ok:
            update_cell(sheets, REPLIES_SHEET, i, 7, "已回复")
            update_cell(sheets, REPLIES_SHEET, i, 10, "")  # 清空✅防止重复发
            # 追加E列记录
            append_to_history(sheets, i,
                              f"{datetime.now().strftime('%Y-%m-%d')} 我回复：（已发送）")
            print(f"  ✅ 回复成功")

        wait = random.randint(15, 30)
        time.sleep(wait)

    # ── 保存本次运行时间戳 ────────────────────────────────────────
    save_last_run_time(sheets)
    print(f"\n🎉 运行完成 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
