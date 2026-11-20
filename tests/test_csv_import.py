"""อ่านไฟล์คำตอบ CSV ทั้งห้อง (ไม่ต้องใช้โมเดล)"""

import csv
import io

import pytest

from src import csv_import
from src.schemas import Exam, Question, RubricCriterion

COMMON = dict(concept="c", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium",
              explanation="", model_answer="", key_points=[], rubric=[])
MCQ = Question(**COMMON, id=1, type="mcq", question="ข้อใดเป็นการเรียนรู้แบบมีผู้สอน", points=1,
               choices=["Supervised", "Unsupervised", "RL", "Clustering"], correct_choice=0)
TF = Question(**COMMON, id=2, type="true_false", question="Test set ใช้ปรับ hyperparameter", points=1,
              choices=["ถูก", "ผิด"], correct_choice=1)
ESSAY = Question(**{**COMMON, "rubric": [RubricCriterion(name="เนื้อหา", description="d", max_points=10)]},
                 id=3, type="essay", question="อธิบาย overfitting", points=10, choices=[], correct_choice=None)
EXAM = Exam(title="ข้อสอบ ML", spec_summary="", concepts=["c"], questions=[MCQ, TF, ESSAY])


def to_csv(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


HEADER = ["เลขที่", csv_import.NAME_TITLE, "ข้อ 1", "ข้อ 2", "ข้อ 3"]


def test_filled_template_with_letters_text_and_true_false():
    filled = csv_import.template_csv(EXAM).decode("utf-8-sig") + "5,นายเอ,ข,ผิด,คำตอบเรียงความ\n6,นางบี,  supervised ,,\n"
    result = csv_import.parse_responses(EXAM, filled.encode("utf-8-sig"))
    a, b = result.students
    assert (a.student_no, a.name, a.answers) == ("5", "นายเอ", {1: 1, 2: 1, 3: "คำตอบเรียงความ"})
    assert b.answers == {1: 0, 2: None, 3: ""}  # ข้อความตัวเลือกไม่สนตัวพิมพ์/ช่องว่าง
    assert all(s.source == "csv" and s.reviewed and not s.import_notes for s in result.students)


def test_extra_columns_ignored_and_duplicate_rows_use_last():
    text = to_csv(["Timestamp"] + HEADER + ["หมายเหตุ"], [
        ["t1", "1", "สมชาย", "Supervised", "ผิด", "ตอบครั้งแรก", "x"],
        ["t2", "1", "สมชาย", "RL", "ถูก", "แก้คำตอบ", "y"],
    ])
    result = csv_import.parse_responses(EXAM, text.encode("utf-8"))
    assert result.matched_questions == [1, 2, 3] and len(result.students) == 1
    assert result.students[0].answers == {1: 2, 2: 0, 3: "แก้คำตอบ"}
    assert any("มากกว่า 1 แถว" in w for w in result.warnings)


def test_unmatched_choice_counts_as_unanswered_and_near_match_is_flagged():
    text = to_csv(HEADER, [["", "ก", "Superviseed", "ไม่แน่ใจ", ""]])
    student = csv_import.parse_responses(EXAM, text.encode("utf-8")).students[0]
    assert student.answers[1] == 0 and "ใกล้เคียง" in student.import_notes[0]
    assert student.answers[2] is None and "จับคู่" in student.import_notes[1]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "cp874"])  # cp874 = Excel ภาษาไทยบันทึกแบบ ANSI
def test_reads_common_thai_encodings(encoding):
    text = to_csv(HEADER, [["7", "สมศรี", "RL", "ถูก", "ตอบ"]])
    student = csv_import.parse_responses(EXAM, text.encode(encoding)).students[0]
    assert (student.name, student.answers) == ("สมศรี", {1: 2, 2: 0, 3: "ตอบ"})


def test_missing_columns_and_unusable_files():
    text = to_csv([csv_import.NAME_TITLE, "ข้อ 1"], [["นายบี", "RL"]])
    result = csv_import.parse_responses(EXAM, text.encode("utf-8"))
    assert result.matched_questions == [1] and any("ข้อ 2, 3" in w for w in result.warnings)
    assert result.students[0].answers == {1: 2, 2: None, 3: ""}
    with pytest.raises(csv_import.CsvImportError):
        csv_import.parse_responses(EXAM, "ชื่อ,คะแนน\nก,1\n".encode("utf-8"))  # ไม่มีคอลัมน์ "ข้อ n"
    with pytest.raises(csv_import.CsvImportError):
        csv_import.parse_responses(EXAM, b"")


def test_roster_template_and_split_name_columns():
    filled = csv_import.roster_template().decode("utf-8-sig") + "๐๑,6501,นายเอ  ใจดี\n2,,นางสาวบี\n,6503,ไม่มีเลขที่\n2,,นางสาวบี (แก้)\n"
    entries, warnings = csv_import.parse_roster(filled.encode("utf-8-sig"))
    assert [(e.number, e.code, e.name) for e in entries] == [("1", "6501", "นายเอ ใจดี"), ("2", "", "นางสาวบี (แก้)")]
    assert any("แถวที่ 4" in w for w in warnings) and any("เลขที่ 2" in w for w in warnings)

    split = to_csv(["ลำดับ", "เลขที่", "เลขประจำตัว", "คำนำหน้า", "ชื่อ", "นามสกุล"], [["1", "3", "6503", "ด.ญ.", "ซี", "ดีมาก"]])
    entries, _ = csv_import.parse_roster(split.encode("cp874"))
    assert [(e.number, e.code, e.name) for e in entries] == [("3", "6503", "ด.ญ.ซี ดีมาก")]

    with pytest.raises(csv_import.CsvImportError, match="เลขที่"):
        csv_import.parse_roster(to_csv(["ชื่อ"], [["ก"]]).encode("utf-8"))


def test_answer_csv_normalizes_student_numbers():
    text = to_csv(HEADER, [["๐๕", "สมศรี", "RL", "ถูก", "ตอบ"]])
    assert csv_import.parse_responses(EXAM, text.encode("utf-8")).students[0].student_no == "5"
