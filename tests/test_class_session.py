"""ข้อมูลทั้งห้อง: รับคำตอบ (CSV / กระดาษที่ AI อ่าน) ใช้ชื่อจากรายชื่อห้อง ตรวจทาน ตรวจทีละคน และตารางคะแนน (LLM จำลอง)"""

from src import answer_reader, class_session, grader
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


def test_grade_score_table_and_csv(monkeypatch):
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



def test_objective_only_exam_needs_no_llm():
    exam = EXAM.model_copy(update={"questions": [MCQ, TF]})
    assert not class_session.needs_llm(exam)
    graded = class_session.grade_student(exam, student("ซี", {1: 0, 2: 0}))
    assert [r.points_awarded for r in graded.results] == [1, 0]


def test_teacher_scored_essay_skips_the_model(monkeypatch):
    def no_llm(**_):
        raise AssertionError("ไม่ควรเรียกโมเดล")
    monkeypatch.setattr(grader, "call_structured", no_llm)
    scored = student("ดี", {1: 0, 2: 1, 3: ""}).model_copy(update={"teacher_scores": {3: 7.5}})
    blank = student("อี", {1: 1, 2: 0, 3: ""})
    assert not class_session.needs_llm(EXAM, [scored, blank])  # เรียงความว่างก็ไม่ต้องใช้โมเดล
    assert class_session.needs_llm(EXAM, [scored, student("เอฟ", {3: "คำตอบ"})])
    results = class_session.grade_student(EXAM, scored).results
    assert [r.points_awarded for r in results] == [1, 1, 7.5]
    assert results[2].criteria == [] and results[2].panel is None and not results[2].needs_teacher_review


def test_read_sheet_calls_reader_and_notes_truncation(monkeypatch):
    monkeypatch.setattr(answer_reader, "read_answers", lambda exam, s: sheet(name="ดี"))
    file = answer_reader.AnswerSheetFile(name="big.pdf", pages=10, images=[b"x"], truncated=True)
    s = class_session.read_sheet(EXAM, file)
    assert s.name == "ดี" and "อ่านเฉพาะ" in s.import_notes[0]


def test_normalize_no():
    assert [class_session.normalize_no(n) for n in [" 07 ", "๑๒", "12/3", ""]] == ["7", "12", "12/3", ""]


def test_roster_names_replace_misread_names_and_flag_mismatches():
    roster = {"1": "นายสมชาย ใจดี", "2": "นางสาวสมศรี มีสุข"}
    tone_marks = class_session.student_from_sheet(EXAM, "a.png", sheet(name="นายสมขาย ใจดื", no="๐๑"))
    wrong_no = class_session.student_from_sheet(EXAM, "b.png", sheet(name="ด.ช.ปิติ ชูใจ", no="2"))
    unknown = class_session.student_from_sheet(EXAM, "c.png", sheet(name="", no="9"))
    blank = class_session.student_from_sheet(EXAM, "d.png", sheet(name="", no=""))
    a, b, c, d = class_session.apply_roster([tone_marks, wrong_no, unknown, blank], roster)
    assert (a.student_no, a.name) == ("1", "นายสมชาย ใจดี")
    assert not [n for n in a.import_notes if not n.startswith("ข้อ ")]  # อ่านวรรณยุกต์ผิดเล็กน้อย ไม่ต้องเตือน
    assert b.name == "นางสาวสมศรี มีสุข" and "ไม่ตรงกับเลขที่ 2" in b.import_notes[0]
    assert c.name == "c" and c.import_notes[0] == "เลขที่ 9 ไม่อยู่ในรายชื่อห้อง"
    assert "ไม่มีเลขที่" in d.import_notes[0]
    assert class_session.apply_roster([tone_marks], {}) == [tone_marks]  # ไม่มีรายชื่อ = ไม่เปลี่ยน

    session, _, _ = class_session.merge_students(class_session.new_session(EXAM), [a, b])
    assert class_session.absent(session, {**roster, "3": "นายสาม"}) == ["3"]
