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


def translate_to_chinese(text: str) -> str:
    if not text.strip() or looks_chinese(text):
        return text
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    prompt = f"""请把下面内容翻译成简洁中文，供商务团队内部查看。
要求：
- 保留邮箱、金额、链接、日期、产品名等关键信息。
- 不要扩写，不要总结成另一层意思。
- 如果原文是邮件正文片段，翻译成自然中文即可。

原文：
{text}
"""
    return model.generate_content(prompt).text.strip()


def main():
    if not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is required")

    creds = get_creds()
    sheets = build("sheets", "v4", credentials=creds)
    data = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{REPLIES_SHEET}!A:K",
    ).execute().get("values", [])

    updates = []
    translated = 0
    for row_index, row in enumerate(data[1:], start=2):
        summary = row[3] if len(row) > 3 else ""
        history = row[4] if len(row) > 4 else ""

        if summary and not looks_chinese(summary):
            updates.append({
                "range": f"{REPLIES_SHEET}!D{row_index}",
                "values": [[translate_to_chinese(summary)]],
            })
            translated += 1
            time.sleep(0.3)

        if history and not looks_chinese(history):
            updates.append({
                "range": f"{REPLIES_SHEET}!E{row_index}",
                "values": [[translate_to_chinese(history)]],
            })
            translated += 1
            time.sleep(0.3)

        if len(updates) >= 20:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
            updates = []

    if updates:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()

    print(json.dumps({"translated_cells": translated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
