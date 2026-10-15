"""Quality Reviewer: ตรวจคุณภาพข้อสอบก่อนนำไปใช้

- ข้อปรนัย: ผู้ตรวจ "ทำข้อสอบเอง" โดยไม่เห็นเฉลย แล้วโค้ดเทียบกับเฉลย ถ้าไม่ตรง = เฉลยอาจผิดหรือถูกได้หลายข้อ
- ทุกข้อ: หาปัญหาร้ายแรง (เฉลยผิด กำกวม ไม่ตรงเนื้อหา rubric ใช้ตรวจไม่ได้)
- โค้ดตรวจโครงสร้าง (จำนวนตัวเลือก เฉลยอยู่ในช่วง มี key points / rubric) โดยไม่ต้องใช้ LLM
ข้อที่ไม่ผ่านจะถูกส่งกลับให้ Question Generator แก้
"""

from dataclasses import dataclass

from . import scoring
from .answer_reader import parse_choice
from .config import EFFORT
from .llm import call_structured
from .schemas import OBJECTIVE_TYPES, Exam, ExamReview, Question
from .sources import Source

SYSTEM_PROMPT = """คุณคือ Quality Reviewer ผู้ตรวจคุณภาพข้อสอบที่เข้มงวดแต่ยุติธรรม ตรวจข้อสอบใน <questions> เทียบกับเนื้อหาที่ได้รับ

## ข้อปรนัย (mcq, true_false) — คุณจะไม่เห็นเฉลย
- ทำข้อสอบเองก่อนจากเนื้อหา: answer = ตัวอักษรของตัวเลือกที่ถูกที่สุด (ก ข ค ง) หรือ "ถูก"/"ผิด" และ answer_text = ข้อความของตัวเลือกนั้น (เฉพาะ mcq)
- ถ้ามีตัวเลือกที่ถูกได้มากกว่าหนึ่งข้อ หรือไม่มีข้อใดถูก ให้ระบุใน problems

## ข้ออัตนัย (short_answer, essay) — คุณจะเห็นคำตอบตัวอย่างและเกณฑ์
- ตรวจว่าคำตอบตัวอย่างถูกต้องตามเนื้อหา และเกณฑ์/ประเด็นใช้ตรวจคำตอบได้จริง answer และ answer_text เป็น ""

## problems: ใส่เฉพาะปัญหาร้ายแรงที่ทำให้ข้อสอบวัดผลไม่ได้หรือไม่ยุติธรรม ได้แก่
1. เฉลยหรือคำตอบตัวอย่างผิด หรือขัดกับเนื้อหา
2. ปรนัยมีตัวเลือกที่ถูกได้มากกว่าหนึ่งข้อ หรือไม่มีข้อใดถูก
3. คำถามกำกวม ขาดข้อมูลที่จำเป็น หรือตีความได้หลายแบบ
4. ถามเรื่องที่ไม่มีในเนื้อหา (กรณีมี <course_material>) หรือข้อเท็จจริงผิด (กรณีมีแค่ <topic>)
5. คำถามใบ้คำตอบ หรือตัวลวงผิดชัดเจนจนไม่มีใครเลือก
6. อัตนัย: ประเด็นหรือเกณฑ์ไม่ตรงกับสิ่งที่คำถามถาม หรือกว้างจนใช้ตรวจไม่ได้
ห้ามใส่เรื่องสำนวน ความยาว หรือข้อเสนอแนะเล็กน้อย ถ้าไม่มีปัญหาร้ายแรง problems ต้องเป็น []
เขียนแต่ละปัญหาเป็นประโยคสั้นที่ชี้จุดผิดชัดเจน และ suggestion บอกวิธีแก้ที่เจาะจง (ถ้าไม่มีปัญหาให้เป็น "")

ต้องมีผลตรวจครบทุกข้อใน <questions> ข้อละ 1 รายการ โดยใช้ question_id ตาม id ที่ได้รับ"""


@dataclass
class ReviewOutcome:
    issues: dict[int, list[str]]  # เฉพาะข้อที่ได้รับการตรวจ: [] = ผ่าน
    suggestions: dict[int, str]


def structural_issues(q: Question) -> list[str]:
    issues = []
    if not q.question.strip():
        issues.append("ไม่มีตัวคำถาม")
    if q.type == "mcq":
        if len(q.choices) != 4:
            issues.append(f"ต้องมี 4 ตัวเลือก แต่มี {len(q.choices)} ตัวเลือก")
        elif len({c.strip() for c in q.choices}) < len(q.choices):
            issues.append("มีตัวเลือกที่ซ้ำกัน")
    if q.type in OBJECTIVE_TYPES and (q.correct_choice is None or not 0 <= q.correct_choice < len(q.choices)):
        issues.append("ไม่มีเฉลยที่ใช้ได้ (correct_choice ไม่ตรงกับตัวเลือกใด)")
    if q.type == "short_answer" and not q.key_points:
        issues.append("ไม่มีประเด็นที่คำตอบต้องมี (key points) สำหรับใช้ตรวจ")
    if q.type == "essay" and not q.rubric:
        issues.append("ไม่มีเกณฑ์ให้คะแนน (rubric)")
    return issues


def _question_block(q: Question) -> str:
    lines = [f'<question id="{q.id}" type="{q.type}" concept="{q.concept}" source_ref="{q.source_ref}">', q.question]
    if q.type == "mcq":
        lines.extend(scoring.choice_label(q, i) for i in range(len(q.choices)))
    elif q.type == "true_false":
        lines.append("ถูก / ผิด")
    else:
        lines.append(f"<model_answer>{q.model_answer}</model_answer>")
        if q.key_points:
            lines.append("<key_points>\n" + "\n".join(f"- {kp}" for kp in q.key_points) + "\n</key_points>")
        if q.rubric:
            lines.append("<rubric>\n" + "\n".join(
                f'<criterion max_points="{c.max_points:g}">{c.name}: {c.description}</criterion>' for c in q.rubric
            ) + "\n</rubric>")
    lines.append("</question>")
    return "\n".join(lines)


def blind_check(q: Question, answer: str, answer_text: str) -> list[str]:
    """เทียบคำตอบที่ผู้ตรวจทำเองกับเฉลย"""
    if q.type not in OBJECTIVE_TYPES:
        return []
    solved = parse_choice(q, answer, answer_text)
    if solved is None or solved == q.correct_choice:
        return []
    return [
        f"ผู้ตรวจทำข้อนี้เองโดยไม่เห็นเฉลย ได้คำตอบ “{scoring.choice_label(q, solved)}” "
        f"แต่เฉลยคือ “{scoring.choice_label(q, q.correct_choice)}” — เฉลยอาจผิด หรือมีตัวเลือกที่ถูกได้มากกว่าหนึ่งข้อ"
    ]


def review_questions(source: Source, exam: Exam, ids: list[int]) -> ReviewOutcome:
    targets = [q for q in exam.questions if q.id in ids]
    blocks = "\n\n".join(_question_block(q) for q in targets)
    content = source.content_blocks() + [{"type": "text", "text": f"<questions>\n{blocks}\n</questions>"}]
    review = call_structured(system=SYSTEM_PROMPT, content=content, schema=ExamReview, effort=EFFORT["review"])

    by_id = {}
    for r in review.reviews:
        by_id.setdefault(r.question_id, r)
    issues: dict[int, list[str]] = {}
    suggestions: dict[int, str] = {}
    for q in targets:
        found = structural_issues(q)
        r = by_id.get(q.id)
        if r is None and not found:
            continue  # ผู้ตรวจข้ามข้อนี้ → ถือว่ายังไม่ได้ตรวจ
        if r is not None:
            found += [p.strip() for p in r.problems if p.strip()]
            found += blind_check(q, r.answer, r.answer_text)
            suggestions[q.id] = r.suggestion.strip()
        issues[q.id] = found
    return ReviewOutcome(issues=issues, suggestions=suggestions)
