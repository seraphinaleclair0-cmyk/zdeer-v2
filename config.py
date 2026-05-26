import os

EMAIL_ACCOUNTS = [
    {
        "email": "seraphinaleclair0@gmail.com",
        "password": os.environ.get("GMAIL_PASS_1", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    {
        "email": "eloise87657@gmail.com",
        "password": os.environ.get("GMAIL_PASS_2", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    {
        "email": "cjtjieting@gmail.com",
        "password": os.environ.get("GMAIL_PASS_3", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
    {
        "email": "pr@zdeer.org",
        "password": os.environ.get("GMAIL_PASS_4", ""),
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    },
]

SHEET_ID      = "1q398s31RRTSVly1Dp9crzBi0LyEfCP-VdnfkGVLJp-E"
OUTBOX_SHEET  = "待发名单"
REPLIES_SHEET = "沟通管理"

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = "gemini-2.5-flash-lite"

NEGOTIATION_CARDS = "negotiation_cards.txt"
TOKEN_JSON        = "token.json"
CREDENTIALS_JSON  = "credentials.json"

SEND_INTERVAL_MIN = 12
SEND_INTERVAL_MAX = 20
