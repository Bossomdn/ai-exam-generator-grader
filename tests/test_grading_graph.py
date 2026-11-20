"""คณะผู้ตรวจเรียงความ (LangGraph): ผู้ตรวจ 3 มุมมอง → เทียบคะแนน → Moderator เมื่อเห็นต่าง (LLM จำลอง)"""

import threading

import pytest

from src import config, grader, grading_graph
from src.schemas import CriterionScore, Exam, ModeratedGrade, Question, RubricCriterion, SubjectiveGrade

COMMON = dict(concept="Overfitting", source_ref="สไลด์ 3", bloom_level="analyze", difficulty="hard",
              explanation="", model_answer="ตัวอย่าง", key_points=[], choices=[], correct_choice=None)
ESSAY = Question(**COMMON, id=2, type="essay", question="อธิบาย overfitting", points=10,
                 rubric=[RubricCriterion(name="เนื้อหา", description="d", max_points=10)])
SHORT = Question(**{**COMMON, "key_points": ["ข้อมูลมีป้ายกำกับ"]}, id=3, type="short_answer",
                 question="supervised คืออะไร", points=2, rubric=[])
MCQ = Question(**{**COMMON, "choices": ["a", "b", "c", "d"], "correct_choice": 1}, id=1, type="mcq",
               question="q", points=1, rubric=[])
ANSWER = "โมเดลจำข้อมูลฝึกมากเกินไป จึงทำนายข้อมูลใหม่ได้แย่"


class FakePanel:
    """ผู้ตรวจแต่ละมุมมองให้คะแนนตามที่กำหนด และบันทึกว่าถูกเรียกด้วยอะไร"""

    def __init__(self, points: dict, moderator_points: float = 5, moderator_evidence: str = "จำข้อมูลฝึก"):
        self.points, self.moderator_points, self.moderator_evidence = points, moderator_points, moderator_evidence
        self.calls: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, *, system, content, schema, effort):
        text = content[-1]["text"]
        with self.lock:
            self.calls.append((schema.__name__, system, text))
        if schema is SubjectiveGrade:
            perspective = next((p for p, focus in grader.PERSPECTIVES.items() if focus in system), None)
            return SubjectiveGrade(
                criteria=[CriterionScore(criterion="เนื้อหา", points_awarded=self.points[perspective], max_points=10,
                                         evidence="จำข้อมูลฝึก", comment=f"มุมมอง {perspective}")],
                missed_key_points=[], misconceptions=[], feedback=f"fb {perspective}",
            )
        assert schema is ModeratedGrade
        return ModeratedGrade(
            rationale="เห็นด้วยกับผู้ตรวจด้านเหตุผล", missed_key_points=["ยกตัวอย่าง"], misconceptions=[],
            feedback="fb moderator",
            criteria=[CriterionScore(criterion="เนื้อหา", points_awarded=self.moderator_points, max_points=10,
                                     evidence=self.moderator_evidence, comment="")],
        )

    def count(self, schema: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == schema)


@pytest.fixture
def panel(monkeypatch):
    def install(points, **kwargs) -> FakePanel:
        fake = FakePanel(points, **kwargs)
        monkeypatch.setattr(grader, "call_structured", fake)
        return fake
    monkeypatch.setattr(config, "GRADER_MODE", "panel")
    return install


def test_agreeing_panel_takes_the_middle_grader_without_moderator(panel):
    fake = panel({"เนื้อหา": 6, "เหตุผล": 7, "เกณฑ์": 5})  # ต่างกัน 2 = 20% ของ 10 คะแนน
    result = grading_graph.grade_answer(ESSAY, ANSWER)
    assert (result.points_awarded, result.feedback) == (6, "fb เนื้อหา")
    assert (result.panel.decision, result.panel.spread, result.panel.needs_teacher) == ("agreed", 2, False)
    assert [(g.grader, g.points) for g in result.panel.grades] == [("เนื้อหา", 6), ("เหตุผล", 7), ("เกณฑ์", 5)]
    assert (fake.count("SubjectiveGrade"), fake.count("ModeratedGrade")) == (3, 0)
    for focus in grader.PERSPECTIVES.values():  # ผู้ตรวจแต่ละคนได้มุมมองของตัวเองคนละครั้ง
        assert sum(focus in system for _, system, _ in fake.calls) == 1


def test_disagreement_is_settled_by_moderator(panel):
    fake = panel({"เนื้อหา": 3, "เหตุผล": 6, "เกณฑ์": 5}, moderator_points=5)
    result = grading_graph.grade_answer(ESSAY, ANSWER)
    assert (result.points_awarded, result.feedback, result.missed_key_points) == (5, "fb moderator", ["ยกตัวอย่าง"])
    assert (result.panel.decision, result.panel.spread, result.panel.needs_teacher) == ("moderated", 3, False)
    assert result.panel.rationale == "เห็นด้วยกับผู้ตรวจด้านเหตุผล"
    moderator_prompt = [text for name, _, text in fake.calls if name == "ModeratedGrade"][0]
    assert all(f'<grader perspective="{p}">' in moderator_prompt for p in grader.PERSPECTIVES)
    assert ANSWER in moderator_prompt


def test_large_disagreement_is_flagged_for_teacher_and_moderator_is_checked(panel):
    panel({"เนื้อหา": 1, "เหตุผล": 9, "เกณฑ์": 5}, moderator_points=12, moderator_evidence="ข้อความที่ไม่มีจริง")
    result = grading_graph.grade_answer(ESSAY, ANSWER)
    assert result.panel.needs_teacher and result.panel.spread == 8
    assert result.points_awarded == 10  # โค้ดจำกัดไม่ให้เกินคะแนนเต็ม
    assert result.unverified_evidence == ["ข้อความที่ไม่มีจริง"]


def test_single_mode_short_answers_and_blank_answers_use_fewer_calls(panel, monkeypatch):
    fake = panel({None: 1.5})
    short = grading_graph.grade_answer(SHORT, "ข้อมูลมีเฉลย")
    assert short.panel is None and fake.count("SubjectiveGrade") == 1  # ตอบสั้นใช้ผู้ตรวจคนเดียว
    assert grading_graph.grade_answer(ESSAY, "   ").points_awarded == 0 and len(fake.calls) == 1  # ไม่ได้ตอบ ไม่เรียก LLM

    monkeypatch.setattr(config, "GRADER_MODE", "single")
    fake = panel({None: 4})
    result = grading_graph.grade_answer(ESSAY, ANSWER)
    assert (result.points_awarded, result.panel, len(fake.calls)) == (4, None, 1)


def test_grade_exam_mixes_code_and_panel(panel):
    fake = panel({"เนื้อหา": 8, "เหตุผล": 8, "เกณฑ์": 8, None: 2})
    exam = Exam(title="t", spec_summary="", concepts=["Overfitting"], questions=[MCQ, ESSAY, SHORT])
    results = grading_graph.grade_exam(exam, {1: 1, 2: ANSWER, 3: "ข้อมูลมีป้ายกำกับ"})
    assert [(r.question_id, r.points_awarded) for r in results] == [(1, 1), (2, 8), (3, 2)]
    assert fake.count("SubjectiveGrade") == 4  # เรียงความ 3 + ตอบสั้น 1
