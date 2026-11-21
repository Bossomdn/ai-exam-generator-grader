"""ค่าตั้งต้นของระบบ (โมเดล, effort ของแต่ละงาน, เกณฑ์ต่าง ๆ)"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

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
    "plan": "low",       # แปลงคำสั่งเป็นสเปกข้อสอบ
    "generate": "high",  # ออกข้อสอบ + เฉลย + rubric ต้องละเอียด
    "review": "high",    # ทำข้อสอบเองโดยไม่เห็นเฉลย + หาจุดบกพร่อง
    "grade": "medium",   # ตรวจอัตนัยทีละข้อ
    "moderate": "high",  # ตัดสินเมื่อคณะผู้ตรวจเรียงความเห็นต่าง
    "advise": "medium",  # สรุปจุดอ่อนและคำแนะนำ
    "read": "medium",    # ถอดคำตอบจากกระดาษคำตอบ PDF/รูป
}

# ใช้ streaming จึงตั้ง max_tokens สูงได้โดยไม่ timeout
MAX_TOKENS = 64000

# ถ้าโมเดลปฏิเสธคำขอ ให้เซิร์ฟเวอร์ส่งต่อไปโมเดลสำรองอัตโนมัติ
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# จำนวนรอบสูงสุดที่ Quality Reviewer ส่งข้อที่มีปัญหากลับไปให้ Generator แก้
MAX_REVISIONS = 2

# จำนวนข้อสูงสุดต่อชุด (โมเดลในเครื่องมี context จำกัด ข้อสอบยาวเกินจะถูกตัด)
MAX_QUESTIONS = 20 if PROVIDER == "ollama" else 40

# หัวข้อที่ได้คะแนนต่ำกว่านี้ (%) ถือว่า "ต้องเรียนเพิ่ม"
WEAK_THRESHOLD = 70.0

# จำนวนข้ออัตนัยที่ตรวจพร้อมกัน
GRADING_WORKERS = 4

# วิธีตรวจเรียงความ: panel = คณะผู้ตรวจ 3 มุมมอง + Moderator (ใช้เวลาประมาณ 3 เท่า), single = ผู้ตรวจคนเดียว
GRADER_MODE = os.getenv("GRADER_MODE", "panel").strip().lower()
PANEL_TYPES = ("essay",)  # ชนิดข้อที่ใช้คณะผู้ตรวจ (ตอบสั้นมีประเด็นชัด ใช้ผู้ตรวจคนเดียวพอ)
# คะแนนในคณะต่างกันไม่เกินสัดส่วนนี้ของคะแนนเต็มข้อ = เห็นตรงกัน ใช้คะแนนกลาง ไม่ต้องเรียก Moderator
PANEL_AGREE = 0.2
# ต่างกันเกินสัดส่วนนี้ = Moderator ตัดสินแล้ว แต่ยังขึ้นเตือนให้ครูตรวจซ้ำ
PANEL_ESCALATE = 0.4

# โฟลเดอร์เก็บข้อมูลนักเรียน: ฐานข้อมูลคะแนน (school.db) และไฟล์สำรอง (backups/)
# ค่าเริ่มต้นคือ data/ ในโปรเจกต์ ซึ่ง git ไม่สนใจ · path แบบสัมพัทธ์นับจากโฟลเดอร์โปรเจกต์
DATA_DIR = ROOT / Path(os.getenv("DATA_DIR", "data")).expanduser()
DB_PATH = DATA_DIR / "school.db"


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def model_name() -> str:
    return OLLAMA_MODEL if PROVIDER == "ollama" else MODEL
