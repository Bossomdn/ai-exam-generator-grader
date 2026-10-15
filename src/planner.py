"""Request Planner: แปลงคำสั่งภาษาธรรมดาของผู้ใช้ + เนื้อหา → สเปกข้อสอบ (ExamSpec)

สเปกนี้แสดงให้ผู้ใช้ตรวจ/แก้ก่อนสร้างข้อสอบ (human-in-the-loop) แล้วจึงส่งต่อให้ Question Generator
"""

from .config import EFFORT, MAX_QUESTIONS
from .llm import call_structured
from .schemas import DIFFICULTY_LABELS, TYPE_LABELS, ExamSpec, SectionSpec
from .sources import Source

DEFAULT_POINTS = {"mcq": 1.0, "true_false": 1.0, "short_answer": 3.0, "essay": 10.0}

SYSTEM_PROMPT = """คุณคือ Request Planner ของระบบออกข้อสอบ หน้าที่คืออ่านคำสั่งภาษาธรรมดาของผู้ใช้ร่วมกับเนื้อหา แล้วแปลงเป็นสเปกข้อสอบที่ชัดเจน (ยังไม่ต้องออกข้อสอบ)

## sections (ชนิดละ 1 รายการ เฉพาะชนิดที่จะออก)
- type: mcq (ปรนัยเลือกตอบ 4 ตัวเลือก), true_false (ถูก/ผิด), short_answer (อัตนัยตอบสั้น), essay (อัตนัยเรียงความ/อธิบายยาว)
- ผู้ใช้บอกแค่ "ปรนัย" = mcq; บอกแค่ "อัตนัย" = short_answer ถ้าคำถามตอบได้ในไม่กี่ประโยค หรือ essay ถ้าต้องอธิบาย วิเคราะห์ หรือแสดงความเห็นพร้อมเหตุผล
- count: จำนวนข้อตามที่ผู้ใช้ขอ ถ้าไม่ระบุอะไรเลย ใช้ mcq 5 ข้อ + short_answer 2 ข้อ
- points_each: คะแนนต่อข้อตามที่ผู้ใช้ขอ ถ้าผู้ใช้บอกคะแนนรวมของตอน ให้หารเฉลี่ยต่อข้อ ถ้าไม่ระบุใช้ mcq/true_false 1, short_answer 3, essay 10
- difficulty: easy / medium / hard ตามที่ขอ ถ้าขอหลายระดับ เช่น "กลาง-ยาก" ใช้ mixed ถ้าไม่ระบุใช้ medium

## ส่วนอื่น
- focus: แนวข้อสอบที่ผู้ใช้ต้องการ เช่น เน้นวิเคราะห์สถานการณ์ เน้นการคำนวณ เน้นความจำ และภาษาที่ใช้ถ้าผู้ใช้ขอ เขียน 1–2 ประโยค ถ้าไม่ระบุให้เขียนว่า "ครอบคลุมทั้งความจำ ความเข้าใจ และการนำไปใช้"
- concepts: หัวข้อ/แนวคิดสำคัญ 3–8 หัวข้อจากเนื้อหาที่ควรออกสอบ ชื่อสั้นและไม่ซ้ำกัน เป็นภาษาไทย (ศัพท์เทคนิคใส่วงเล็บภาษาอังกฤษได้)
  ถ้าผู้ใช้ขอเน้นบางหัวข้อ ให้มีเฉพาะหัวข้อเหล่านั้น ถ้ามีแค่ <topic> ให้แตกเป็นหัวข้อย่อยมาตรฐานของเรื่องนั้น
- title: ชื่อชุดข้อสอบสั้น ๆ ภาษาไทย
- assumptions: ทุกอย่างที่ผู้ใช้ไม่ได้ระบุแล้วคุณใช้ค่า default หรือตีความเอง ข้อละประโยคสั้น ๆ เช่น "ไม่ได้ระบุความยาก จึงใช้ระดับกลาง" ถ้าผู้ใช้ระบุครบให้เป็น []
- ถ้าคำสั่งไม่เกี่ยวกับการออกข้อสอบ ให้ใช้ค่า default ทั้งหมดและบันทึกไว้ใน assumptions"""


def normalize_spec(spec: ExamSpec) -> ExamSpec:
    """รวมชนิดที่ซ้ำ ตัดรายการที่ไม่มีข้อ เติมคะแนนที่ขาด และเรียงปรนัยก่อนอัตนัย"""
    merged: dict[str, SectionSpec] = {}
    for s in spec.sections:
        if s.count <= 0:
            continue
        if s.type in merged:
            merged[s.type] = merged[s.type].model_copy(update={"count": merged[s.type].count + s.count})
        else:
            points = s.points_each if s.points_each > 0 else DEFAULT_POINTS[s.type]
            merged[s.type] = s.model_copy(update={"points_each": points})
    return spec.model_copy(update={
        "title": spec.title.strip() or "ข้อสอบ",
        "sections": [merged[t] for t in TYPE_LABELS if t in merged],
        "concepts": list(dict.fromkeys(c.strip() for c in spec.concepts if c.strip())),
        "focus": spec.focus.strip(),
    })


def spec_problems(spec: ExamSpec) -> list[str]:
    """ข้อผิดพลาดที่ต้องแก้ก่อนสร้างข้อสอบ (ว่าง = ใช้ได้)"""
    problems = []
    total = sum(s.count for s in spec.sections)
    if total == 0:
        problems.append("ต้องมีข้อสอบอย่างน้อย 1 ข้อ")
    elif total > MAX_QUESTIONS:
        problems.append(f"รวม {total} ข้อ เกินที่ระบบรองรับต่อชุด ({MAX_QUESTIONS} ข้อ) — ลดจำนวนข้อหรือแบ่งเป็นหลายชุด")
    if not spec.concepts:
        problems.append("ต้องมีหัวข้อที่จะออกสอบอย่างน้อย 1 หัวข้อ")
    return problems


def describe_spec(spec: ExamSpec) -> str:
    parts = [
        f"{TYPE_LABELS[s.type]} {s.count} ข้อ × {s.points_each:g} คะแนน ({DIFFICULTY_LABELS[s.difficulty]})"
        for s in spec.sections
    ]
    total = sum(s.count * s.points_each for s in spec.sections)
    text = " · ".join(parts) + f" · รวม {total:g} คะแนน"
    return f"{text}\n\nแนวข้อสอบ: {spec.focus}" if spec.focus else text


def plan_exam(source: Source, user_prompt: str) -> ExamSpec:
    request = user_prompt.strip() or "ออกข้อสอบตามค่า default"
    content = source.content_blocks() + [
        {"type": "text", "text": f"<user_request>\n{request}\n</user_request>"},
    ]
    spec = call_structured(system=SYSTEM_PROMPT, content=content, schema=ExamSpec, effort=EFFORT["plan"])
    return normalize_spec(spec)
