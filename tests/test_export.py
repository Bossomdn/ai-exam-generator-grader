from datetime import date

import pytest

from src import export, scoring
from src.schemas import (
    CriterionScore, Exam, LearningReport, Question, RubricCriterion, SubjectiveGrade, WeakTopic,
)

COMMON = dict(bloom_level="understand", difficulty="medium")


def make_exam() -> Exam:
    return Exam(
        title="แบบทดสอบ <ML>",
        spec_summary="",
        concepts=["Overfitting", "Metrics"],
        questions=[
            Question(id=1, type="mcq", concept="Overfitting", source_ref="สไลด์ 4", points=1,
                     question="ข้อใดคือ <b>overfitting</b>", choices=["ก1", "ข2", "ค3", "ง4"], correct_choice=2,
                     explanation="EXPLAIN_MCQ", model_answer="", key_points=[], rubric=[], **COMMON),
            Question(id=2, type="true_false", concept="Metrics", source_ref="สไลด์ 5", points=1,
                     question="Recall = TP/(TP+FN)", choices=["ถูก", "ผิด"], correct_choice=0,
                     explanation="EXPLAIN_TF", model_answer="", key_points=[], rubric=[], **COMMON),
            Question(id=3, type="essay", concept="Metrics", source_ref="สไลด์ 5", points=10,
                     question="อธิบาย precision กับ recall", choices=[], correct_choice=None,
                     explanation="", model_answer="MODEL_ANSWER", key_points=[],
                     rubric=[RubricCriterion(name="เนื้อหา", description="RUBRIC_DESC", max_points=10)], **COMMON),
        ],
    )


def test_exam_html_has_no_answers_and_escapes_text():
    html = export.exam_html(make_exam())
    for secret in ("EXPLAIN_MCQ", "EXPLAIN_TF", "MODEL_ANSWER", "RUBRIC_DESC"):
        assert secret not in html
    assert "&lt;b&gt;overfitting&lt;/b&gt;" in html and "<b>overfitting</b>" not in html
    assert "ตอนที่ 1 ปรนัย (2 ข้อ 2 คะแนน)" in html
    assert "ตอนที่ 2 อัตนัย (1 ข้อ 10 คะแนน)" in html
    assert "ค. ค3" in html
    assert html.count('<div class="lines">') == 1


def test_exam_html_numbers_sections_when_only_subjective():
    exam = make_exam()
    exam.questions = [q for q in exam.questions if q.type == "essay"]
    html = export.exam_html(exam)
    assert "ตอนที่ 1 อัตนัย" in html and "ปรนัย" not in html


def test_answer_key_html_contains_key_and_rubric():
    html = export.answer_key_html(make_exam())
    assert "ค. ค3" in html and "EXPLAIN_MCQ" in html
    assert "MODEL_ANSWER" in html and "RUBRIC_DESC" in html


def test_report_html_marks_weak_topics_with_text_and_strips_numbering():
    exam = make_exam()
    q = {x.id: x for x in exam.questions}
    results = [
        scoring.grade_objective(q[1], 2),
        scoring.grade_objective(q[2], 1),
        scoring.finalize_subjective(q[3], SubjectiveGrade(
            criteria=[CriterionScore(criterion="เนื้อหา", points_awarded=4, max_points=10, evidence="", comment="")],
            missed_key_points=[], misconceptions=[], feedback="FEEDBACK"), "คำตอบ"),
    ]
    mastery = scoring.concept_mastery(results)
    report = LearningReport(
        overall_summary="SUMMARY", strengths=[], answer_writing_tips=[], practice_prompt="",
        weak_topics=[WeakTopic(concept="Metrics", why="WHY", review=["สไลด์ 5"], actions=["ACT"])],
        study_plan=["1. ทบทวน recall", "2) ฝึกคำนวณ"],
    )
    html = export.report_html(exam, results, mastery, report, "นาย ก", on=date(2026, 10, 14))
    assert "14 ต.ค. 2569" in html and "นาย ก" in html
    assert "ต้องเรียนเพิ่ม" in html and "ผ่าน" in html
    assert "<li>ทบทวน recall</li>" in html and "<li>ฝึกคำนวณ</li>" in html
    assert "FEEDBACK" in html and "SUMMARY" in html


def test_report_html_without_advisor_report():
    exam = make_exam()
    results = [scoring.unanswered_result(exam.questions[2])]
    html = export.report_html(exam, results, scoring.concept_mastery(results), None)
    assert "รายละเอียดรายข้อ" in html and "สรุปและคำแนะนำ" not in html


def test_html_to_pdf_produces_pdf():
    try:
        pdf = export.html_to_pdf(export.exam_html(make_exam()))
    except export.ExportError as e:
        pytest.skip(f"ไม่มีเบราว์เซอร์สำหรับสร้าง PDF: {e}")
    assert pdf.startswith(b"%PDF")
