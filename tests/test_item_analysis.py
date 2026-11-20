"""วิเคราะห์ข้อสอบจากผลสอบจริง: ค่า p, r, ตัวลวง, KR-20/alpha (ค่าที่คาดหวังคำนวณด้วยมือ) และ Item Analyst (LLM จำลอง)"""

import dataclasses
import re

import pytest

from src import class_session, item_analysis, item_analyst, scoring
from src.schemas import Exam, ItemAnalystReport, ItemDiagnosis, Question, RubricCriterion, StudentRecord

COMMON = dict(concept="c", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium", explanation="e",
              model_answer="", key_points=[], rubric=[], points=1)
Q1 = Question(**COMMON, id=1, type="mcq", question="q1", choices=["a", "b", "c", "d"], correct_choice=0)
Q2 = Question(**COMMON, id=2, type="mcq", question="q2", choices=["a", "b", "c", "d"], correct_choice=1)
Q3 = Question(**COMMON, id=3, type="true_false", question="q3", choices=["ถูก", "ผิด"], correct_choice=0)
Q4 = Question(**{**COMMON, "points": 4, "model_answer": "ตัวอย่าง",
                 "rubric": [RubricCriterion(name="เนื้อหา", description="d", max_points=4)]},
              id=4, type="essay", question="q4", choices=[], correct_choice=None)
EXAM = Exam(title="ชุดวิเคราะห์", spec_summary="", concepts=["c"], questions=[Q1, Q2, Q3, Q4])

# (ข้อ 1, ข้อ 2, ข้อ 3, คะแนนเรียงความ) — คะแนนรวม 6 5 5 5 4 3 3 1 1 1
# ข้อ 2 ออกแบบให้กลุ่มเก่งตอบผิด (ค) แต่กลุ่มอ่อนตอบถูก → อำนาจจำแนกติดลบ
ROWS = [(0, 2, 0, 4), (0, 2, 0, 3), (0, 2, 0, 3), (0, 1, 0, 2), (0, 1, 1, 2),
        (1, 1, 0, 1), (0, 1, 1, 1), (2, 1, 1, 0), (1, 1, None, 0), (1, 1, 1, 0)]


def graded(exam: Exam, rows) -> list[StudentRecord]:
    students = []
    for n, (a1, a2, a3, essay_points) in enumerate(rows, start=1):
        student = class_session.grade_student(  # ปรนัยตรวจด้วยโค้ด
            exam.model_copy(update={"questions": [q for q in exam.questions if q.type != "essay"]}),
            StudentRecord(student_no=str(n), name=f"ผู้สอบ {n}", answers={1: a1, 2: a2, 3: a3}, source="csv"),
        )
        essay = scoring.unanswered_result(Q4).model_copy(update={"points_awarded": essay_points})
        students.append(student.model_copy(update={"results": student.results + [essay]}))
    return students


def test_difficulty_discrimination_distractors_and_alpha():
    stats = item_analysis.analyze(EXAM, graded(EXAM, ROWS) + [
        StudentRecord(name="ยังไม่ตรวจ", answers={}, source="csv")])  # คนที่ยังไม่ตรวจไม่นับ
    assert (stats.n, stats.group_size, stats.mean_percent, stats.sd) == (10, 3, 48.6, 1.8)
    q1, q2, q3, q4 = stats.items
    assert [(i.p, i.r) for i in stats.items] == [(0.6, 1.0), (0.7, -1.0), (0.5, 1.0), (0.4, 0.83)]
    assert [i.p_label for i in stats.items] == ["ปานกลาง", "ค่อนข้างง่าย", "ปานกลาง", "ปานกลาง"]
    assert [(c.total, c.upper, c.lower) for c in q1.choices] == [(6, 3, 0), (3, 0, 2), (1, 0, 1), (0, 0, 0)]
    assert q1.flags == ["ตัวลวง “ง. d” ไม่มีคนเลือก"]
    assert q2.r_label == "ติดลบ" and q2.flags[0].startswith("อำนาจจำแนกติดลบ")
    assert "ตัวลวง “ค. c” ดึงกลุ่มเก่งได้มากกว่ากลุ่มอ่อน" in q2.flags
    assert [(c.label, c.total) for c in q3.choices] == [("ถูก", 5), ("ผิด", 4), ("(ไม่ได้ตอบ)", 1)]
    assert q3.flags == [] and q4.flags == [] and q4.choices == []
    # alpha = 4/3 × (1 − (0.24 + 0.21 + 0.25 + 1.84) / 3.24)
    assert (stats.reliability_name, stats.reliability) == ("Cronbach's alpha", 0.29)
    assert [i.question_id for i in stats.flagged] == [1, 2]
    assert "น้อยกว่า 20" in stats.warnings[0]


def test_objective_only_uses_kr20_and_edge_cases():
    exam = EXAM.model_copy(update={"questions": [Q1, Q2, Q3]})
    stats = item_analysis.analyze(exam, graded(exam, ROWS))
    assert stats.reliability_name == "KR-20"
    assert item_analysis.analyze(exam, graded(exam, ROWS[:1])) is None  # ตรวจแล้วคนเดียว วิเคราะห์ไม่ได้
    same = item_analysis.analyze(exam, graded(exam, [(0, 1, 0, 0), (0, 1, 0, 0)]))
    assert same.reliability is None and any("ความเชื่อมั่น" in w for w in same.warnings)
    assert same.items[0].flags[0].startswith("ง่ายมาก")


class FakeAnalyst:
    def __init__(self, items):
        self.items, self.prompts = items, []

    def __call__(self, *, system, content, schema, effort):
        assert schema is ItemAnalystReport
        self.prompts.append(content[-1]["text"])
        return ItemAnalystReport(summary="สรุป", items=self.items)


def test_item_analyst_sees_only_flagged_items_and_fills_gaps(monkeypatch):
    stats = item_analysis.analyze(EXAM, graded(EXAM, ROWS))
    fake = FakeAnalyst([
        ItemDiagnosis(question_id=2, diagnosis="เฉลยน่าจะผิด กลุ่มเก่งเลือก ค ทั้งหมด", action="rewrite",
                      suggestion="ตรวจเฉลยใหม่"),
        ItemDiagnosis(question_id=4, diagnosis="ไม่ได้ส่งไป", action="revise", suggestion=""),  # ต้องถูกตัดทิ้ง
    ])
    monkeypatch.setattr(item_analyst, "call_structured", fake)
    report = item_analyst.analyze_items(EXAM, stats)
    assert re.findall(r'<item id="(\d+)"', fake.prompts[0]) == ["1", "2"]
    assert 'key="true" total="7" upper="0" lower="3"' in fake.prompts[0]  # เฉลยข้อ 2 กลุ่มอ่อนเลือกหมด
    assert [(d.question_id, d.action) for d in report.items] == [(1, "revise"), (2, "rewrite")]
    assert report.items[0].diagnosis == "ตัวลวง “ง. d” ไม่มีคนเลือก"  # agent ตกหล่น ใช้ข้อสังเกตของโค้ด

    issues, suggestions = item_analyst.revision_request(report, stats)
    assert sorted(issues) == [1, 2] and issues[2][0] == "จากผลสอบจริง: เฉลยน่าจะผิด กลุ่มเก่งเลือก ค ทั้งหมด"
    assert suggestions[2] == "เขียนข้อใหม่ทั้งข้อ โดยวัดหัวข้อเดิม: ตรวจเฉลยใหม่"
    kept = report.model_copy(update={"items": [d.model_copy(update={"action": "keep"}) for d in report.items]})
    assert item_analyst.revision_request(kept, stats) == ({}, {})


def test_no_flagged_items_skips_the_llm(monkeypatch):
    monkeypatch.setattr(item_analyst, "call_structured", lambda **_: pytest.fail("ไม่ควรเรียก LLM"))
    stats = item_analysis.analyze(EXAM, graded(EXAM, ROWS))
    clean = dataclasses.replace(stats, items=[i for i in stats.items if not i.flags])
    assert item_analyst.analyze_items(EXAM, clean).items == []
