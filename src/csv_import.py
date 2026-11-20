"""นำเข้าคำตอบทั้งห้องจากไฟล์ CSV

- template_csv(): เทมเพลตให้ครูกรอกคำตอบ (คอลัมน์ เลขที่, ชื่อ-นามสกุล, ข้อ 1, ข้อ 2, ...)
- parse_responses(): อ่าน CSV เป็นคำตอบรายคน

จับคู่คอลัมน์กับข้อสอบด้วยหัวคอลัมน์ที่ขึ้นต้นด้วย "ข้อ n" (คอลัมน์อื่นที่ไม่รู้จักจะถูกข้าม)
ปรนัยจับคู่ด้วยข้อความตัวเลือก ตัวอักษร (ก ข ค ง / A B C D) หรือ ถูก/ผิด
"""

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional, Union

from .answer_reader import parse_choice
from .schemas import OBJECTIVE_TYPES, Exam, Question, StudentRecord

NAME_TITLE = "ชื่อ-นามสกุล"
QUESTION_HEADER = re.compile(r"^\s*ข้อ\s*(\d+)")


class CsvImportError(ValueError):
    """ไฟล์ที่อ่านไม่ได้ ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def template_csv(exam: Exam) -> bytes:
    """หัวคอลัมน์สำหรับกรอกคำตอบเอง (utf-8-sig เพื่อให้ Excel แสดงภาษาไทยถูก)"""
    buf = io.StringIO()
    csv.writer(buf).writerow(["เลขที่", NAME_TITLE] + [f"ข้อ {q.id}" for q in exam.questions])
    return buf.getvalue().encode("utf-8-sig")


@dataclass
class ImportResult:
    students: list[StudentRecord]
    matched_questions: list[int]
    warnings: list[str] = field(default_factory=list)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp874"):  # cp874 = CSV ที่ Excel ภาษาไทยบันทึกแบบ ANSI
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvImportError("อ่านตัวอักษรในไฟล์ไม่ได้ ลองบันทึกเป็น CSV แบบ UTF-8")


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def match_choice(q: Question, raw: str) -> tuple[Optional[int], bool]:
    """คืน (index ตัวเลือก, จับคู่แบบใกล้เคียงหรือไม่)
    ลำดับ: ข้อความตัวเลือกตรงตัว → ตัวอักษร (ก/ข/A/B) หรือ ถูก/ผิด → ข้อความใกล้เคียง (นับเป็นใกล้เคียง)"""
    for i, choice in enumerate(q.choices):
        if _normalize(choice) == _normalize(raw):
            return i, False
    index = parse_choice(q, raw)  # ไม่ส่งข้อความตัวเลือก = ไม่เทียบแบบใกล้เคียง
    if index is not None:
        return index, False
    index = parse_choice(q, raw, raw)
    return index, index is not None


def _map_columns(exam: Exam, header: list[str]) -> tuple[dict[int, int], Optional[int], Optional[int], list[str]]:
    """คืน ({question_id: คอลัมน์}, คอลัมน์ชื่อ, คอลัมน์เลขที่, คำเตือน)"""
    valid_ids = {q.id for q in exam.questions}
    columns: dict[int, int] = {}
    name_col = id_col = None
    warnings = []
    for col, raw in enumerate(header):
        title = raw.strip()
        if match := QUESTION_HEADER.match(title):
            qid = int(match.group(1))
            if qid not in valid_ids:
                warnings.append(f"คอลัมน์ “{title[:40]}” ไม่ตรงกับข้อใดในข้อสอบ — ข้าม")
            elif qid in columns:
                warnings.append(f"มีคอลัมน์ของข้อ {qid} มากกว่า 1 คอลัมน์ — ใช้คอลัมน์แรก")
            else:
                columns[qid] = col
            continue
        lowered = title.casefold()
        if name_col is None and ("ชื่อ" in title or "name" in lowered):
            name_col = col
        elif id_col is None and ("เลขที่" in title or "รหัส" in title or lowered in ("id", "no", "no.")):
            id_col = col
    return columns, name_col, id_col, warnings


def _cell(row: list[str], col: Optional[int]) -> str:
    return row[col].strip() if col is not None and col < len(row) else ""


def parse_responses(exam: Exam, data: bytes) -> ImportResult:
    rows = [row for row in csv.reader(io.StringIO(_decode(data))) if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvImportError("ไฟล์ว่าง ไม่มีข้อมูล")
    header, body = rows[0], rows[1:]
    columns, name_col, id_col, warnings = _map_columns(exam, header)
    if not columns:
        raise CsvImportError(
            "ไม่พบคอลัมน์คำตอบ — หัวคอลัมน์ต้องขึ้นต้นด้วย “ข้อ 1”, “ข้อ 2”, ... (ดาวน์โหลดเทมเพลต CSV จากแอป)"
        )
    missing = [q.id for q in exam.questions if q.id not in columns]
    if missing:
        warnings.append(f"ไม่พบคอลัมน์ของข้อ {', '.join(map(str, missing))} — นับเป็นไม่ได้ตอบทุกคน")
    if name_col is None:
        warnings.append(f"ไม่พบคอลัมน์ชื่อ (“{NAME_TITLE}”) — ตั้งชื่อเป็น “ผู้สอบคนที่ n” แทน")

    questions = {q.id: q for q in exam.questions}
    by_key: dict[str, StudentRecord] = {}
    for number, row in enumerate(body, start=1):
        name = _cell(row, name_col) or f"ผู้สอบคนที่ {number}"
        notes = []
        answers: dict[int, Union[int, str, None]] = {}
        for qid, col in columns.items():
            raw, q = _cell(row, col), questions[qid]
            if q.type not in OBJECTIVE_TYPES:
                answers[qid] = raw
            elif not raw:
                answers[qid] = None
            else:
                index, approximate = match_choice(q, raw)
                answers[qid] = index
                if index is None:
                    notes.append(f"ข้อ {qid}: จับคู่ “{raw[:40]}” กับตัวเลือกไม่ได้ — นับเป็นไม่ได้ตอบ")
                elif approximate:
                    notes.append(f"ข้อ {qid}: “{raw[:40]}” ไม่ตรงกับตัวเลือกทุกตัวอักษร จับคู่แบบใกล้เคียง")
        for qid in missing:
            answers[qid] = None if questions[qid].type in OBJECTIVE_TYPES else ""
        student = StudentRecord(student_no=_cell(row, id_col), name=name, answers=answers, source="csv",
                                import_notes=notes)
        if student.key in by_key:
            warnings.append(f"{name} มีมากกว่า 1 แถว — ใช้แถวล่างสุด")
        by_key[student.key] = student
    return ImportResult(students=list(by_key.values()), matched_questions=sorted(columns), warnings=warnings)
