"""ข้อมูลการสอบทั้งห้อง: รวมคำตอบที่นำเข้า (CSV / กระดาษคำตอบที่ AI อ่าน) ใช้ชื่อจากรายชื่อห้อง
ตรวจทีละคน และสรุปคะแนน (บันทึกลงฐานข้อมูลด้วย src/school_db.py)

คำตอบที่ AI อ่านจากกระดาษต้องให้ครูตรวจทาน (reviewed) ก่อนจึงจะตรวจคะแนนได้
"""

import csv
import io
from difflib import SequenceMatcher
from pathlib import Path

from . import answer_reader, exam_forms, scoring
from .grading_graph import grade_exam
from .schemas import OBJECTIVE_TYPES, ClassSession, Exam, ExamForm, ExtractedAnswerSheet, StudentRecord

NO_NAME_NOTE = "อ่านชื่อผู้สอบไม่ได้ — ใช้ชื่อไฟล์แทน กรุณาแก้ชื่อ"
NAME_MATCH = 0.6  # ชื่อที่ AI อ่านได้คล้ายชื่อในรายชื่อห้องน้อยกว่านี้ = เลขที่อาจอ่านผิด
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def normalize_no(raw: str) -> str:
    """เลขที่ให้อยู่รูปเดียวกัน: เลขไทยเป็นเลขอารบิก ตัดศูนย์นำหน้า ("๐๗" → "7")"""
    number = raw.strip().translate(THAI_DIGITS)
    return str(int(number)) if number.isascii() and number.isdigit() else number


def new_session(exam: Exam) -> ClassSession:
    return ClassSession(exam=exam)


# ---------------- รับคำตอบเข้า ----------------

def student_from_sheet(exam: Exam, sheet_name: str, extracted: ExtractedAnswerSheet) -> StudentRecord:
    """แปลงสิ่งที่ AI อ่านจากกระดาษเป็นคำตอบรายข้อ พร้อมคำเตือนให้ครูตรวจทาน"""
    by_id = {a.question_id: a for a in extracted.answers}
    answers = {}
    notes = []
    for q in exam.questions:
        answers[q.id] = answer_reader.to_form_value(q, by_id.get(q.id))
        if warning := answer_reader.review_warning(q, by_id.get(q.id)):
            notes.append(f"ข้อ {q.id}: {warning}")
    name = extracted.student_name.strip()
    if not name:
        name = Path(sheet_name).stem
        notes.insert(0, NO_NAME_NOTE)
    return StudentRecord(
        student_no=normalize_no(extracted.student_no), name=name, answers=answers, source="answer_sheet",
        import_notes=notes, reviewed=False, sheet_name=sheet_name, extracted=extracted,
    )


def read_sheet(exam: Exam, sheet: answer_reader.AnswerSheetFile, forms: list[ExamForm] = (),
               form_code: str = "") -> StudentRecord:
    """ให้ AI อ่านกระดาษคำตอบ 1 คน (เรียก LLM อาจโยน LLMError)

    มีชุดสลับ: อ่านด้วยชุดที่ครูเลือก (form_code ว่าง = ไม่ทราบ เริ่มที่ชุดแรก) ถ้ารหัสชุดบนหัวกระดาษเป็นชุดอื่น
    อ่านใหม่ด้วยชุดนั้น แล้วแปลงคำตอบกลับเป็นข้อสอบต้นฉบับ"""
    if not forms:
        student = student_from_sheet(exam, sheet.name, answer_reader.read_answers(exam, sheet))
    else:
        form = exam_forms.find(forms, form_code) or forms[0]
        extracted = answer_reader.read_answers(exam_forms.form_exam(exam, form), sheet)
        printed = exam_forms.find(forms, extracted.form_code)
        notes = []
        if printed and printed.code != form.code:  # กระดาษเป็นชุดอื่น อ่านใหม่ด้วยชุดที่พิมพ์จริง
            form = printed
            extracted = answer_reader.read_answers(exam_forms.form_exam(exam, form), sheet)
        elif printed is None:
            notes.append(f"อ่านรหัสชุดบนหัวกระดาษไม่ได้ — ตรวจคำตอบตามชุด {form.code} ตรวจดูว่าถูกชุดไหม")
        student = student_from_sheet(exam_forms.form_exam(exam, form), sheet.name, extracted)
        student = student.model_copy(update={
            "answers": exam_forms.to_base(form, student.answers), "form": form.code,
            "import_notes": notes + student.import_notes,
        })
    if sheet.truncated:
        note = f"กระดาษมีมากกว่า {answer_reader.MAX_PAGES} หน้า อ่านเฉพาะ {answer_reader.MAX_PAGES} หน้าแรก"
        student = student.model_copy(update={"import_notes": [note] + student.import_notes})
    return student


def apply_roster(students: list[StudentRecord], roster: dict[str, str]) -> list[StudentRecord]:
    """ใช้ชื่อจากรายชื่อห้องตามเลขที่ (AI อ่านชื่อลายมือภาษาไทยผิดบ่อยกว่าเลขที่)
    และเตือนเมื่อเลขที่ไม่อยู่ในรายชื่อ หรือชื่อในกระดาษไม่ตรงกับเลขที่นั้น · roster = {เลขที่: ชื่อ}"""
    if not roster:
        return students
    result = []
    for s in students:
        notes = list(s.import_notes)
        name = s.name
        if s.student_no in roster:
            name = roster[s.student_no]
            notes = [n for n in notes if n != NO_NAME_NOTE]
            written = s.extracted.student_name.strip() if s.extracted else ""
            if written and _similarity(written, name) < NAME_MATCH:
                notes.insert(0, f"ชื่อในกระดาษ “{written}” ไม่ตรงกับเลขที่ {s.student_no} ในรายชื่อห้อง ({name}) "
                                "— ตรวจเลขที่อีกครั้ง")
        elif s.student_no:
            notes.insert(0, f"เลขที่ {s.student_no} ไม่อยู่ในรายชื่อห้อง")
        else:
            notes.insert(0, "ไม่มีเลขที่ — เลือกผู้สอบจากรายชื่อห้อง")
        result.append(s.model_copy(update={"name": name, "import_notes": notes}))
    return result


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, "".join(a.split()), "".join(b.split())).ratio()


def absent(session: ClassSession, roster: dict[str, str]) -> list[str]:
    """เลขที่ในรายชื่อห้องที่ยังไม่มีคำตอบ"""
    submitted = {s.student_no for s in session.students}
    return [number for number in roster if number not in submitted]


def dedupe(students: list[StudentRecord]) -> list[StudentRecord]:
    """กระดาษหลายใบที่อ่านได้ชื่อ/เลขที่เดียวกัน (มักเป็นเพราะอ่านผิด) ไม่ให้ทับกัน"""
    seen: set[str] = set()
    result = []
    for s in students:
        if s.key in seen:
            s = s.model_copy(update={
                "name": f"{s.name} ({s.sheet_name})",
                "student_no": "",
                "import_notes": ["ชื่อหรือเลขที่ซ้ำกับกระดาษใบอื่นในชุดเดียวกัน — ตรวจทานชื่อและเลขที่"] + s.import_notes,
            })
        seen.add(s.key)
        result.append(s)
    return result


def merge_students(session: ClassSession, incoming: list[StudentRecord]) -> tuple[ClassSession, int, int]:
    """เพิ่มผู้สอบใหม่ หรือแทนที่คำตอบของคนเดิม (เลขที่/ชื่อเดียวกัน ผลตรวจเดิมถูกล้าง) คืน (ห้อง, เพิ่ม, อัปเดต)"""
    students = list(session.students)
    index = {s.key: i for i, s in enumerate(students)}
    added = updated = 0
    for student in incoming:
        student = student.model_copy(update={"results": None})
        if student.key in index:
            students[index[student.key]] = student
            updated += 1
        else:
            index[student.key] = len(students)
            students.append(student)
            added += 1
    return session.model_copy(update={"students": students}), added, updated


def update_student(session: ClassSession, index: int, student: StudentRecord) -> ClassSession:
    students = list(session.students)
    students[index] = student
    return session.model_copy(update={"students": students})


def awaiting_review(session: ClassSession) -> list[int]:
    return [i for i, s in enumerate(session.students) if not s.reviewed]


def ready_to_grade(session: ClassSession) -> list[int]:
    return [i for i, s in enumerate(session.students) if s.reviewed and s.results is None]


# ---------------- ตรวจและสรุปคะแนน ----------------

def needs_llm(exam: Exam, students: list[StudentRecord] | None = None) -> bool:
    """ต้องใช้โมเดลไหม: ปรนัย ข้อที่ไม่ได้ตอบ และเรียงความที่ครูกรอกคะแนนมาแล้ว ตรวจด้วยโค้ดได้
    ไม่ระบุ students = ดูจากข้อสอบอย่างเดียว (มีอัตนัยเมื่อไหร่ถือว่าต้องใช้)"""
    subjective = [q.id for q in exam.questions if q.type not in OBJECTIVE_TYPES]
    if students is None:
        return bool(subjective)
    return any(
        qid not in s.teacher_scores and str(s.answers.get(qid) or "").strip()
        for s in students for qid in subjective
    )


def grade_student(
    exam: Exam, student: StudentRecord, *, guidance: dict[int, tuple[str, ...]] | None = None
) -> StudentRecord:
    """ปรนัยตรวจด้วยโค้ด อัตนัยตรวจด้วย grader (เรียก LLM) ยกเว้นเรียงความที่ครูกรอกคะแนนมา — อาจโยน LLMError"""
    results = grade_exam(exam, student.answers, guidance=guidance, review_with_teacher=True,
                         teacher_scores=student.teacher_scores)
    return student.model_copy(update={"results": results})


def status(student: StudentRecord) -> str:
    if not student.reviewed:
        return "รอตรวจทาน"
    if student.results is None:
        return "ยังไม่ได้ตรวจ"
    return "รอครูตัดสินคะแนน" if any(r.needs_teacher_review for r in student.results) else "ตรวจแล้ว"


def score_rows(session: ClassSession) -> list[dict]:
    """ตารางคะแนนรายคน (ใช้แสดงในแอปและส่งออก CSV)"""
    rows = []
    with_forms = any(s.form for s in session.students)
    for s in session.students:
        row: dict = {"เลขที่": s.student_no, "ชื่อ": s.name}
        if with_forms:
            row["ชุด"] = s.form
        if s.results is None:
            row.update({"คะแนนรวม": None, "คะแนนเต็ม": None, "ร้อยละ": None, "ปรนัย": None, "อัตนัย": None,
                        "ให้ครูดู": None})
        else:
            t = scoring.totals(s.results)
            row.update({
                "คะแนนรวม": t["score"],
                "คะแนนเต็ม": t["max"],
                "ร้อยละ": round(100 * t["score"] / t["max"], 1) if t["max"] else 0.0,
                "ปรนัย": t["objective_score"],
                "อัตนัย": t["subjective_score"],
                "ให้ครูดู": sum(1 for r in s.results if r.needs_teacher_review),  # ข้อที่คณะผู้ตรวจเห็นต่างมาก
            })
            row.update({f"ข้อ {r.question_id}": r.points_awarded for r in s.results})
        row["สถานะ"] = status(s)
        rows.append(row)
    return rows


def scores_csv(session: ClassSession) -> bytes:
    """คะแนนรายคนสำหรับลงสมุดคะแนน"""
    columns = ["เลขที่", "ชื่อ"] + (["ชุด"] if any(s.form for s in session.students) else [])
    columns += ["คะแนนรวม", "คะแนนเต็ม", "ร้อยละ", "ปรนัย", "อัตนัย", "สถานะ"]
    columns += [f"ข้อ {q.id}" for q in session.exam.questions]  # เลขข้อของข้อสอบต้นฉบับ (ชุด ก)
    return table_csv(score_rows(session), columns)


def table_csv(rows: list[dict], columns: list[str] | None = None) -> bytes:
    """ตารางเป็น CSV (utf-8-sig เพื่อให้ Excel แสดงภาษาไทยถูก) ช่องว่าง = ''"""
    columns = columns or (list(rows[0]) if rows else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else (f"{v:g}" if isinstance(v, float) else v)) for k, v in row.items()})
    return buf.getvalue().encode("utf-8-sig")

