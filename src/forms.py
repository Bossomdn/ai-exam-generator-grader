"""เชื่อมกับ Google Forms

- apps_script(): สร้างโค้ด Google Apps Script ที่สร้าง Google Form จากข้อสอบ (ครูวางใน script.google.com แล้วกด Run)
- parse_responses(): อ่านไฟล์ผลตอบ (CSV จาก Google Forms หรือเทมเพลตของแอป) เป็นคำตอบรายคน
- template_csv(): เทมเพลต CSV สำหรับกรอกคำตอบเอง (เช่น คัดลอกจากกระดาษ)

จับคู่คอลัมน์กับข้อสอบด้วยหัวคอลัมน์ที่ขึ้นต้นด้วย "ข้อ n" จึงห้ามแก้ชื่อข้อในฟอร์ม
ปรนัยจับคู่ด้วยข้อความตัวเลือก (ฟอร์มสลับลำดับตัวเลือกได้โดยไม่กระทบ)
"""

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional, Union

from .answer_reader import parse_choice
from .schemas import OBJECTIVE_TYPES, Exam, Question, StudentRecord

NAME_TITLE = "ชื่อ-นามสกุล"
ID_TITLE = "เลขที่ / รหัสผู้สอบ (ถ้ามี)"
QUESTION_HEADER = re.compile(r"^\s*ข้อ\s*(\d+)")
TIMESTAMP_HEADERS = ("timestamp", "ประทับเวลา")


class FormsError(ValueError):
    """ไฟล์ที่อ่านไม่ได้ ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


def question_title(q: Question) -> str:
    """ชื่อข้อในฟอร์ม = หัวคอลัมน์ใน CSV (รวมเป็นบรรทัดเดียว เพราะชื่อข้อในฟอร์มขึ้นบรรทัดใหม่ไม่ได้)"""
    return f"ข้อ {q.id}. " + " ".join(q.question.split())


# ---------------- สร้าง Google Form ----------------

SCRIPT_TEMPLATE = """/**
 * สร้าง Google Form จากข้อสอบ "__TITLE__" (สร้างโดย AI Exam Generator & Grader)
 *
 * วิธีใช้
 * 1. เปิด https://script.google.com แล้วกด "New project" (โปรเจ็กต์ใหม่)
 * 2. ลบโค้ดตัวอย่างทิ้ง แล้ววางโค้ดทั้งหมดในไฟล์นี้ กด Save
 * 3. เลือกฟังก์ชัน createExamForm แล้วกด Run
 * 4. ครั้งแรก Google จะขอสิทธิ์: Review permissions → เลือกบัญชี → Advanced → Go to ... (unsafe) → Allow
 *    (มีคำเตือนเพราะเป็นสคริปต์ที่เราสร้างเอง ไม่ได้ผ่านการตรวจของ Google)
 * 5. ดูลิงก์ใน Execution log: ลิงก์แก้ไขฟอร์ม (สำหรับครู) และลิงก์ส่งให้ผู้สอบ
 *
 * เก็บคำตอบครบแล้ว: เปิดฟอร์ม → แท็บ Responses → ⋮ → Download responses (.csv) แล้วนำไฟล์ไปนำเข้าในแอป
 * ห้ามแก้ชื่อข้อ ("ข้อ 1." ...) และช่อง "ชื่อ-นามสกุล" เพราะแอปใช้จับคู่คำตอบ
 */

// false (แนะนำ) = ฟอร์มธรรมดา แอปเป็นผู้ตรวจ ผู้สอบไม่เห็นเฉลย
// true = เปิดเป็นแบบทดสอบ (quiz) ให้ Google ให้คะแนนปรนัยด้วย — ต้องไปปิดการตั้งค่า
//        "ผู้ตอบสามารถดูคำตอบที่ถูกต้อง" ในฟอร์มเอง ไม่งั้นคนที่ส่งก่อนจะเห็นเฉลย
var AS_QUIZ = false;

var EXAM = __EXAM_DATA__;

function createExamForm() {
  var form = FormApp.create(EXAM.title);
  form.setDescription(EXAM.description);
  form.setConfirmationMessage('ส่งคำตอบเรียบร้อยแล้ว ขอบคุณ');
  if (AS_QUIZ) {
    form.setIsQuiz(true);
  }

  form.addTextItem().setTitle(EXAM.nameTitle).setRequired(true);
  form.addTextItem().setTitle(EXAM.idTitle);

  EXAM.sections.forEach(function (section) {
    form.addSectionHeaderItem().setTitle(section.title);
    section.questions.forEach(function (q) {
      var item;
      if (q.choices.length > 0) {
        item = form.addMultipleChoiceItem().setTitle(q.title);
        item.setChoices(q.choices.map(function (text, i) {
          return AS_QUIZ ? item.createChoice(text, i === q.correct) : item.createChoice(text);
        }));
      } else {
        item = form.addParagraphTextItem().setTitle(q.title);
      }
      item.setHelpText(q.help);
      if (AS_QUIZ) {
        item.setPoints(q.quizPoints);
      }
    });
  });

  Logger.log('ลิงก์แก้ไขฟอร์ม (สำหรับครู): ' + form.getEditUrl());
  Logger.log('ลิงก์ส่งให้ผู้สอบ: ' + form.getPublishedUrl());
}
"""


def _form_data(exam: Exam) -> dict:
    objective = [q for q in exam.questions if q.type in OBJECTIVE_TYPES]
    subjective = [q for q in exam.questions if q.type not in OBJECTIVE_TYPES]
    sections = []
    for questions, label in ((objective, "ปรนัย (เลือกคำตอบที่ถูกที่สุด)"), (subjective, "อัตนัย (เขียนตอบ)")):
        if not questions:
            continue
        points = sum(q.points for q in questions)
        sections.append({
            "title": f"ตอนที่ {len(sections) + 1} {label} — {len(questions)} ข้อ {points:g} คะแนน",
            "questions": [{
                "title": question_title(q),
                "choices": [c.strip() for c in q.choices],
                "correct": q.correct_choice,
                "help": f"{q.points:g} คะแนน",
                "quizPoints": max(1, round(q.points)),  # Google Forms รับคะแนนเป็นจำนวนเต็มเท่านั้น
            } for q in questions],
        })
    total = sum(q.points for q in exam.questions)
    return {
        "title": exam.title,
        "description": f"จำนวน {len(exam.questions)} ข้อ · คะแนนเต็ม {total:g} คะแนน",
        "nameTitle": NAME_TITLE,
        "idTitle": ID_TITLE,
        "sections": sections,
    }


def apps_script(exam: Exam) -> str:
    data = json.dumps(_form_data(exam), ensure_ascii=False, indent=2)
    title = " ".join(exam.title.split()).replace("*/", "* /")
    return SCRIPT_TEMPLATE.replace("__TITLE__", title).replace("__EXAM_DATA__", data)


def template_csv(exam: Exam) -> bytes:
    """หัวคอลัมน์สำหรับกรอกคำตอบเอง (utf-8-sig เพื่อให้ Excel แสดงภาษาไทยถูก)"""
    buf = io.StringIO()
    csv.writer(buf).writerow(["เลขที่", NAME_TITLE] + [f"ข้อ {q.id}" for q in exam.questions])
    return buf.getvalue().encode("utf-8-sig")


# ---------------- อ่านผลตอบ ----------------

@dataclass
class ImportResult:
    students: list[StudentRecord]
    source: str
    matched_questions: list[int]
    warnings: list[str] = field(default_factory=list)


def _decode(data: bytes, filename: str) -> str:
    if filename.lower().endswith(".zip") or data[:2] == b"PK":  # Google Forms ดาวน์โหลดเป็น .csv.zip
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise FormsError("ในไฟล์ .zip ไม่มีไฟล์ .csv")
                data = zf.read(names[0])
        except zipfile.BadZipFile as e:
            raise FormsError("เปิดไฟล์ .zip ไม่ได้ ไฟล์อาจเสียหาย") from e
    for encoding in ("utf-8-sig", "cp874"):  # cp874 = CSV ที่ Excel ภาษาไทยบันทึกแบบ ANSI
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FormsError("อ่านตัวอักษรในไฟล์ไม่ได้ ลองบันทึกเป็น CSV แบบ UTF-8")


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
            if title.endswith("]"):  # คอลัมน์ "[Score]" / "[Feedback]" ที่ Google เพิ่มให้แบบทดสอบ
                continue
            if qid not in valid_ids:
                warnings.append(f"คอลัมน์ “{title[:40]}” ไม่ตรงกับข้อใดในข้อสอบ — ข้าม")
            elif qid in columns:
                warnings.append(f"มีคอลัมน์ของข้อ {qid} มากกว่า 1 คอลัมน์ — ใช้คอลัมน์แรก")
            else:
                columns[qid] = col
            continue
        lowered = title.casefold()
        if name_col is None and (title == NAME_TITLE or "ชื่อ" in title or "name" in lowered):
            name_col = col
        elif id_col is None and ("เลขที่" in title or "รหัส" in title or lowered in ("id", "no", "no.")):
            id_col = col
    return columns, name_col, id_col, warnings


def _cell(row: list[str], col: Optional[int]) -> str:
    return row[col].strip() if col is not None and col < len(row) else ""


def parse_responses(exam: Exam, data: bytes, filename: str = "responses.csv") -> ImportResult:
    rows = [row for row in csv.reader(io.StringIO(_decode(data, filename))) if any(cell.strip() for cell in row)]
    if not rows:
        raise FormsError("ไฟล์ว่าง ไม่มีข้อมูล")
    header, body = rows[0], rows[1:]
    columns, name_col, id_col, warnings = _map_columns(exam, header)
    if not columns:
        raise FormsError(
            "ไม่พบคอลัมน์คำตอบ — หัวคอลัมน์ต้องขึ้นต้นด้วย “ข้อ 1”, “ข้อ 2”, ... "
            "(ใช้ไฟล์จากฟอร์มที่แอปสร้าง หรือเทมเพลต CSV ของแอป)"
        )
    missing = [q.id for q in exam.questions if q.id not in columns]
    if missing:
        warnings.append(f"ไม่พบคอลัมน์ของข้อ {', '.join(map(str, missing))} — นับเป็นไม่ได้ตอบทุกคน")
    if name_col is None:
        warnings.append(f"ไม่พบคอลัมน์ชื่อ (“{NAME_TITLE}”) — ตั้งชื่อเป็น “ผู้สอบคนที่ n” แทน")
    is_google = any(h.strip().casefold() in TIMESTAMP_HEADERS for h in header)

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
        student = StudentRecord(
            student_no=_cell(row, id_col), name=name, answers=answers,
            source="google_forms" if is_google else "csv", import_notes=notes,
        )
        if student.key in by_key:
            warnings.append(f"{name} ส่งคำตอบมากกว่า 1 ครั้ง — ใช้ครั้งล่าสุด")
        by_key[student.key] = student  # แถวหลังทับแถวก่อน (Google Forms เรียงตามเวลาส่ง)
    return ImportResult(
        students=list(by_key.values()),
        source="google_forms" if is_google else "csv",
        matched_questions=sorted(columns),
        warnings=warnings,
    )
