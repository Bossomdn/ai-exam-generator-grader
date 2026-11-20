"""สร้าง Apps Script ของ Google Form และอ่านไฟล์ผลตอบ CSV (ไม่ต้องใช้โมเดล / อินเทอร์เน็ต)"""

import csv
import io
import json
import zipfile

import pytest

from src import forms
from src.schemas import Exam, Question, RubricCriterion

COMMON = dict(concept="c", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium",
              explanation="", model_answer="", key_points=[], rubric=[])
MCQ = Question(**COMMON, id=1, type="mcq", question="ข้อใดเป็นการเรียนรู้\nแบบมีผู้สอน", points=1,
               choices=["Supervised", "Unsupervised", "RL", "Clustering"], correct_choice=0)
TF = Question(**COMMON, id=2, type="true_false", question="Test set ใช้ปรับ hyperparameter", points=1,
              choices=["ถูก", "ผิด"], correct_choice=1)
ESSAY = Question(**{**COMMON, "rubric": [RubricCriterion(name="เนื้อหา", description="d", max_points=10)]},
                 id=3, type="essay", question="อธิบาย overfitting", points=10, choices=[], correct_choice=None)
EXAM = Exam(title='ข้อสอบ ML */ ทดสอบ', spec_summary="", concepts=["c"], questions=[MCQ, TF, ESSAY])


def script_data(script: str) -> dict:
    start = script.index("var EXAM = ") + len("var EXAM = ")
    end = script.index("\n\nfunction createExamForm")
    return json.loads(script[start:end].rstrip().rstrip(";"))


def to_csv(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


GOOGLE_HEADER = ["Timestamp", "Score", forms.NAME_TITLE, forms.ID_TITLE] + [forms.question_title(q) for q in EXAM.questions]


def test_apps_script_embeds_exam_and_keeps_header_comment_closed():
    script = forms.apps_script(EXAM)
    assert "function createExamForm()" in script and "var AS_QUIZ = false;" in script
    # ชื่อข้อสอบที่มี */ ต้องไม่ปิดคอมเมนต์ส่วนหัวก่อนเวลา
    assert script.index("*/") > script.index("ห้ามแก้ชื่อข้อ")
    data = script_data(script)
    assert data["nameTitle"] == forms.NAME_TITLE
    assert [s["title"].split(" —")[0] for s in data["sections"]] == [
        "ตอนที่ 1 ปรนัย (เลือกคำตอบที่ถูกที่สุด)", "ตอนที่ 2 อัตนัย (เขียนตอบ)"]
    questions = [q for s in data["sections"] for q in s["questions"]]
    assert [q["title"] for q in questions] == [forms.question_title(q) for q in EXAM.questions]
    assert questions[0]["title"] == "ข้อ 1. ข้อใดเป็นการเรียนรู้ แบบมีผู้สอน"  # รวมเป็นบรรทัดเดียว
    assert (questions[0]["choices"], questions[0]["correct"]) == (MCQ.choices, 0)
    assert questions[2]["choices"] == [] and questions[2]["quizPoints"] == 10


def test_google_forms_csv_maps_answers_by_choice_text():
    header = GOOGLE_HEADER + [forms.question_title(MCQ) + " [Score]"]  # คอลัมน์คะแนนรายข้อของ quiz ต้องถูกข้าม
    text = to_csv(header, [
        ["2026/11/20 10:00", "2 / 12", "สมชาย ใจดี", "1", "Supervised", "ผิด", "overfitting คือ...", "1"],
        ["2026/11/20 10:05", "", "สมหญิง", "", "  unsupervised ", "", "", ""],
        ["2026/11/20 10:09", "", "สมชาย ใจดี", "1", "RL", "ถูก", "แก้คำตอบ", "0"],  # ส่งซ้ำ → ใช้ครั้งล่าสุด
    ])
    result = forms.parse_responses(EXAM, text.encode("utf-8"))
    assert result.source == "google_forms" and result.matched_questions == [1, 2, 3]
    by_name = {s.name: s for s in result.students}
    assert by_name["สมชาย ใจดี"].answers == {1: 2, 2: 0, 3: "แก้คำตอบ"}
    assert by_name["สมหญิง"].answers == {1: 1, 2: None, 3: ""}
    assert all(not s.import_notes for s in result.students)
    assert any("มากกว่า 1 ครั้ง" in w for w in result.warnings)
    assert not any("มากกว่า 1 คอลัมน์" in w for w in result.warnings)


def test_unmatched_choice_counts_as_unanswered_and_near_match_is_flagged():
    text = to_csv(GOOGLE_HEADER, [["t", "", "ก", "", "Superviseed", "ไม่แน่ใจ", ""]])
    student = forms.parse_responses(EXAM, text.encode("utf-8")).students[0]
    assert student.answers[1] == 0 and "ใกล้เคียง" in student.import_notes[0]
    assert student.answers[2] is None and "จับคู่" in student.import_notes[1]


def _zip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ข้อสอบ ML (Responses).csv", data)
    return buf.getvalue()


@pytest.mark.parametrize("encode, filename", [
    (lambda t: t.encode("utf-8-sig"), "responses.csv"),
    (lambda t: t.encode("cp874"), "excel_ansi.csv"),  # Excel ภาษาไทยบันทึกแบบ ANSI
    (lambda t: _zip(t.encode("utf-8")), "ข้อสอบ ML.csv.zip"),  # Google Forms ดาวน์โหลดเป็น zip
])
def test_reads_utf8_bom_thai_ansi_and_zip(encode, filename):
    text = to_csv(GOOGLE_HEADER, [["t", "", "สมศรี", "", "RL", "ถูก", "ตอบ"]])
    student = forms.parse_responses(EXAM, encode(text), filename).students[0]
    assert (student.name, student.answers) == ("สมศรี", {1: 2, 2: 0, 3: "ตอบ"})


def test_filled_template_with_letters_is_not_flagged():
    filled = forms.template_csv(EXAM).decode("utf-8-sig") + "5,นายเอ,ข,ผิด,คำตอบเรียงความ\n"
    result = forms.parse_responses(EXAM, filled.encode("utf-8-sig"))
    student = result.students[0]
    assert result.source == "csv" and (student.student_no, student.name) == ("5", "นายเอ")
    assert student.answers == {1: 1, 2: 1, 3: "คำตอบเรียงความ"} and student.import_notes == []


def test_missing_columns_and_unusable_files():
    text = to_csv([forms.NAME_TITLE, forms.question_title(MCQ)], [["นายบี", "RL"]])
    result = forms.parse_responses(EXAM, text.encode("utf-8"))
    assert result.matched_questions == [1] and any("ข้อ 2, 3" in w for w in result.warnings)
    assert result.students[0].answers == {1: 2, 2: None, 3: ""}
    with pytest.raises(forms.FormsError):
        forms.parse_responses(EXAM, "ชื่อ,คะแนน\nก,1\n".encode("utf-8"))  # ไม่มีคอลัมน์ "ข้อ n"
    with pytest.raises(forms.FormsError):
        forms.parse_responses(EXAM, b"")
    with pytest.raises(forms.FormsError):
        forms.parse_responses(EXAM, b"PK not a zip", "broken.zip")
