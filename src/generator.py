"""Question Generator: ออกข้อสอบพร้อมเฉลยและ rubric ตามสเปก และแก้ข้อที่ Quality Reviewer ส่งกลับมา"""

import json

from .config import EFFORT
from .llm import call_structured
from .planner import describe_spec
from .schemas import TYPE_LABELS, Exam, ExamSpec, Question, RevisedQuestions
from .sources import Source

RULES = """## ความถูกต้องและที่มา
- ถ้ามี <course_material> หรือเอกสารแนบ: ออกข้อสอบจากเนื้อหานั้นเท่านั้น และเฉลยต้องตรงกับเนื้อหา
  source_ref ให้ระบุตำแหน่ง เช่น "สไลด์ 3" (ใช้เลขจากเครื่องหมาย [สไลด์ n]) หรือ "หน้า 2"
- ถ้ามีแค่ <topic>: ออกข้อสอบจากความรู้มาตรฐานของหัวข้อนั้น ใช้เฉพาะข้อเท็จจริงที่เป็นที่ยอมรับทั่วไป และให้ source_ref เป็น "ความรู้ทั่วไป: <หัวข้อย่อย>"
- concept ของแต่ละข้อต้องสะกดตรงกับรายการ concepts ในสเปกทุกตัวอักษร
- ใช้ภาษาไทย เว้นแต่สเปกขอภาษาอื่น (ศัพท์เทคนิคเขียนทับศัพท์หรือวงเล็บภาษาอังกฤษได้)

## กติกาแต่ละชนิด
- mcq: choices มี 4 ตัวเลือกพอดี (ไม่ต้องใส่ ก. ข. นำหน้า) มีคำตอบถูกเพียงข้อเดียว ตัวลวงต้องฟังดูเป็นไปได้และสะท้อนความเข้าใจผิดที่พบบ่อย ห้ามใช้ "ถูกทุกข้อ" หรือ "ไม่มีข้อใดถูก" explanation อธิบายว่าทำไมข้อที่ถูกจึงถูก และแต่ละตัวลวงผิดเพราะอะไร โดยอ้างถึงตัวเลือกด้วยเนื้อความ ไม่ใช่หมายเลขหรือตัวอักษร
- true_false: choices เป็น ["ถูก", "ผิด"] correct_choice เป็น 0 (ถูก) หรือ 1 (ผิด) ข้อความต้องชัดเจน ไม่กำกวม explanation อธิบายเหตุผล
- short_answer: key_points 2–4 ประเด็นที่ชัดเจนและตรวจได้ model_answer เป็นคำตอบตัวอย่างที่ได้คะแนนเต็ม correct_choice เป็น null choices และ rubric เป็น []
- essay: rubric 3–4 เกณฑ์ ผลรวม max_points เท่ากับ points ของข้อนั้นพอดี description ของแต่ละเกณฑ์บอกว่าคำตอบแบบไหนได้เต็ม ได้บางส่วน หรือไม่ได้คะแนน model_answer เป็นคำตอบตัวอย่าง correct_choice เป็น null choices และ key_points เป็น []
- ข้อปรนัยให้ model_answer เป็น "" key_points และ rubric เป็น []
- คำถามแต่ละข้อต้องอ่านแล้วเข้าใจได้ด้วยตัวเอง ไม่ถามซ้ำกัน และไม่ใบ้คำตอบของข้ออื่น"""

GENERATE_PROMPT = f"""คุณคือ Question Generator ผู้เชี่ยวชาญด้านการวัดและประเมินผลการเรียนรู้ หน้าที่ของคุณคือออกข้อสอบที่มีคุณภาพจากเนื้อหาที่ได้รับ ตาม <exam_spec>

## ทำตามสเปก
- จำนวนข้อของแต่ละชนิดต้องเท่ากับ count ในสเปกพอดี และ points ของแต่ละข้อเท่ากับ points_each ของชนิดนั้น
- ความยากตาม difficulty (mixed = คละง่าย กลาง ยาก) และทำตาม focus
- กระจายข้อสอบให้ครอบคลุมทุกหัวข้อใน concepts เท่าที่จำนวนข้อเอื้อ และใช้หลายระดับ Bloom ตามที่ focus เน้น
- สลับตำแหน่งคำตอบที่ถูกของ mcq ให้หลากหลาย
- title ใช้ตามสเปก concepts ใช้รายการเดียวกับสเปก spec_summary สรุปสั้น ๆ ว่าข้อสอบชุดนี้มีอะไรบ้าง
- ให้ id เรียงจาก 1 ตามลำดับ เรียงข้อตามลำดับ sections ในสเปก

{RULES}"""

REVISE_PROMPT = f"""คุณคือ Question Generator หน้าที่รอบนี้คือแก้ข้อสอบที่ Quality Reviewer พบปัญหา

- <questions_to_fix> คือข้อที่ต้องแก้ แต่ละข้อมี <problems> ที่ผู้ตรวจพบ และ <suggestion> วิธีแก้ที่ผู้ตรวจเสนอ
- แก้ให้ปัญหาที่ระบุหมดไป จะปรับคำถาม ตัวเลือก เฉลย หรือเขียนข้อใหม่ทั้งข้อก็ได้ ตรวจเฉลยกับเนื้อหาให้แน่ใจก่อนตอบ
- ต้องคง id, type, points และ concept เดิม และต้องไม่ซ้ำกับข้ออื่นใน <other_questions>
- ส่งกลับเฉพาะข้อที่แก้ ครบทุกข้อใน <questions_to_fix>

{RULES}"""


def _clean(exam: Exam) -> Exam:
    """แก้ปัญหาเล็ก ๆ ที่โมเดลอาจพลาด เพื่อให้ตรวจด้วยโค้ดได้แน่นอน"""
    questions = []
    for number, q in enumerate(exam.questions, start=1):
        update: dict = {"id": number}
        if q.type == "true_false":
            update["choices"] = ["ถูก", "ผิด"]
        if q.type in ("short_answer", "essay"):
            update["correct_choice"] = None
            update["choices"] = []
        if q.type == "essay" and q.rubric:
            rubric_total = sum(c.max_points for c in q.rubric)
            if rubric_total and abs(rubric_total - q.points) > 1e-6:
                scale = q.points / rubric_total
                update["rubric"] = [
                    c.model_copy(update={"max_points": round(c.max_points * scale, 2)}) for c in q.rubric
                ]
        questions.append(q.model_copy(update=update))
    concepts = list(dict.fromkeys(exam.concepts + [q.concept for q in questions]))
    return exam.model_copy(update={"questions": questions, "concepts": concepts})


def fit_to_spec(exam: Exam, spec: ExamSpec) -> tuple[Exam, dict[str, int]]:
    """บังคับให้ตรงสเปกด้วยโค้ด: ตัดข้อที่เกิน เรียงตามสเปก ตั้งคะแนนต่อข้อ
    คืน (ข้อสอบ, จำนวนข้อที่ยังขาดของแต่ละชนิด)"""
    questions: list[Question] = []
    missing: dict[str, int] = {}
    for section in spec.sections:
        of_type = [q for q in exam.questions if q.type == section.type][: section.count]
        questions.extend(q.model_copy(update={"points": section.points_each}) for q in of_type)
        if len(of_type) < section.count:
            missing[section.type] = section.count - len(of_type)
    fitted = exam.model_copy(update={
        "title": spec.title,
        "spec_summary": describe_spec(spec),
        "concepts": list(spec.concepts),
        "questions": questions,
    })
    return _clean(fitted), missing


def describe_missing(missing: dict[str, int]) -> str:
    return ", ".join(f"{TYPE_LABELS[t]} {n} ข้อ" for t, n in missing.items())


def _spec_block(spec: ExamSpec) -> dict:
    data = spec.model_dump(exclude={"assumptions"})
    return {"type": "text", "text": f"<exam_spec>\n{json.dumps(data, ensure_ascii=False, indent=1)}\n</exam_spec>"}


def _generate_once(source: Source, spec: ExamSpec, feedback: str = "") -> Exam:
    content = source.content_blocks() + [_spec_block(spec)]
    if feedback:
        content.append({"type": "text", "text": f"<feedback>\n{feedback}\n</feedback>"})
    return call_structured(system=GENERATE_PROMPT, content=content, schema=Exam, effort=EFFORT["generate"])


def generate_from_spec(source: Source, spec: ExamSpec) -> tuple[Exam, dict[str, int]]:
    """ออกข้อสอบตามสเปก ถ้าได้ไม่ครบจะลองใหม่ 1 ครั้งพร้อมบอกว่าขาดอะไร"""
    exam, missing = fit_to_spec(_generate_once(source, spec), spec)
    if missing:
        feedback = f"รอบก่อนออกข้อสอบไม่ครบตามสเปก ขาด {describe_missing(missing)} ครั้งนี้ต้องออกให้ครบทุกชนิดตาม count"
        retry, retry_missing = fit_to_spec(_generate_once(source, spec, feedback), spec)
        if sum(retry_missing.values()) < sum(missing.values()):
            exam, missing = retry, retry_missing
    return exam, missing


def _questions_to_fix(exam: Exam, issues: dict[int, list[str]], suggestions: dict[int, str]) -> str:
    parts = []
    for q in exam.questions:
        if q.id not in issues:
            continue
        problems = "\n".join(f"- {p}" for p in issues[q.id])
        parts.append(
            f'<question_to_fix id="{q.id}">\n{q.model_dump_json(indent=1)}\n'
            f"<problems>\n{problems}\n</problems>\n"
            f"<suggestion>{suggestions.get(q.id, '')}</suggestion>\n</question_to_fix>"
        )
    return "\n".join(parts)


def apply_revisions(exam: Exam, revised: list[Question], ids: list[int]) -> tuple[Exam, list[int]]:
    """แทนที่ข้อเดิมด้วยข้อที่แก้แล้ว โดยคง id / ชนิด / คะแนนเดิม คืน (ข้อสอบ, id ที่ถูกแก้จริง)"""
    by_id = {q.id: q for q in revised if q.id in ids}
    if not by_id and len(revised) == len(ids):  # โมเดลเปลี่ยนเลขข้อ → จับคู่ตามลำดับ
        by_id = dict(zip(ids, revised))
    types = {q.id: q.type for q in exam.questions}
    by_id = {i: q for i, q in by_id.items() if q.type == types.get(i)}  # ห้ามเปลี่ยนชนิดข้อ
    questions = []
    for q in exam.questions:
        new = by_id.get(q.id)
        if new is None:
            questions.append(q)
            continue
        update = {"id": q.id, "points": q.points}
        if new.concept not in exam.concepts:
            update["concept"] = q.concept
        questions.append(new.model_copy(update=update))
    return _clean(exam.model_copy(update={"questions": questions})), sorted(by_id)


def revise_questions(
    source: Source, spec: ExamSpec, exam: Exam, issues: dict[int, list[str]], suggestions: dict[int, str]
) -> tuple[Exam, list[int]]:
    others = "\n".join(f"ข้อ {q.id}. {q.question}" for q in exam.questions if q.id not in issues)
    content = source.content_blocks() + [
        _spec_block(spec),
        {"type": "text", "text": f"<other_questions>\n{others}\n</other_questions>"},
        {"type": "text", "text": f"<questions_to_fix>\n{_questions_to_fix(exam, issues, suggestions)}\n</questions_to_fix>"},
    ]
    result = call_structured(system=REVISE_PROMPT, content=content, schema=RevisedQuestions, effort=EFFORT["generate"])
    return apply_revisions(exam, result.questions, sorted(issues))
