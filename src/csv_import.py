"""นำเข้าคำตอบทั้งห้องและรายชื่อนักเรียนจากไฟล์ CSV

- template_csv(): เทมเพลตให้ครูกรอกคำตอบ (คอลัมน์ เลขที่, ชื่อ-นามสกุล, [ชุด], ข้อ 1, ข้อ 2, ...)
- parse_responses(): อ่าน CSV เป็นคำตอบรายคน
- roster_template() / parse_roster(): รายชื่อห้อง (เลขที่, รหัสนักเรียน, ชื่อ-นามสกุล)

จับคู่คอลัมน์กับข้อสอบด้วยหัวคอลัมน์ที่ขึ้นต้นด้วย "ข้อ n" (คอลัมน์อื่นที่ไม่รู้จักจะถูกข้าม)
ปรนัยจับคู่ด้วยข้อความตัวเลือก ตัวอักษร (ก ข ค ง / A B C D) หรือ ถูก/ผิด
เรียงความ: ใส่ข้อความคำตอบ (AI ตรวจ) หรือใส่เฉพาะตัวเลขคะแนนที่ครูตรวจบนกระดาษแล้ว (ไม่ส่งให้ AI)
  ตอบสั้นเก็บเป็นข้อความเสมอ เพราะคำตอบที่เป็นตัวเลขอาจเป็นคำตอบจริง
มีชุดสลับ: กรอกเลขข้อและตัวเลือกตามที่พิมพ์ในชุดของคนนั้น (คอลัมน์ "ชุด") ระบบแปลงกลับเป็นข้อสอบต้นฉบับเอง
"""

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional, Union

from . import exam_forms
from .answer_reader import parse_choice
from .class_session import normalize_no
from .schemas import OBJECTIVE_TYPES, Exam, ExamForm, Question, RosterEntry, StudentRecord

NAME_TITLE = "ชื่อ-นามสกุล"
FORM_TITLE = "ชุด"
QUESTION_HEADER = re.compile(r"^\s*ข้อ\s*(\d+)")
SCORE = re.compile(r"^\d+(?:\.\d+)?$")  # ช่องเรียงความที่มีแต่ตัวเลข = คะแนนที่ครูตรวจเอง


class CsvImportError(ValueError):
    """ไฟล์ที่อ่านไม่ได้ ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def _layout(exam: Exam, forms: list[ExamForm]) -> Exam:
    """เลขข้อตามที่พิมพ์ (มีชุดสลับ: ทุกชุดใช้เลข 1..n เหมือนกัน)"""
    return exam_forms.form_exam(exam, forms[0]) if forms else exam


def template_csv(exam: Exam, forms: list[ExamForm] = ()) -> bytes:
    """หัวคอลัมน์สำหรับกรอกคำตอบเอง (utf-8-sig เพื่อให้ Excel แสดงภาษาไทยถูก)"""
    buf = io.StringIO()
    form_column = [FORM_TITLE] if forms else []
    csv.writer(buf).writerow(["เลขที่", NAME_TITLE] + form_column + [f"ข้อ {q.id}" for q in _layout(exam, forms).questions])
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


def _map_columns(
    exam: Exam, header: list[str]
) -> tuple[dict[int, int], Optional[int], Optional[int], Optional[int], list[str]]:
    """คืน ({question_id: คอลัมน์}, คอลัมน์ชื่อ, คอลัมน์เลขที่, คอลัมน์ชุด, คำเตือน)"""
    valid_ids = {q.id for q in exam.questions}
    columns: dict[int, int] = {}
    name_col = id_col = form_col = None
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
        if form_col is None and (title.startswith(FORM_TITLE) or lowered in ("form", "version")):
            form_col = col
        elif name_col is None and ("ชื่อ" in title or "name" in lowered):
            name_col = col
        elif id_col is None and ("เลขที่" in title or "รหัส" in title or lowered in ("id", "no", "no.")):
            id_col = col
    return columns, name_col, id_col, form_col, warnings


def _cell(row: list[str], col: Optional[int]) -> str:
    return row[col].strip() if col is not None and col < len(row) else ""


def parse_responses(exam: Exam, data: bytes, forms: list[ExamForm] = ()) -> ImportResult:
    rows = [row for row in csv.reader(io.StringIO(_decode(data))) if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvImportError("ไฟล์ว่าง ไม่มีข้อมูล")
    header, body = rows[0], rows[1:]
    layout = _layout(exam, forms)
    columns, name_col, id_col, form_col, warnings = _map_columns(layout, header)
    if not columns:
        raise CsvImportError(
            "ไม่พบคอลัมน์คำตอบ — หัวคอลัมน์ต้องขึ้นต้นด้วย “ข้อ 1”, “ข้อ 2”, ... (ดาวน์โหลดเทมเพลต CSV จากแอป)"
        )
    missing = [q.id for q in layout.questions if q.id not in columns]
    if missing:
        warnings.append(f"ไม่พบคอลัมน์ของข้อ {', '.join(map(str, missing))} — นับเป็นไม่ได้ตอบทุกคน")
    if name_col is None:
        warnings.append(f"ไม่พบคอลัมน์ชื่อ (“{NAME_TITLE}”) — ตั้งชื่อเป็น “ผู้สอบคนที่ n” แทน")
    if forms and form_col is None:
        warnings.append(f"ไม่พบคอลัมน์ “{FORM_TITLE}” — ตรวจทุกคนตามชุด {forms[0].code}")

    by_key: dict[str, StudentRecord] = {}
    for number, row in enumerate(body, start=1):
        name = _cell(row, name_col) or f"ผู้สอบคนที่ {number}"
        notes = []
        form = None
        if forms:
            form = exam_forms.find(forms, _cell(row, form_col))
            if form is None:
                if form_col is not None:
                    notes.append(f"ไม่ระบุชุดหรือชุดไม่ถูกต้อง — ตรวจตามชุด {forms[0].code}")
                form = forms[0]
        questions = {q.id: q for q in (exam_forms.form_exam(exam, form) if form else exam).questions}
        answers: dict[int, Union[int, str, None]] = {}
        scores: dict[int, float] = {}
        for qid, col in columns.items():
            raw, q = _cell(row, col), questions[qid]
            if q.type == "essay" and SCORE.match(raw):
                answers[qid] = ""
                if float(raw) <= q.points:
                    scores[qid] = float(raw)
                else:
                    notes.append(f"ข้อ {qid}: คะแนน {raw} เกินคะแนนเต็ม {q.points:g} — นับเป็นไม่ได้ตอบ แก้ไฟล์แล้วนำเข้าใหม่")
            elif q.type not in OBJECTIVE_TYPES:
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
        student = StudentRecord(
            student_no=normalize_no(_cell(row, id_col)), name=name, source="csv", import_notes=notes,
            answers=exam_forms.to_base(form, answers) if form else answers, form=form.code if form else "",
            teacher_scores=exam_forms.to_base(form, scores) if form else scores,
        )
        if student.key in by_key:
            warnings.append(f"{name} มีมากกว่า 1 แถว — ใช้แถวล่างสุด")
        by_key[student.key] = student
    return ImportResult(students=list(by_key.values()), matched_questions=sorted(columns), warnings=warnings)


# ---------------- รายชื่อห้อง ----------------

def roster_template() -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerow(["เลขที่", "รหัสนักเรียน", NAME_TITLE])
    return buf.getvalue().encode("utf-8-sig")


def _roster_columns(header: list[str]) -> dict[str, int]:
    """หาคอลัมน์: number, code และชื่อแบบช่องเดียว (name) หรือแยก คำนำหน้า (title) / ชื่อ (first) / นามสกุล (last)"""
    columns: dict[str, int] = {}
    for col, raw in enumerate(header):
        title = "".join(raw.split())
        if "เลขที่" in title or title.casefold() in ("no", "no."):
            role = "number"
        elif "รหัส" in title or "ประจำตัว" in title or title.casefold() == "id":
            role = "code"
        elif ("ชื่อ" in title and "สกุล" in title) or title.casefold() == "name":
            role = "name"
        elif "คำนำหน้า" in title:
            role = "title"
        elif "นามสกุล" in title:
            role = "last"
        elif "ชื่อ" in title:
            role = "first"
        else:
            continue
        columns.setdefault(role, col)
    return columns


def parse_roster(data: bytes) -> tuple[list[RosterEntry], list[str]]:
    """อ่านรายชื่อห้อง คืน (รายชื่อเรียงตามไฟล์, คำเตือน) — รองรับชื่อช่องเดียว หรือแยกคำนำหน้า/ชื่อ/นามสกุล"""
    rows = [row for row in csv.reader(io.StringIO(_decode(data))) if any(cell.strip() for cell in row)]
    if not rows:
        raise CsvImportError("ไฟล์ว่าง ไม่มีข้อมูล")
    columns = _roster_columns(rows[0])
    if "number" not in columns or not {"name", "first"} & set(columns):
        raise CsvImportError(f"ต้องมีคอลัมน์ “เลขที่” และ “{NAME_TITLE}” (ดาวน์โหลดเทมเพลตรายชื่อจากแอป)")
    entries: dict[str, RosterEntry] = {}
    warnings = []
    for line, row in enumerate(rows[1:], start=2):
        if "name" in columns:
            name = _cell(row, columns["name"])
        else:  # คำนำหน้าติดกับชื่อตามแบบไทย เช่น นายสมชาย ใจดี
            first = _cell(row, columns.get("title")) + _cell(row, columns["first"])
            name = " ".join(part for part in (first, _cell(row, columns.get("last"))) if part)
        number = normalize_no(_cell(row, columns["number"]))
        if not number or not name:
            warnings.append(f"แถวที่ {line}: ไม่มีเลขที่หรือชื่อ — ข้าม")
            continue
        if number in entries:
            warnings.append(f"เลขที่ {number} มีมากกว่า 1 แถว — ใช้แถวล่างสุด")
        entries[number] = RosterEntry(number=number, name=" ".join(name.split()),
                                      code=_cell(row, columns.get("code")))
    if not entries:
        raise CsvImportError("ไม่พบรายชื่อในไฟล์")
    return list(entries.values()), warnings
