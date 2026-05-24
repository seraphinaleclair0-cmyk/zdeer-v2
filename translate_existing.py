import json
import os
import re
import time

import google.generativeai as genai
from googleapiclient.discovery import build

from config import GEMINI_API_KEY, GEMINI_MODEL, REPLIES_SHEET, SHEET_ID
from google_auth import get_creds


def looks_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def translate_to_chinese(model, text: str) -> str:
    """翻译或改写成中文摘要，调用前已在外层加了 sleep"""
    if not text.strip() or looks_chinese(text):
        return text

    prompt = f"""请把下面内容改写成简洁中文，供商务团队内部查看。
要求：
- 保留邮箱、金额、链接、日期、产品名等关键信息。
- 不要扩写，不要总结成另一层意思。
- 如果是系统通知或退信，直接写：「系统通知：xxx」或「退信通知：xxx」。
- 如果是达人回复，写：「达人回复：xxx」。
- 输出1-2句中文即可。

原文：
{text}
"""
    try:
        return model.generate_content(prompt).text.strip()
    except Exception as e:
        print(f"  ⚠️ Gemini失败：{e}，等待10秒后跳过")
        time.sleep(10)
        return text  # 失败就保留原文，不中断


def main():
    if not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is required")

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)

    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds)

    data = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A:K",
    ).execute().get("values", [])

    print(f"共 {len(data) - 1} 行需要检查")

    updates = []
    translated = 0

    for row_index, row in enumerate(data[1:], start=2):
        summary = row[3] if len(row) > 3 else ""
        history = row[4] if len(row) > 4 else ""

        if summary and not looks_chinese(summary):
            print(f"  行{row_index} D列翻译中...")
            time.sleep(4)  # 防限流：每次调用前等4秒
            new_summary = translate_to_chinese(model, summary)
            updates.append({
                "range": f"{REPLIES_SHEET}!D{row_index}",
                "values": [[new_summary]],
            })
            translated += 1

        if history and not looks_chinese(history):
            print(f"  行{row_index} E列翻译中...")
            time.sleep(4)  # 防限流
            new_history = translate_to_chinese(model, history)
            updates.append({
                "range": f"{REPLIES_SHEET}!E{row_index}",
                "values": [[new_history]],
            })
            translated += 1

        # 每20条批量写入一次，避免积压
        if len(updates) >= 20:
            print(f"  批量写入 {len(updates)} 条...")
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            updates = []
            time.sleep(2)

    # 写入剩余
    if updates:
        print(f"  最终写入 {len(updates)} 条...")
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    print(json.dumps({"translated_cells": translated}, ensure_ascii=False))
    print("✅ 翻译完成")


if __name__ == "__main__":
    main()
