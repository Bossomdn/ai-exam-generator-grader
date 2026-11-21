"""ข้อสอบชุดสลับกันลอก (ชุด ก ข ค ง): ข้อเดิมทั้งหมด สลับลำดับข้อและตัวเลือก (ไม่ใช้ LLM)

- ชุด ก = ลำดับต้นฉบับ ชุดอื่นสลับลำดับข้อภายในตอน (ปรนัยก่อนอัตนัยเหมือนเดิม) และสลับตัวเลือกปรนัย
  ข้อที่ตัวเลือกอ้างถึงกันเอง (เช่น "ถูกทุกข้อ", "ก และ ข") และข้อถูก/ผิด ไม่สลับตัวเลือก
- พิมพ์และอ่านกระดาษด้วยข้อสอบตามชุด (form_exam) แต่คำตอบถูกแปลงกลับเป็นข้อสอบต้นฉบับทันที (to_base)
  การตรวจ คะแนน และการวิเคราะห์ข้อสอบจึงใช้ข้อสอบต้นฉบับชุดเดียว รวมผลทุกชุดได้ตรง ๆ
"""

import random
import re
from typing import Optional, Union

from .schemas import OBJECTIVE_TYPES, Exam, ExamForm, Question

FORM_CODES = "กขคง"
MAX_FORMS = len(FORM_CODES)
# ตัวเลือกที่อ้างถึงตัวเลือกอื่นหรือตำแหน่ง สลับแล้วความหมายเปลี่ยน
_REFERS_TO_OTHERS = re.compile(
    r"ทุกข้อ|ทุกตัวเลือก|ไม่มีข้อ|ข้างต้น|ทั้ง\s*[กขคงจ]|[กขคงจ]\s*(และ|หรือ)\s*[กขคงจ]"
    r"|all of the above|none of the above|both [a-e]\b|[a-e] and [a-e]\b",
    re.IGNORECASE,
)

Answer = Union[int, str, None]


def _keeps_choice_order(q: Question) -> bool:
    return q.type != "mcq" or any(_REFERS_TO_OTHERS.search(c) for c in q.choices)


def _shuffled(items: list, rng: random.Random) -> list:
    """สลับให้ต่างจากเดิมเมื่อทำได้ (มีมากกว่า 1 ตัว)"""
    result = items[:]
    for _ in range(10):
        rng.shuffle(result)
        if result != items or len(items) < 2:
            break
    return result


def make_forms(exam: Exam, count: int, seed: int) -> list[ExamForm]:
    """ชุดสลับ count ชุด (1 = ไม่สลับ) seed เดิมได้ชุดเดิมเสมอ (ใช้ id ของการสอบ)"""
    count = max(1, min(count, MAX_FORMS))
    objective = [q.id for q in exam.questions if q.type in OBJECTIVE_TYPES]
    subjective = [q.id for q in exam.questions if q.type not in OBJECTIVE_TYPES]
    forms = [ExamForm(code=FORM_CODES[0], order=objective + subjective)]
    for code in FORM_CODES[1:count]:
        rng = random.Random(f"{seed}-{code}")
        choices = {
            q.id: _shuffled(list(range(len(q.choices))), rng)
            for q in exam.questions if not _keeps_choice_order(q)
        }
        forms.append(ExamForm(code=code, order=_shuffled(objective, rng) + _shuffled(subjective, rng),
                              choices=choices))
    return forms


def find(forms: list[ExamForm], code: str) -> Optional[ExamForm]:
    code = code.strip().replace("ชุด", "").strip()
    return next((f for f in forms if f.code == code), None)


def form_exam(exam: Exam, form: ExamForm) -> Exam:
    """ข้อสอบตามที่พิมพ์ในชุดนี้: เลขข้อ 1..n ตามลำดับของชุด ตัวเลือกสลับตามชุด (เฉลยย้ายตาม)"""
    by_id = {q.id: q for q in exam.questions}
    questions = []
    for printed, base_id in enumerate(form.order, start=1):
        q = by_id[base_id]
        update: dict = {"id": printed}
        if perm := form.choices.get(base_id):
            update["choices"] = [q.choices[i] for i in perm]
            update["correct_choice"] = perm.index(q.correct_choice)
        questions.append(q.model_copy(update=update))
    return exam.model_copy(update={"questions": questions})


def to_base(form: ExamForm, answers: dict[int, Answer]) -> dict[int, Answer]:
    """คำตอบตามชุด (เลขข้อและตัวเลือกที่พิมพ์) → คำตอบของข้อสอบต้นฉบับ"""
    result: dict[int, Answer] = {}
    for printed, value in answers.items():
        if not 1 <= printed <= len(form.order):
            continue
        base_id = form.order[printed - 1]
        perm = form.choices.get(base_id)
        if perm and isinstance(value, int) and 0 <= value < len(perm):
            value = perm[value]
        result[base_id] = value
    return result


def to_form(form: ExamForm, answers: dict[int, Answer]) -> dict[int, Answer]:
    """คำตอบของข้อสอบต้นฉบับ → ตามเลขข้อและตัวเลือกที่พิมพ์ในชุดนี้ (ใช้ตอนครูตรวจทานเทียบกับกระดาษ)"""
    result: dict[int, Answer] = {}
    for printed, base_id in enumerate(form.order, start=1):
        value = answers.get(base_id)
        perm = form.choices.get(base_id)
        if perm and isinstance(value, int) and value in perm:
            value = perm.index(value)
        result[printed] = value
    return result
