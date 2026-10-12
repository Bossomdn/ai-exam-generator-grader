"""ค่าตั้งต้นของระบบ (โมเดล, effort ของแต่ละงาน, เกณฑ์ต่าง ๆ)"""

import os

from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

# ระดับความลึกในการคิดของแต่ละงาน (low | medium | high | xhigh | max)
EFFORT = {
    "generate": "high",  # ออกข้อสอบ + เฉลย + rubric ต้องละเอียด
    "grade": "medium",   # ตรวจอัตนัยทีละข้อ
    "advise": "medium",  # สรุปจุดอ่อนและคำแนะนำ
}

# ใช้ streaming จึงตั้ง max_tokens สูงได้โดยไม่ timeout
MAX_TOKENS = 64000

# ถ้าโมเดลปฏิเสธคำขอ ให้เซิร์ฟเวอร์ส่งต่อไปโมเดลสำรองอัตโนมัติ
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# หัวข้อที่ได้คะแนนต่ำกว่านี้ (%) ถือว่า "ต้องเรียนเพิ่ม"
WEAK_THRESHOLD = 70.0

# จำนวนข้ออัตนัยที่ตรวจพร้อมกัน
GRADING_WORKERS = 4


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
