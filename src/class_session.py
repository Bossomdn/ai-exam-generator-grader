"""ข้อมูลการสอบทั้งห้อง: รวมรายชื่อที่นำเข้า ตรวจทีละคน สรุปคะแนน และบันทึก/เปิดเป็นไฟล์ JSON

ข้อมูลนักเรียนเก็บในหน่วยความจำของแอปและไฟล์ที่ผู้ใช้ดาวน์โหลดเท่านั้น (ไม่บันทึกลงดิสก์เอง)
"""

import csv
import io

from pydantic import ValidationError

from . import scoring
from .grader import grade_exam
from .schemas import OBJECTIVE_TYPES, ClassSession, Exam, StudentRecord


class ClassFileError(ValueError):
    """ไฟล์ข้อมูลห้องที่เปิดไม่ได้ ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def new_session(exam: Exam) -> ClassSession:
    return ClassSession(exam=exam)


def merge_students(session: ClassSession, incoming: list[StudentRecord]) -> tuple[ClassSession, int, int]:
    """เพิ่มผู้สอบใหม่ หรือแทนที่คำตอบของคนเดิม (ผลตรวจเดิมถูกล้าง) คืน (ห้อง, จำนวนที่เพิ่ม, จำนวนที่อัปเดต)"""
    students = list(session.students)
    index = {s.key: i for i, s in enumerate(students)}
    added = updated = 0
    for student in incoming:
        if student.key in index:
            students[index[student.key]] = student.model_copy(update={"results": None})
            updated += 1
        else:
            index[student.key] = len(students)
            students.append(student.model_copy(update={"results": None}))
            added += 1
    return session.model_copy(update={"students": students}), added, updated


def replace_student(session: ClassSession, student: StudentRecord) -> ClassSession:
    students = [student if s.key == student.key else s for s in session.students]
    return session.model_copy(update={"students": students})


def needs_llm(exam: Exam) -> bool:
    """ข้อสอบมีอัตนัยไหม (ปรนัยล้วนตรวจด้วยโค้ดได้ ไม่ต้องใช้โมเดล)"""
    return any(q.type not in OBJECTIVE_TYPES for q in exam.questions)


def grade_student(exam: Exam, student: StudentRecord) -> StudentRecord:
    """ปรนัยตรวจด้วยโค้ด อัตนัยตรวจด้วย grader (เรียก LLM) — อาจโยน LLMError"""
    return student.model_copy(update={"results": grade_exam(exam, student.answers)})


def ungraded(session: ClassSession) -> list[StudentRecord]:
    return [s for s in session.students if s.results is None]


def score_rows(session: ClassSession) -> list[dict]:
    """ตารางคะแนนรายคน (ใช้แสดงในแอปและส่งออก CSV)"""
    rows = []
    for s in session.students:
        row: dict = {"เลขที่": s.student_no, "ชื่อ": s.name}
        if s.results is None:
            row.update({"คะแนนรวม": None, "คะแนนเต็ม": None, "ร้อยละ": None, "ปรนัย": None, "อัตนัย": None,
                        "สถานะ": "ยังไม่ได้ตรวจ"})
        else:
            t = scoring.totals(s.results)
            row.update({
                "คะแนนรวม": t["score"],
                "คะแนนเต็ม": t["max"],
                "ร้อยละ": round(100 * t["score"] / t["max"], 1) if t["max"] else 0.0,
                "ปรนัย": t["objective_score"],
                "อัตนัย": t["subjective_score"],
                "สถานะ": "ตรวจแล้ว",
            })
            row.update({f"ข้อ {r.question_id}": r.points_awarded for r in s.results})
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


def to_json(session: ClassSession) -> bytes:
    return session.model_dump_json(indent=1).encode("utf-8")


def from_json(data: bytes) -> ClassSession:
    try:
        return ClassSession.model_validate_json(data)
    except ValidationError as e:
        raise ClassFileError("ไฟล์นี้ไม่ใช่ข้อมูลห้องที่บันทึกจากแอป หรือไฟล์เสียหาย") from e
