"""ข้อมูลการสอบทั้งห้อง: รวมรายชื่อที่นำเข้า (CSV / กระดาษคำตอบที่ AI อ่าน) ตรวจทีละคน สรุปคะแนน
และบันทึก/เปิดเป็นไฟล์ JSON

ข้อมูลนักเรียนเก็บในหน่วยความจำของแอปและไฟล์ที่ผู้ใช้ดาวน์โหลดเท่านั้น (ไม่บันทึกลงดิสก์เอง)
คำตอบที่ AI อ่านจากกระดาษต้องให้ครูตรวจทาน (reviewed) ก่อนจึงจะตรวจคะแนนได้
"""

import csv
import io
from pathlib import Path

from pydantic import ValidationError

from . import answer_reader, scoring
from .grader import grade_exam
from .schemas import OBJECTIVE_TYPES, ClassSession, Exam, ExtractedAnswerSheet, StudentRecord


class ClassFileError(ValueError):
    """ไฟล์ข้อมูลห้องที่เปิดไม่ได้ ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


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
        notes.insert(0, "อ่านชื่อผู้สอบไม่ได้ — ใช้ชื่อไฟล์แทน กรุณาแก้ชื่อ")
    return StudentRecord(
        student_no=extracted.student_no.strip(), name=name, answers=answers, source="answer_sheet",
        import_notes=notes, reviewed=False, sheet_name=sheet_name, extracted=extracted,
    )


def read_sheet(exam: Exam, sheet: answer_reader.AnswerSheetFile) -> StudentRecord:
    """ให้ AI อ่านกระดาษคำตอบ 1 คน (เรียก LLM อาจโยน LLMError)"""
    student = student_from_sheet(exam, sheet.name, answer_reader.read_answers(exam, sheet))
    if sheet.truncated:
        note = f"กระดาษมีมากกว่า {answer_reader.MAX_PAGES} หน้า อ่านเฉพาะ {answer_reader.MAX_PAGES} หน้าแรก"
        student = student.model_copy(update={"import_notes": [note] + student.import_notes})
    return student


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

def needs_llm(exam: Exam) -> bool:
    """ข้อสอบมีอัตนัยไหม (ปรนัยล้วนตรวจด้วยโค้ดได้ ไม่ต้องใช้โมเดล)"""
    return any(q.type not in OBJECTIVE_TYPES for q in exam.questions)


def grade_student(exam: Exam, student: StudentRecord) -> StudentRecord:
    """ปรนัยตรวจด้วยโค้ด อัตนัยตรวจด้วย grader (เรียก LLM) — อาจโยน LLMError"""
    return student.model_copy(update={"results": grade_exam(exam, student.answers)})


def status(student: StudentRecord) -> str:
    if not student.reviewed:
        return "รอตรวจทาน"
    return "ยังไม่ได้ตรวจ" if student.results is None else "ตรวจแล้ว"


def score_rows(session: ClassSession) -> list[dict]:
    """ตารางคะแนนรายคน (ใช้แสดงในแอปและส่งออก CSV)"""
    rows = []
    for s in session.students:
        row: dict = {"เลขที่": s.student_no, "ชื่อ": s.name}
        if s.results is None:
            row.update({"คะแนนรวม": None, "คะแนนเต็ม": None, "ร้อยละ": None, "ปรนัย": None, "อัตนัย": None})
        else:
            t = scoring.totals(s.results)
            row.update({
                "คะแนนรวม": t["score"],
                "คะแนนเต็ม": t["max"],
                "ร้อยละ": round(100 * t["score"] / t["max"], 1) if t["max"] else 0.0,
                "ปรนัย": t["objective_score"],
                "อัตนัย": t["subjective_score"],
            })
            row.update({f"ข้อ {r.question_id}": r.points_awarded for r in s.results})
        row["สถานะ"] = status(s)
        rows.append(row)
    return rows


def scores_csv(session: ClassSession) -> bytes:
    """คะแนนรายคนสำหรับลงสมุดคะแนน (utf-8-sig เพื่อให้ Excel แสดงภาษาไทยถูก)"""
    columns = ["เลขที่", "ชื่อ", "คะแนนรวม", "คะแนนเต็ม", "ร้อยละ", "ปรนัย", "อัตนัย", "สถานะ"]
    columns += [f"ข้อ {q.id}" for q in session.exam.questions]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in score_rows(session):
        writer.writerow({k: ("" if v is None else (f"{v:g}" if isinstance(v, float) else v)) for k, v in row.items()})
    return buf.getvalue().encode("utf-8-sig")


# ---------------- บันทึก / เปิดไฟล์ ----------------

def to_json(session: ClassSession) -> bytes:
    return session.model_dump_json(indent=1).encode("utf-8")


def from_json(data: bytes) -> ClassSession:
    try:
        return ClassSession.model_validate_json(data)
    except ValidationError as e:
        raise ClassFileError("ไฟล์นี้ไม่ใช่ข้อมูลห้องที่บันทึกจากแอป หรือไฟล์เสียหาย") from e
