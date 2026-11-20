"""ข้อมูลทั้งห้อง: รับคำตอบ (CSV / กระดาษที่ AI อ่าน) ตรวจทาน ตรวจทีละคน ตารางคะแนน และบันทึก/เปิดไฟล์ (LLM จำลอง)"""

import pytest

from src import answer_reader, class_session, grader
from src.class_session import ClassFileError
from src.schemas import (
    CriterionScore, Exam, ExtractedAnswer, ExtractedAnswerSheet, Question, RubricCriterion, StudentRecord,
    SubjectiveGrade,
)

COMMON = dict(concept="c", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium",
              explanation="", model_answer="", key_points=[], rubric=[])
MCQ = Question(**COMMON, id=1, type="mcq", question="q1", points=1, choices=["a", "b", "c", "d"], correct_choice=0)
TF = Question(**COMMON, id=2, type="true_false", question="q2", points=1, choices=["ถูก", "ผิด"], correct_choice=1)
ESSAY = Question(**{**COMMON, "rubric": [RubricCriterion(name="เนื้อหา", description="d", max_points=6),
                                         RubricCriterion(name="เหตุผล", description="d", max_points=4)]},
                 id=3, type="essay", question="q3", points=10, choices=[], correct_choice=None)
EXAM = Exam(title="ชุดทดสอบ", spec_summary="", concepts=["c"], questions=[MCQ, TF, ESSAY])


def student(name: str, answers: dict, no: str = "") -> StudentRecord:
    return StudentRecord(student_no=no, name=name, answers=answers, source="csv")


def fake_grade(*, system, content, schema, effort):
    assert schema is SubjectiveGrade
    return SubjectiveGrade(criteria=[CriterionScore(criterion="เนื้อหา", points_awarded=5, max_points=6,
                                                    evidence="", comment="")],
                           missed_key_points=[], misconceptions=[], feedback="fb")


def sheet(name: str = "", no: str = "", answers=None) -> ExtractedAnswerSheet:
    return ExtractedAnswerSheet(student_name=name, student_no=no, answers=answers if answers is not None else [
        ExtractedAnswer(question_id=1, answer="ข", choice_text="b", confidence="high", note=""),
        ExtractedAnswer(question_id=3, answer="เรียงความ", choice_text="", confidence="low", note="ลายมือเลือน"),
    ])  # ข้อ 2 หายไป


def test_merge_adds_new_and_replaces_same_key_clearing_results():
    session, added, updated = class_session.merge_students(
        class_session.new_session(EXAM), [student("เอ", {1: 0}, no="1"), student("บี", {1: 1})])
    assert (added, updated) == (2, 0)
    session = class_session.update_student(session, 0, session.students[0].model_copy(update={"results": []}))
    session, added, updated = class_session.merge_students(session, [student("เอ (แก้ชื่อ)", {1: 2}, no="1")])
    assert (added, updated) == (0, 1) and len(session.students) == 2
    assert session.students[0].name == "เอ (แก้ชื่อ)" and session.students[0].results is None


def test_sheet_answers_need_review_and_carry_warnings():
    s = class_session.student_from_sheet(EXAM, "scan_07.png", sheet(name="นายเจ็ด", no="7"))
    assert (s.name, s.student_no, s.source, s.reviewed, s.sheet_name) == ("นายเจ็ด", "7", "answer_sheet", False, "scan_07.png")
    assert s.answers == {1: 1, 2: None, 3: "เรียงความ"}
    assert [n.split(":")[0] for n in s.import_notes] == ["ข้อ 2", "ข้อ 3"]  # หาไม่เจอ + อ่านไม่ค่อยออก
    unnamed = class_session.student_from_sheet(EXAM, "scan_08.png", sheet())
    assert unnamed.name == "scan_08" and "อ่านชื่อผู้สอบไม่ได้" in unnamed.import_notes[0]


def test_dedupe_keeps_sheets_with_same_name_apart():
    a = class_session.student_from_sheet(EXAM, "p1.png", sheet(name="สมชาย", no="3"))
    b = class_session.student_from_sheet(EXAM, "p2.png", sheet(name="สมชาย", no="3"))
    first, second = class_session.dedupe([a, b])
    assert first.key == "3" and second.name == "สมชาย (p2.png)" and second.key != first.key
    assert "ซ้ำ" in second.import_notes[0]


def test_only_reviewed_students_are_graded(monkeypatch):
    monkeypatch.setattr(grader, "call_structured", fake_grade)
    from_sheet = class_session.student_from_sheet(EXAM, "s.png", sheet(name="ซี"))
    session, _, _ = class_session.merge_students(class_session.new_session(EXAM), [
        student("เอ", {1: 0, 2: 1, 3: "คำตอบ"}, no="1"), from_sheet])
    assert class_session.awaiting_review(session) == [1] and class_session.ready_to_grade(session) == [0]
    assert [r["สถานะ"] for r in class_session.score_rows(session)] == ["ยังไม่ได้ตรวจ", "รอตรวจทาน"]
    session = class_session.update_student(session, 1, from_sheet.model_copy(update={"reviewed": True}))
    assert class_session.ready_to_grade(session) == [0, 1]


def test_grade_score_table_csv_and_json_roundtrip(monkeypatch):
    monkeypatch.setattr(grader, "call_structured", fake_grade)
    session, _, _ = class_session.merge_students(class_session.new_session(EXAM), [
        student("เอ", {1: 0, 2: 1, 3: "คำตอบ"}, no="1"),
        class_session.student_from_sheet(EXAM, "b.png", sheet(name="บี")),
    ])
    assert class_session.needs_llm(EXAM)
    session = class_session.update_student(session, 0, class_session.grade_student(EXAM, session.students[0]))

    rows = class_session.score_rows(session)
    assert (rows[0]["คะแนนรวม"], rows[0]["คะแนนเต็ม"], rows[0]["ร้อยละ"]) == (7, 12, 58.3)  # 1 + 1 + 5
    assert (rows[0]["ข้อ 3"], rows[0]["สถานะ"], rows[1]["สถานะ"]) == (5, "ตรวจแล้ว", "รอตรวจทาน")
    lines = class_session.scores_csv(session).decode("utf-8-sig").splitlines()
    assert lines[0] == "เลขที่,ชื่อ,คะแนนรวม,คะแนนเต็ม,ร้อยละ,ปรนัย,อัตนัย,สถานะ,ข้อ 1,ข้อ 2,ข้อ 3"
    assert lines[1].startswith("1,เอ,7,12,58.3,2,5,ตรวจแล้ว") and "รอตรวจทาน" in lines[2]

    loaded = class_session.from_json(class_session.to_json(session))
    assert loaded == session and loaded.students[1].extracted.student_name == "บี"
    with pytest.raises(ClassFileError):
        class_session.from_json(b'{"students": []}')
    with pytest.raises(ClassFileError):
        class_session.from_json(b"not json")


def test_objective_only_exam_needs_no_llm():
    exam = EXAM.model_copy(update={"questions": [MCQ, TF]})
    assert not class_session.needs_llm(exam)
    graded = class_session.grade_student(exam, student("ซี", {1: 0, 2: 0}))
    assert [r.points_awarded for r in graded.results] == [1, 0]


def test_read_sheet_calls_reader_and_notes_truncation(monkeypatch):
    monkeypatch.setattr(answer_reader, "read_answers", lambda exam, s: sheet(name="ดี"))
    file = answer_reader.AnswerSheetFile(name="big.pdf", pages=10, images=[b"x"], truncated=True)
    s = class_session.read_sheet(EXAM, file)
    assert s.name == "ดี" and "อ่านเฉพาะ" in s.import_notes[0]
