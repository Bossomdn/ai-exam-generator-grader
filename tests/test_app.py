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
import src.llm
import src.planner
import src.reviewer
from src import answer_reader
from src.schemas import (
    CriterionScore, Exam, ExamReview, ExamSpec, ExtractedAnswer, ExtractedAnswerSheet, LearningReport, Question,
    QuestionReview, RevisedQuestions, RubricCriterion, SectionSpec, SubjectiveGrade, WeakTopic,
)

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
    raise AssertionError(f"ไม่ได้เตรียมคำตอบของ {schema.__name__}")


@pytest.fixture
def app(monkeypatch) -> AppTest:
    monkeypatch.setattr(src.llm, "check_ready", lambda: (True, "พร้อม (จำลอง)"))
    for module in (src.planner, src.generator, src.reviewer, src.grader, src.advisor):
        monkeypatch.setattr(module, "call_structured", fake_llm)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def click(at: AppTest, label: str) -> None:
    [b for b in at.button if b.label == label][0].click().run()
    assert not at.exception, at.exception


def test_create_take_grade_and_practice(app):
    at = app
    [r for r in at.radio if r.label == "แหล่งเนื้อหา"][0].set_value("ใช้เนื้อหาตัวอย่าง").run()
    click(at, "วิเคราะห์คำสั่ง")
    assert [t.value for t in at.text_input if t.key and t.key.startswith("spec_title")] == ["ML เบื้องต้น"]
    assert any("ระบบจึงตั้งค่าให้" in i.value for i in at.info)

    click(at, "สร้างข้อสอบตามสเปกนี้")
    exam = at.session_state["exam"]
    assert [(q.type, q.points) for q in exam.questions] == [("mcq", 1), ("mcq", 1), ("essay", 10)]
    assert {k: v.status for k, v in at.session_state["quality"].items()} == {1: "passed", 2: "fixed", 3: "passed"}
    assert any("แก้ตามผลตรวจแล้วผ่าน 1 ข้อ" in m.value for m in at.markdown)

    at.text_area[0].set_value("ปรนัย 3 ข้อ").run()  # เปลี่ยนคำสั่ง → สเปกเดิมใช้ไม่ได้แล้ว
    assert any("เปลี่ยนไปแล้ว" in i.value for i in at.info)

    v = at.session_state["version"]
    at.session_state[f"ans_{v}_1"] = 0
    at.session_state[f"ans_{v}_3"] = "overfitting คือ..."
    at.run()
    click(at, "ส่งคำตอบให้ตรวจ")
    assert [(r.question_id, r.points_awarded) for r in at.session_state["results"]] == [(1, 1), (2, 0), (3, 2)]
    assert any("ต้องเรียนเพิ่ม" in p.proto.text for p in at.get("progress"))

    click(at, "สร้างแบบฝึกเพิ่มเรื่องที่อ่อน")
    assert at.session_state["version"] == v + 1 and at.session_state["results"] is None
    assert at.session_state["trace"][0].agent == "Request Planner"


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_uploaded_sheet_shows_review_warnings(app):
    at = app
    exam = Exam(title="ชุดทดสอบ", spec_summary="", concepts=["Supervised", "Overfitting"],
                questions=[mcq(1), mcq(2), ESSAY])
    extracted = ExtractedAnswerSheet(student_name="นางสาวทดสอบ", answers=[
        ExtractedAnswer(question_id=1, answer="ข", choice_text="ค1", confidence="high", note=""),  # ตัวอักษรไม่ตรงข้อความ
        ExtractedAnswer(question_id=3, answer="คำตอบ", choice_text="", confidence="low", note="ลายมือไม่ชัด"),
    ])  # ข้อ 2 หายไป
    at.session_state["exam"] = exam
    at.session_state["version"] = 1
    at.session_state["answer_mode"] = "อัปโหลดกระดาษคำตอบ (PDF / รูปถ่าย)"
    at.session_state["sheet"] = {"version": 1, "result": extracted,
                                 "file": answer_reader.AnswerSheetFile(name="scan.png", pages=1, images=[_png()])}
    at.run()
    assert not at.exception, at.exception
    warnings = " | ".join(w.value for w in at.warning)
    assert "ไม่ตรงกับข้อความตัวเลือก" in warnings
    assert "ไม่พบคำตอบข้อนี้ในไฟล์" in warnings
    assert "ลายมือไม่ชัด" in warnings
