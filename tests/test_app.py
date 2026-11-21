"""ทดสอบหน้าเว็บทั้ง flow ด้วย Streamlit AppTest + LLM จำลอง (ไม่ต้องมีโมเดล)"""

import io
import re
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.advisor
import src.generator
import src.grader
import src.item_analyst
import src.llm
import src.planner
import src.reviewer
from src import answer_reader, class_session, csv_import, school_db
from src.schemas import (
    ClassSession, CriterionScore, Exam, ExamReview, ExamSpec, ExtractedAnswer, ExtractedAnswerSheet, ItemAnalystReport,
    ItemDiagnosis, LearningReport,
    Question, QuestionReview, RevisedQuestions, RosterEntry, RubricCriterion, SectionSpec, StudentRecord,
    SubjectiveGrade, WeakTopic,
)
from ui import nav

APP = str(Path(__file__).parent.parent / "app.py")
SPEC = ExamSpec(
    title="ML เบื้องต้น", focus="เน้นความเข้าใจ", concepts=["Supervised", "Overfitting"],
    assumptions=["ไม่ได้ระบุความยาก จึงใช้ระดับกลาง"],
    sections=[SectionSpec(type="essay", count=1, points_each=10, difficulty="hard"),
              SectionSpec(type="mcq", count=2, points_each=1, difficulty="medium")],
)
COMMON = dict(concept="Supervised", source_ref="สไลด์ 2", bloom_level="understand", difficulty="medium", points=1,
              explanation="e", model_answer="", key_points=[], rubric=[])


def mcq(i: int, correct: int = 0) -> Question:
    return Question(**{**COMMON, "id": i, "type": "mcq", "question": f"คำถาม {i}",
                       "choices": ["ก1", "ข1", "ค1", "ง1"], "correct_choice": correct})


ESSAY = Question(**{**COMMON, "id": 3, "type": "essay", "question": "อธิบาย overfitting", "choices": [],
                    "correct_choice": None, "model_answer": "ตัวอย่าง", "concept": "Overfitting",
                    "rubric": [RubricCriterion(name="เนื้อหา", description="d", max_points=4)]})


def fake_llm(*, system, content, schema, effort):
    text = content[-1]["text"]
    if schema is ExamSpec:
        return SPEC
    if schema is Exam:  # ข้อ 2 เฉลยผิด → Reviewer ต้องส่งกลับแก้
        return Exam(title="x", spec_summary="", concepts=[], questions=[mcq(1), mcq(2, correct=2), ESSAY])
    if schema is ExamReview:
        ids = [int(i) for i in re.findall(r'<question id="(\d+)"', text)]
        return ExamReview(reviews=[QuestionReview(question_id=i, answer="ก" if i < 3 else "", answer_text="",
                                                  problems=[], suggestion="") for i in ids])
    if schema is RevisedQuestions:
        ids = [int(i) for i in re.findall(r'<question_to_fix id="(\d+)"', text)]
        return RevisedQuestions(questions=[mcq(i, correct=0) for i in ids])
    if schema is SubjectiveGrade:
        return SubjectiveGrade(criteria=[CriterionScore(criterion="เนื้อหา", points_awarded=2, max_points=4,
                                                        evidence="", comment="")],
                               missed_key_points=[], misconceptions=[], feedback="fb")
    if schema is LearningReport:
        return LearningReport(
            overall_summary="สรุป", strengths=[], answer_writing_tips=[], study_plan=["ทบทวน"],
            weak_topics=[WeakTopic(concept="Overfitting", why="w", review=["สไลด์ 2"], actions=["a"])],
            practice_prompt="ปรนัย 3 ข้อ เรื่อง Overfitting",
        )
    if schema is ItemAnalystReport:  # ข้อแรกที่ส่งมาให้แก้ ข้ออื่นใช้ต่อ
        ids = [int(i) for i in re.findall(r'<item id="(\d+)"', text)]
        return ItemAnalystReport(summary="บางข้อควรปรับ", items=[
            ItemDiagnosis(question_id=i, diagnosis=f"วินิจฉัยข้อ {i}", action="keep" if n else "revise",
                          suggestion="" if n else "เปลี่ยนตัวลวงที่ไม่มีคนเลือก")
            for n, i in enumerate(ids)])
    raise AssertionError(f"ไม่ได้เตรียมคำตอบของ {schema.__name__}")


@pytest.fixture
def app(monkeypatch) -> AppTest:
    monkeypatch.setattr(src.llm, "check_ready", lambda: (True, "พร้อม (จำลอง)"))
    for module in (src.planner, src.generator, src.reviewer, src.grader, src.advisor, src.item_analyst):
        monkeypatch.setattr(module, "call_structured", fake_llm)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def click(at: AppTest, label: str) -> None:
    [b for b in at.button if b.label == label][0].click().run()
    assert not at.exception, at.exception


def go(at: AppTest, page: str) -> None:
    """สลับไปหน้าในเมนู เช่น go(at, nav.CLASS)
    AppTest จำหน้าปัจจุบันเอง: ถ้าแอปสั่ง st.switch_page ระหว่างรัน ต้องเรียก go() ตามก่อนกดปุ่มในหน้าใหม่"""
    at.switch_page(page).run()
    assert not at.exception, at.exception


def page_links(at: AppTest) -> list[str]:
    """หน้าที่มีลิงก์ให้กดไป (แปลง url ของหน้า เช่น "class_exam" กลับเป็นไฟล์ใน views/ หน้าเริ่มต้นมี url ว่าง)"""
    return [f"views/{link.proto.page}.py" if link.proto.page else nav.CREATE for link in at.get("page_link")]


def test_create_take_grade_and_practice(app):
    at = app
    [r for r in at.radio if r.label == "แหล่งเนื้อหา"][0].set_value("ใช้เนื้อหาตัวอย่าง").run()
    assert [b for b in at.button if b.label == "วิเคราะห์คำสั่ง"][0].disabled  # ยังไม่มีคำสั่ง
    at.text_area(key="create_prompt").set_value("ขอข้อสอบผสม").run()
    click(at, "วิเคราะห์คำสั่ง")
    assert [t.value for t in at.text_input if t.key and t.key.startswith("spec_title")] == ["ML เบื้องต้น"]
    assert any("ระบบจึงตั้งค่าให้" in i.value for i in at.info)

    at.text_area(key="create_prompt").set_value("ปรนัย 3 ข้อ").run()  # เปลี่ยนคำสั่ง → สเปกเดิมใช้ไม่ได้แล้ว
    assert any("เปลี่ยนไปแล้ว" in i.value for i in at.info)
    at.text_area(key="create_prompt").set_value("ขอข้อสอบผสม").run()

    click(at, "สร้างข้อสอบตามสเปกนี้")
    exam = at.session_state["exam"]
    assert [(q.type, q.points) for q in exam.questions] == [("mcq", 1), ("mcq", 1), ("essay", 10)]
    assert {k: v.status for k, v in at.session_state["quality"].items()} == {1: "passed", 2: "fixed", 3: "passed"}
    assert any("แก้ตามผลตรวจแล้วผ่าน 1 ข้อ" in m.value for m in at.markdown)
    assert not [b for b in at.button if b.label == "วิเคราะห์คำสั่ง"]  # สร้างเสร็จ: ข้อสอบขึ้นก่อน ฟอร์มซ่อน
    assert {nav.CLASS, nav.PRACTICE} <= set(page_links(at))

    go(at, nav.PRACTICE)
    v = at.session_state["version"]
    at.session_state[f"ans_{v}_1"] = 0
    at.session_state[f"ans_{v}_3"] = "overfitting คือ..."
    at.run()
    go(at, nav.CREATE)  # สลับหน้าแล้วกลับมา คำตอบต้องยังอยู่
    go(at, nav.PRACTICE)
    assert at.session_state[f"ans_{v}_3"] == "overfitting คือ..."
    assert any("ตอบแล้ว 2/3 ข้อ" in c.value for c in at.caption)

    click(at, "ส่งคำตอบให้ตรวจ")  # ตรวจเสร็จแล้วไปหน้าผลเอง
    assert [(r.question_id, r.points_awarded) for r in at.session_state["results"]] == [(1, 1), (2, 0), (3, 2)]
    assert any("ต้องเรียนเพิ่ม" in p.proto.text for p in at.get("progress"))
    assert any("คณะผู้ตรวจ" in c.value and "เห็นตรงกัน" in c.value for c in at.caption)  # เรียงความตรวจด้วยคณะ

    go(at, nav.FEEDBACK)
    click(at, "สร้างแบบฝึกเพิ่มเรื่องที่อ่อน")
    assert at.session_state["version"] == v + 1 and at.session_state["results"] is None
    assert at.session_state["trace"][0].agent == "Request Planner"
    assert any(b.label == "ส่งคำตอบให้ตรวจ" for b in at.button)  # พาไปหน้าทำแบบฝึก


def test_pages_without_data_point_to_the_step_before(app):
    at = app
    expected = {nav.PRACTICE: nav.CREATE, nav.FEEDBACK: nav.PRACTICE, nav.CLASS: nav.ROOMS, nav.SCORES: nav.ROOMS}
    for page, before in expected.items():
        go(at, page)
        assert before in page_links(at), page
    add_room()
    go(at, nav.CLASS)  # มีห้องแล้วแต่ยังไม่มีข้อสอบ
    assert nav.CREATE in page_links(at)
    go(at, nav.BACKUP)
    assert any(b.label == "สร้างไฟล์สำรอง" for b in at.button)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_uploaded_sheet_shows_review_warnings(app):
    at = app
    exam = Exam(title="ชุดทดสอบ", spec_summary="", concepts=["Supervised", "Overfitting"],
                questions=[mcq(1), mcq(2), ESSAY])
    extracted = ExtractedAnswerSheet(student_name="นางสาวทดสอบ", student_no="", answers=[
        ExtractedAnswer(question_id=1, answer="ข", choice_text="ค1", confidence="high", note=""),  # ตัวอักษรไม่ตรงข้อความ
        ExtractedAnswer(question_id=3, answer="คำตอบ", choice_text="", confidence="low", note="ลายมือไม่ชัด"),
    ])  # ข้อ 2 หายไป
    at.session_state["exam"] = exam
    at.session_state["version"] = 1
    at.session_state["answer_mode"] = "อัปโหลดกระดาษคำตอบ (PDF / รูปถ่าย)"
    at.session_state["sheet"] = {"version": 1, "result": extracted,
                                 "file": answer_reader.AnswerSheetFile(name="scan.png", pages=1, images=[_png()])}
    go(at, nav.PRACTICE)
    warnings = " | ".join(w.value for w in at.warning)
    assert "ไม่ตรงกับข้อความตัวเลือก" in warnings
    assert "ไม่พบคำตอบข้อนี้ในไฟล์" in warnings
    assert "ลายมือไม่ชัด" in warnings


def add_room(roster: list[RosterEntry] = ()) -> school_db.Classroom:
    with school_db.connect() as conn:
        room = school_db.add_classroom(conn, 2569, "ม.4", "1")
        school_db.import_roster(conn, room.id, list(roster))
    return room


def start_class(at: AppTest, exam: Exam) -> None:
    at.session_state["exam"] = exam
    go(at, nav.CLASS)
    click(at, f"เริ่มเก็บคำตอบทั้งห้องด้วยข้อสอบ “{exam.title}”")
    assert at.session_state["class_sitting"].classroom.name == "ม.4/1"


def current_steps(at: AppTest) -> list[str]:
    """ขั้นที่แถบขั้นตอนบอกว่าต้องทำต่อ (badge สีน้ำเงิน)"""
    return [m.value for m in at.markdown if ":blue-badge[" in m.value]


def test_class_tab_imports_and_grades_whole_class(app):
    at = app
    exam = Exam(title="ชุดทั้งห้อง", spec_summary="", concepts=["Supervised", "Overfitting"],
                questions=[mcq(1), mcq(2), ESSAY])
    room = add_room()
    start_class(at, exam)
    assert any("นำเข้าคำตอบ" in s for s in current_steps(at))
    filled = csv_import.template_csv(exam).decode("utf-8-sig") + "1,นายเอ,ก,ค1,คำตอบ\n2,นางบี,,ข,\n"
    imported = csv_import.parse_responses(exam, filled.encode("utf-8"))
    at.session_state["class_session"] = class_session.merge_students(
        at.session_state["class_session"], imported.students)[0]
    at.run()
    assert not at.exception, at.exception
    assert any("ตรวจคะแนน" in s for s in current_steps(at))

    click(at, "ตรวจคำตอบที่ยังไม่ได้ตรวจ (2 คน)")
    assert any("ตรวจครบ 2 คนแล้ว" in s.value for s in at.success)
    assert any("วิเคราะห์ข้อสอบ" in s for s in current_steps(at))
    rows = class_session.score_rows(at.session_state["class_session"])
    # นายเอ: ข้อ 1 ถูก (1) + ข้อ 2 ตอบ ค (0) + เรียงความ (คะแนนเต็มข้อนี้ 1 จึงได้ไม่เกิน 1) = 2
    assert [(r["ชื่อ"], r["คะแนนรวม"], r["สถานะ"]) for r in rows] == [("นายเอ", 2, "ตรวจแล้ว"), ("นางบี", 0, "ตรวจแล้ว")]
    assert [r["ให้ครูดู"] for r in rows] == [0, 0]
    assert any("เฉลี่ย" in c.value for c in at.caption)
    with school_db.connect() as conn:  # บันทึกลงฐานข้อมูลแล้ว
        table = school_db.classroom_table(conn, room.id)
    assert [(r["ชื่อ"], r["เฉลี่ย"]) for r in table] == [("นายเอ", 66.7), ("นางบี", 0.0)]  # 2 จาก 3 คะแนน

    click(at, "ปิดการสอบนี้")
    assert at.session_state["class_session"] is None
    click(at, "เปิดการสอบนี้")
    assert [s.name for s in at.session_state["class_session"].students if s.results] == ["นายเอ", "นางบี"]


def test_class_tab_requires_review_of_scanned_sheets_before_grading(app):
    at = app
    exam = Exam(title="ชุดกระดาษ", spec_summary="", concepts=["Supervised", "Overfitting"],
                questions=[mcq(1), mcq(2), ESSAY])
    add_room()  # ไม่มีรายชื่อ = ครูพิมพ์ชื่อเอง
    start_class(at, exam)
    clean = class_session.student_from_sheet(exam, "a.png", ExtractedAnswerSheet(
        student_name="นายเอ", student_no="1", answers=[
            ExtractedAnswer(question_id=1, answer="ก", choice_text="ก1", confidence="high", note=""),
            ExtractedAnswer(question_id=2, answer="ก", choice_text="ก1", confidence="high", note=""),
            ExtractedAnswer(question_id=3, answer="ตอบ", choice_text="", confidence="high", note=""),
        ]))
    flagged = class_session.student_from_sheet(exam, "b.png", ExtractedAnswerSheet(student_name="", student_no="", answers=[
        ExtractedAnswer(question_id=1, answer="ข", choice_text="ค1", confidence="high", note=""),
    ]))
    at.session_state["class_session"] = class_session.merge_students(
        at.session_state["class_session"], [clean, flagged])[0]
    at.session_state["class_images"] = {"b.png": [_png()]}
    at.run()
    assert not at.exception, at.exception
    assert not [b for b in at.button if b.label.startswith("ตรวจคำตอบที่ยังไม่ได้ตรวจ")]  # ยังไม่มีใครผ่านการตรวจทาน

    click(at, "ยืนยันทุกคนที่ไม่มีคำเตือน (1 คน)")
    assert any("ตัวอักษรที่อ่านได้" in w.value for w in at.warning)  # คำเตือนรายข้อของคนที่เหลือ
    [t for t in at.text_input if t.label == "ชื่อ-นามสกุล"][0].set_value("นางบี")
    click(at, "ยืนยันคำตอบของคนนี้")
    students = at.session_state["class_session"].students
    assert [s.reviewed for s in students] == [True, True]
    assert (students[1].name, students[1].answers[1]) == ("นางบี", 1)

    click(at, "ตรวจคำตอบที่ยังไม่ได้ตรวจ (2 คน)")
    assert [r["สถานะ"] for r in class_session.score_rows(at.session_state["class_session"])] == ["ตรวจแล้ว", "ตรวจแล้ว"]


def test_review_with_roster_picks_student_from_list(app):
    at = app
    exam = Exam(title="ชุดรายชื่อ", spec_summary="", concepts=["Supervised"], questions=[mcq(1)])
    add_room([RosterEntry(number="1", name="นายเอ"), RosterEntry(number="2", name="นางสาวบี")])
    start_class(at, exam)
    sheets = [
        class_session.student_from_sheet(exam, "a.png", ExtractedAnswerSheet(student_name="นายเอ", student_no="1", answers=[
            ExtractedAnswer(question_id=1, answer="ก", choice_text="ก1", confidence="high", note="")])),
        class_session.student_from_sheet(exam, "b.png", ExtractedAnswerSheet(student_name="", student_no="", answers=[
            ExtractedAnswer(question_id=1, answer="ข", choice_text="ข1", confidence="high", note="")])),
    ]
    roster = {"1": "นายเอ", "2": "นางสาวบี"}
    at.session_state["class_session"] = class_session.merge_students(
        at.session_state["class_session"], class_session.apply_roster(sheets, roster))[0]
    at.run()
    assert not at.exception, at.exception
    assert any("ยังไม่มีคำตอบ 1 คน: เลขที่ 2" in c.value for c in at.caption)

    [s for s in at.selectbox if s.label == "เลือกผู้สอบที่จะตรวจทาน"][0].set_value(1).run()
    picker = [s for s in at.selectbox if s.label == "ผู้สอบ (จากรายชื่อห้อง)"][0]
    assert picker.value is None  # ไม่มีเลขที่ ต้องเลือกเอง
    picker.set_value("1")
    click(at, "ยืนยันคำตอบของคนนี้")
    assert any("มีคำตอบของ “นายเอ” อยู่แล้ว" in e.value for e in at.error)  # เลขที่ซ้ำกับใบอื่น

    [s for s in at.selectbox if s.label == "ผู้สอบ (จากรายชื่อห้อง)"][0].set_value("2")
    click(at, "ยืนยันคำตอบของคนนี้")
    student = at.session_state["class_session"].students[1]
    assert (student.student_no, student.name, student.reviewed) == ("2", "นางสาวบี", True)


def test_rooms_page_adds_several_rooms_in_a_row(app):
    at = app
    go(at, nav.ROOMS)
    at.text_input(key="new_room_grade").set_value("ม.4")
    at.text_input(key="new_room_numbers").set_value("1-3")
    click(at, "เพิ่มห้อง")
    assert any("เพิ่มห้อง ม.4/1, ม.4/2, ม.4/3" in s.value for s in at.success)
    # ชั้นยังอยู่ ช่องเลขห้องว่าง → แก้แค่เลขห้องแล้วเพิ่มต่อ (ห้อง 2 มีแล้ว ต้องบอกว่าข้าม)
    assert at.text_input(key="new_room_grade").value == "ม.4" and at.text_input(key="new_room_numbers").value == ""
    at.text_input(key="new_room_numbers").set_value("๒, 4")
    click(at, "เพิ่มห้อง")
    assert any("เพิ่มห้อง ม.4/4" in s.value for s in at.success)
    assert any("ม.4/2 มีอยู่แล้ว" in m.value for m in at.markdown)
    with school_db.connect() as conn:
        assert [r.name for r in school_db.classrooms(conn)] == ["ม.4/1", "ม.4/2", "ม.4/3", "ม.4/4"]
    assert any("ปีการศึกษา" in m.value and "4 ห้อง" in m.value for m in at.markdown)

    at.text_input(key="new_room_numbers").set_value("5-2")
    click(at, "เพิ่มห้อง")
    assert any("ช่วงห้อง" in e.value for e in at.error)


def test_school_pages_add_room_show_scores_and_back_up(app):
    at = app
    go(at, nav.ROOMS)
    at.text_input(key="new_room_grade").set_value("ม.4")
    at.text_input(key="new_room_numbers").set_value("2")
    click(at, "เพิ่มห้อง")
    with school_db.connect() as conn:
        [room] = school_db.classrooms(conn)
        exam = Exam(title="ชุดแท็บห้า", spec_summary="", concepts=["Supervised"], questions=[mcq(1)])
        student = StudentRecord(student_no="1", name="เอ", answers={1: 0}, source="csv")
        session = ClassSession(exam=exam, students=[class_session.grade_student(exam, student)])
        school_db.save_session(conn, school_db.open_sitting(conn, exam, room.id, 1), session)
    assert room.name == "ม.4/2" and at.session_state["school_room"] == room.id

    go(at, nav.SCORES)
    for view in ("รายห้อง", "รายชั้น"):
        [r for r in at.radio if r.label == "ดูคะแนน"][0].set_value(view).run()
        assert not at.exception, at.exception
        assert any("ชุดแท็บห้า" in str(d.value) for d in at.dataframe)
    [r for r in at.radio if r.label == "ดูคะแนน"][0].set_value("รายคน").run()
    [s for s in at.selectbox if s.label == "นักเรียน"][0].set_value(0).run()
    assert any("ชุดแท็บห้า" in str(d.value) for d in at.dataframe)

    go(at, nav.BACKUP)
    click(at, "สร้างไฟล์สำรอง")
    assert at.session_state["school_backup"].startswith(b"SQLite format 3")


def test_class_tab_item_analysis_revises_exam(app):
    at = app
    exam = Exam(title="ชุดวิเคราะห์", spec_summary="", concepts=["Supervised", "Overfitting"],
                questions=[mcq(1), mcq(2), ESSAY])
    add_room()
    start_class(at, exam)
    filled = csv_import.template_csv(exam).decode("utf-8-sig") + "1,เอ,ก,ก1,ตอบ\n2,บี,ข,ข,\n3,ซี,ก,ค,ตอบ\n"
    at.session_state["class_session"] = class_session.merge_students(
        at.session_state["class_session"], csv_import.parse_responses(exam, filled.encode("utf-8")).students)[0]
    at.run()
    click(at, "ตรวจคำตอบที่ยังไม่ได้ตรวจ (3 คน)")
    assert any("ผู้สอบที่ตรวจแล้ว **3 คน**" in m.value for m in at.markdown)

    [b for b in at.button if b.label.startswith("🔍 ให้ Item Analyst")][0].click().run()
    assert not at.exception, at.exception
    assert any("บางข้อควรปรับ" in i.value for i in at.info)
    [b for b in at.button if b.label.startswith("🛠️ ปรับปรุงข้อสอบตามคำแนะนำ (1 ข้อ)")][0].click().run()
    assert not at.exception, at.exception
    assert any("ก่อนแก้" in c.value for c in at.caption) and any("Reviewer ตรวจผ่าน" in m.value for m in at.markdown)

    click(at, "ใช้ข้อสอบฉบับปรับปรุง")
    assert at.session_state["exam"].title == "ชุดวิเคราะห์ (ฉบับปรับปรุงจากผลสอบ)"
    assert any("ใช้ฉบับปรับปรุงแล้ว" in s.value for s in at.success) and nav.CREATE in page_links(at)
    assert [s.agent for s in at.session_state["trace"]] == [
        "Item Analyst", "Question Generator (แก้ไข)", "Quality Reviewer"]
    assert at.session_state["class_session"].exam == exam  # ผลสอบเดิมยังผูกกับข้อสอบฉบับที่ใช้สอบจริง
