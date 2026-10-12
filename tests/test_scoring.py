from src import scoring
from src.schemas import CriterionScore, Question, RubricCriterion, SubjectiveGrade


def make_question(**overrides) -> Question:
    base = dict(
        id=1, type="mcq", concept="Overfitting", source_ref="สไลด์ 4", bloom_level="understand",
        difficulty="medium", points=1, question="ข้อใดคือ overfitting",
        choices=["ก", "ข", "ค", "ง"], correct_choice=2, explanation="เพราะ...",
        model_answer="", key_points=[], rubric=[],
    )
    base.update(overrides)
    return Question(**base)


def essay_question() -> Question:
    return make_question(
        id=3, type="essay", concept="Metrics", source_ref="สไลด์ 5", points=10,
        choices=[], correct_choice=None, model_answer="ตัวอย่าง",
        rubric=[
            RubricCriterion(name="เนื้อหา", description="ถูกต้อง", max_points=6),
            RubricCriterion(name="เหตุผล", description="มีเหตุผล", max_points=4),
        ],
    )


def test_grade_objective_correct_wrong_and_blank():
    q = make_question()
    assert scoring.grade_objective(q, 2).points_awarded == 1
    wrong = scoring.grade_objective(q, 0)
    assert wrong.points_awarded == 0 and wrong.is_correct is False
    blank = scoring.grade_objective(q, None)
    assert blank.points_awarded == 0 and blank.student_answer == "(ไม่ได้ตอบ)"


def test_choice_label_uses_thai_letters_for_mcq_only():
    assert scoring.choice_label(make_question(), 1) == "ข. ข"
    tf = make_question(type="true_false", choices=["ถูก", "ผิด"], correct_choice=0)
    assert scoring.choice_label(tf, 1) == "ผิด"


def test_short_answer_criteria_split_points_across_key_points():
    q = make_question(type="short_answer", points=3, choices=[], correct_choice=None,
                      key_points=["a", "b", "c"])
    criteria = scoring.subjective_criteria(q)
    assert [c[0] for c in criteria] == ["a", "b", "c"]
    assert sum(c[2] for c in criteria) == 3


def test_finalize_subjective_clips_points_and_flags_fake_evidence():
    q = essay_question()
    answer = "Recall สำคัญเพราะการพลาดผู้ป่วยจริงมีต้นทุนสูง"
    grade = SubjectiveGrade(
        criteria=[
            # ให้เกินเกณฑ์ → ต้องถูกตัดเหลือ 6
            CriterionScore(criterion="เนื้อหา", points_awarded=9, max_points=9,
                           evidence="การพลาดผู้ป่วยจริงมีต้นทุนสูง", comment=""),
            # อ้างข้อความที่ไม่มีอยู่ในคำตอบ
            CriterionScore(criterion="เหตุผล", points_awarded=3, max_points=4,
                           evidence="precision ต่ำกว่า recall เสมอ", comment=""),
        ],
        missed_key_points=[], misconceptions=[], feedback="ดี",
    )
    result = scoring.finalize_subjective(q, grade, answer)
    assert result.criteria[0].points_awarded == 6
    assert result.points_awarded == 9
    assert result.unverified_evidence == ["precision ต่ำกว่า recall เสมอ"]


def test_finalize_subjective_matches_criterion_names_with_extra_text():
    q = make_question(type="short_answer", points=2, choices=[], correct_choice=None, key_points=["Bias", "Variance"])
    grade = SubjectiveGrade(
        criteria=[
            # โมเดลเติมข้อความต่อท้ายชื่อเกณฑ์ และให้ max_points เกินจริง
            CriterionScore(criterion="Bias (เต็ม 1 คะแนน)", points_awarded=5, max_points=5, evidence="", comment=""),
            CriterionScore(criterion="อะไรสักอย่าง", points_awarded=1, max_points=1, evidence="", comment=""),
            CriterionScore(criterion="เกณฑ์ที่ไม่มีอยู่จริง", points_awarded=9, max_points=9, evidence="", comment=""),
        ],
        missed_key_points=[], misconceptions=[], feedback="",
    )
    result = scoring.finalize_subjective(q, grade, "คำตอบ")
    assert [c.criterion for c in result.criteria] == ["Bias", "Variance"]
    assert [c.points_awarded for c in result.criteria] == [1, 1]
    assert result.points_awarded == 2


def test_quote_matching_ignores_whitespace_and_quote_marks():
    assert scoring.quote_in_answer("“ต้นทุน   สูง”", "การพลาดมีต้นทุน สูง มาก")
    assert not scoring.quote_in_answer("", "อะไรก็ได้")


def test_unanswered_subjective_gets_zero_and_lists_missed_points():
    result = scoring.unanswered_result(essay_question())
    assert result.points_awarded == 0
    assert result.missed_key_points == ["เนื้อหา", "เหตุผล"]


def test_totals_and_concept_mastery_sorted_weakest_first():
    mcq = make_question()
    results = [
        scoring.grade_objective(mcq, 2),  # Overfitting 1/1
        scoring.grade_objective(make_question(id=2, concept="Metrics", source_ref="สไลด์ 5"), 0),  # Metrics 0/1
        scoring.unanswered_result(essay_question()),  # Metrics 0/10
    ]
    t = scoring.totals(results)
    assert (t["score"], t["max"]) == (1, 12)
    assert (t["objective_score"], t["objective_max"]) == (1, 2)
    assert t["subjective_max"] == 10

    mastery = scoring.concept_mastery(results)
    assert [m.concept for m in mastery] == ["Metrics", "Overfitting"]
    assert mastery[0].percent == 0 and mastery[1].percent == 100
    assert mastery[0].question_ids == [2, 3]
