"""ค่าตั้งต้นของระบบ (โมเดล, effort ของแต่ละงาน, เกณฑ์ต่าง ๆ)"""

import os

from dotenv import load_dotenv

load_dotenv()

# เลือกผู้ให้บริการโมเดล: "ollama" (ฟรี รันในเครื่อง) หรือ "claude" (Claude API เสียเงินตามการใช้งาน)
PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:12b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
# ค่า default ของ Ollama สั้นมาก ต้องขยายไม่งั้นเนื้อหาสไลด์ยาว ๆ จะถูกตัด
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))
# true = ให้โมเดลคิดก่อนตอบ (แม่นขึ้นแต่ช้าลง)
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").strip().lower() == "true"

# ระดับความลึกในการคิดของแต่ละงาน (low | medium | high | xhigh | max) — ใช้กับ Claude
EFFORT = {
    "generate": "high",  # ออกข้อสอบ + เฉลย + rubric ต้องละเอียด
    "grade": "medium",   # ตรวจอัตนัยทีละข้อ
    "advise": "medium",  # สรุปจุดอ่อนและคำแนะนำ
    "read": "medium",    # ถอดคำตอบจากกระดาษคำตอบ PDF/รูป
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


def model_name() -> str:
    return OLLAMA_MODEL if PROVIDER == "ollama" else MODEL
