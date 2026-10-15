"""ทดสอบลำดับการทำงานของ agent ฝั่งสร้างข้อสอบ ด้วย LLM จำลอง (ไม่ต้องใช้โมเดลจริง)"""

import re

import pytest

from src import generation_graph, generator, planner, reviewer
from src.config import MAX_REVISIONS
from src.schemas import (
    Exam, ExamReview, ExamSpec, Question, QuestionReview, RevisedQuestions, RubricCriterion, SectionSpec,
)
from src.sources import load_text

SOURCE = load_text("[สไลด์ 1]\nเนื้อหา".encode("utf-8"), "lecture.txt")
SPEC = ExamSpec(
    title="ทดสอบ",
    sections=[SectionSpec(type="mcq", count=2, points_each=2, difficulty="medium"),
              SectionSpec(type="essay", count=1, points_each=10, difficulty="hard")],
    focus="", concepts=["A", "B"], assumptions=[],
)
COMMON = dict(concept="A", source_ref="สไลด์ 1", bloom_level="understand", difficulty="medium", points=1,
              explanation="", model_answer="", key_points=[], rubric=[])


def mcq(i: int, correct: int = 0, **kw) -> Question:
    return Question(**{**COMMON, "id": i, "type": "mcq", "question": f"คำถาม {i}",
                       "choices": ["w", "x", "y", "z"], "correct_choice": correct, **kw})


def essay(i: int) -> Question:
    rubric = [RubricCriterion(name="เนื้อหา", description="d", max_points=3),
              RubricCriterion(name="เหตุผล", description="d", max_points=2)]
    return Question(**{**COMMON, "id": i, "type": "essay", "question": f"อธิบาย {i}", "choices": [],
                       "correct_choice": None, "rubric": rubric, "model_answer": "ตัวอย่าง"})


def exam_of(*questions: Question) -> Exam:
    return Exam(title="x", spec_summary="", concepts=["A"], questions=list(questions))


class FakeLLM:
    """ตอบตาม schema ที่ถูกขอ และจดว่าแต่ละ agent ถูกเรียกกี่ครั้ง"""

    def __init__(self, exams, review, revise=None):
        self.exams = list(exams)        # Exam ที่ Generator จะคืนทีละครั้ง
        self.review = review            # (round, ids) -> list[QuestionReview]
        self.revise = revise            # ids -> list[Question]
        self.calls: list[str] = []
        self.reviewed: list[list[int]] = []

    def __call__(self, *, system, content, schema, effort):
        self.calls.append(schema.__name__)
        text = content[-1]["text"]
        if schema is ExamSpec:
            return SPEC
        if schema is Exam:
            return self.exams.pop(0)
        if schema is ExamReview:
            ids = [int(i) for i in re.findall(r'<question id="(\d+)"', text)]
            self.reviewed.append(ids)
            return ExamReview(reviews=self.review(len(self.reviewed), ids))
        if schema is RevisedQuestions:
            ids = [int(i) for i in re.findall(r'<question_to_fix id="(\d+)"', text)]
            return RevisedQuestions(questions=self.revise(ids))
        raise AssertionError(schema)


def ok(i: int, answer: str = "") -> QuestionReview:
    return QuestionReview(question_id=i, answer=answer, answer_text="", problems=[], suggestion="")


@pytest.fixture
def use(monkeypatch):
    def install(fake: FakeLLM) -> FakeLLM:
        for module in (planner, generator, reviewer):
            monkeypatch.setattr(module, "call_structured", fake)
        return fake
    return install


def statuses(result) -> dict[int, str]:
    return {q.question_id: q.status for q in result.quality}


def test_all_pass_goes_straight_to_end(use):
    fake = use(FakeLLM([exam_of(mcq(1), mcq(2), essay(3))],
                       review=lambda r, ids: [ok(1, "ก"), ok(2, "ก"), ok(3)]))
    result = generation_graph.run_generation(SOURCE, request="ปรนัย 2 ข้อ เรียงความ 1 ข้อ")
    assert [s.agent for s in result.trace] == ["Request Planner", "Question Generator", "Quality Reviewer"]
    assert result.rounds == 0 and set(statuses(result).values()) == {"passed"}
    # คะแนนตามสเปก และ rubric ถูกปรับให้รวมเท่าคะแนนข้อ
    assert [q.points for q in result.exam.questions] == [2, 2, 10]
    assert sum(c.max_points for c in result.exam.questions[2].rubric) == pytest.approx(10)


def test_confirmed_spec_skips_planner(use):
    fake = use(FakeLLM([exam_of(mcq(1), mcq(2), essay(3))], review=lambda r, ids: [ok(i, "ก") for i in ids]))
    result = generation_graph.run_generation(SOURCE, spec=SPEC)
    assert "ExamSpec" not in fake.calls
    assert result.trace[0].agent == "Question Generator"


def test_wrong_key_found_by_blind_solve_is_sent_back_and_fixed(use):
    fake = use(FakeLLM(
        [exam_of(mcq(1), mcq(2, correct=1), essay(3))],
        review=lambda r, ids: [ok(i, "ก") for i in ids],  # ผู้ตรวจตอบ ก ทุกข้อ แต่เฉลยข้อ 2 คือ ข
        revise=lambda ids: [mcq(i, correct=0, question="คำถามที่แก้แล้ว") for i in ids],
    ))
    result = generation_graph.run_generation(SOURCE, spec=SPEC)
    assert fake.reviewed == [[1, 2, 3], [2]]  # รอบสองตรวจเฉพาะข้อที่แก้
    assert result.rounds == 1
    assert statuses(result) == {1: "passed", 2: "fixed", 3: "passed"}
    fixed = result.quality[1]
    assert fixed.revisions == 1 and "ไม่เห็นเฉลย" in fixed.issues[0]
    assert result.exam.questions[1].correct_choice == 0 and result.exam.questions[1].points == 2


def test_loop_stops_after_max_revisions(use):
    def review(round_no, ids):
        return [ok(i, "ก") if i != 3 else QuestionReview(
            question_id=3, answer="", answer_text="", problems=["เกณฑ์ไม่ตรงกับคำถาม"], suggestion="แก้เกณฑ์")
            for i in ids]

    fake = use(FakeLLM([exam_of(mcq(1), mcq(2), essay(3))], review=review, revise=lambda ids: [essay(i) for i in ids]))
    result = generation_graph.run_generation(SOURCE, spec=SPEC)
    assert result.rounds == MAX_REVISIONS
    assert fake.calls.count("ExamReview") == MAX_REVISIONS + 1
    assert statuses(result)[3] == "flagged" and result.quality[2].issues == ["เกณฑ์ไม่ตรงกับคำถาม"]


def test_question_skipped_by_reviewer_is_unchecked_not_revised(use):
    fake = use(FakeLLM([exam_of(mcq(1), mcq(2), essay(3))], review=lambda r, ids: [ok(1, "ก")]))
    result = generation_graph.run_generation(SOURCE, spec=SPEC)
    assert "RevisedQuestions" not in fake.calls
    assert statuses(result) == {1: "passed", 2: "unchecked", 3: "unchecked"}


def test_generator_trims_extra_and_retries_when_short(use):
    fake = use(FakeLLM([exam_of(mcq(1), mcq(2), mcq(3)), exam_of(mcq(1), mcq(2), mcq(3), essay(4))],
                       review=lambda r, ids: []))
    exam, missing = generator.generate_from_spec(SOURCE, SPEC)
    assert fake.calls == ["Exam", "Exam"] and missing == {}
    assert [(q.id, q.type) for q in exam.questions] == [(1, "mcq"), (2, "mcq"), (3, "essay")]


def test_apply_revisions_keeps_id_type_points_and_matches_by_order():
    exam = exam_of(mcq(1, points=2), mcq(2, points=2), essay(3))
    revised, changed = generator.apply_revisions(exam, [mcq(99, correct=3, concept="ไม่มีในรายการ")], [2])
    assert changed == [2]
    q2 = revised.questions[1]
    assert (q2.id, q2.points, q2.correct_choice, q2.concept) == (2, 2, 3, "A")
    _, changed = generator.apply_revisions(exam, [essay(2)], [2])  # เปลี่ยนชนิดข้อไม่ได้
    assert changed == []


def test_structural_issues_need_no_llm():
    assert reviewer.structural_issues(mcq(1)) == []
    assert reviewer.structural_issues(mcq(1).model_copy(update={"choices": ["a", "b", "c"]}))
    assert reviewer.structural_issues(mcq(1, correct=7))
    assert reviewer.structural_issues(essay(1).model_copy(update={"rubric": []}))


def test_normalize_spec_and_problems():
    spec = planner.normalize_spec(ExamSpec(
        title=" ", focus="", concepts=["A", " A ", ""], assumptions=[],
        sections=[SectionSpec(type="essay", count=1, points_each=0, difficulty="hard"),
                  SectionSpec(type="mcq", count=3, points_each=1, difficulty="easy"),
                  SectionSpec(type="mcq", count=2, points_each=1, difficulty="easy"),
                  SectionSpec(type="true_false", count=0, points_each=1, difficulty="easy")],
    ))
    assert [(s.type, s.count, s.points_each) for s in spec.sections] == [("mcq", 5, 1), ("essay", 1, 10)]
    assert spec.concepts == ["A"] and spec.title == "ข้อสอบ"
    assert planner.spec_problems(spec) == []
    too_many = spec.model_copy(update={"sections": [SectionSpec(type="mcq", count=500, points_each=1, difficulty="easy")]})
    assert planner.spec_problems(too_many)
    assert planner.spec_problems(spec.model_copy(update={"sections": [], "concepts": []})) == [
        "ต้องมีข้อสอบอย่างน้อย 1 ข้อ", "ต้องมีหัวข้อที่จะออกสอบอย่างน้อย 1 หัวข้อ"]
