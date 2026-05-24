"""
regenerate.py — 只处理「沟通管理」中打了 🔄 的行
重新根据 H列指令生成 I列草稿，不发任何邮件，不碰待发名单
"""

import time
from datetime import datetime

import google.generativeai as genai
from googleapiclient.discovery import build

from config import (
    EMAIL_ACCOUNTS,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    NEGOTIATION_CARDS,
    REPLIES_SHEET,
    SHEET_ID,
)
from google_auth import get_creds
from main import (
    ai_regenerate_reply,
    get_sheet_data,
    load_negotiation_cards,
    update_cell,
)


def main():
    print(f"\n🔄 开始重新生成草稿 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    creds  = get_creds()
    sheets = build("sheets", "v4", credentials=creds)
    cards  = load_negotiation_cards()

    replies_data = get_sheet_data(sheets, REPLIES_SHEET)
    regenerated  = 0

    for i, row in enumerate(replies_data[1:], start=2):
        if len(row) < 10:
            continue

        stage       = row[5].strip() if len(row) > 5 else ""
        instruction = row[7].strip() if len(row) > 7 else ""
        send_flag   = row[9].strip() if len(row) > 9 else ""
        history     = row[4].strip() if len(row) > 4 else ""

        if send_flag != "🔄":
            continue

        if not instruction:
            print(f"  ⚠️ 行{i} 打了🔄但H列没有指令，跳过")
            update_cell(sheets, REPLIES_SHEET, i, 10, "")  # 清空🔄
            continue

        try:
            print(f"  🔄 行{i} 重新生成中：{instruction[:30]}...")
            new_reply = ai_regenerate_reply(instruction, history, stage, cards)
            update_cell(sheets, REPLIES_SHEET, i, 9, new_reply)
            update_cell(sheets, REPLIES_SHEET, i, 10, "")  # 清空🔄
            regenerated += 1
            print(f"  ✅ 行{i} 生成完成，等你确认后打 ✅ 发送")
        except Exception as e:
            print(f"  ⚠️ 行{i} 生成失败：{e}")

    print(f"\n🎉 完成 — 共重新生成 {regenerated} 条 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
