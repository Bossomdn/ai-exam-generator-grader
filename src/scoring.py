"""ส่วนที่คำนวณด้วยโค้ดล้วน (ไม่เรียก LLM): ตรวจปรนัย, รวมคะแนน, คะแนนรายหัวข้อ, ตรวจหลักฐาน"""

import re
from collections import defaultdict
from typing import Optional

from .schemas import (
    OBJECTIVE_TYPES,
    ConceptMastery,
    CriterionScore,
    Question,
    QuestionResult,
    SubjectiveGrade,
)

CHOICE_LETTERS = "กขคงจฉ"


def choice_label(question: Question, index: Optional[int]) -> str:
    if index is None or not 0 <= index < len(question.choices):
        return "(ไม่ได้ตอบ)"
    if question.type == "mcq":
        return f"{CHOICE_LETTERS[index]}. {question.choices[index]}"
    return question.choices[index]


def grade_objective(question: Question, answer: Optional[int]) -> QuestionResult:
    is_correct = answer is not None and answer == question.correct_choice
    return QuestionResult(
        question_id=question.id,
        type=question.type,
        concept=question.concept,
        source_ref=question.source_ref,
        points_awarded=question.points if is_correct else 0.0,
        max_points=question.points,
        is_correct=is_correct,
        student_answer=choice_label(question, answer),
        feedback=question.explanation,
    )


def subjective_criteria(question: Question) -> list[tuple[str, str, float]]:
    """เกณฑ์ที่ใช้ตรวจ: (ชื่อ, คำอธิบาย, คะแนนเต็ม)"""
    if question.type == "essay" and question.rubric:
        return [(c.name, c.description, c.max_points) for c in question.rubric]
    points = question.key_points or ["ความถูกต้องของคำตอบ"]
    each = question.points / len(points)
    return [(kp, "คำตอบมีประเด็นนี้ครบถ้วนและถูกต้อง", each) for kp in points]


def _normalize(text: str) -> str:
    text = re.sub(r"[\"'“”‘’«»]", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def quote_in_answer(quote: str, answer: str) -> bool:
    quote_n = _normalize(quote)
    return bool(quote_n) and quote_n in _normalize(answer)


def unanswered_result(question: Question) -> QuestionResult:
    return QuestionResult(
        question_id=question.id,
        type=question.type,
        concept=question.concept,
        source_ref=question.source_ref,
        points_awarded=0.0,
        max_points=question.points,
        student_answer="(ไม่ได้ตอบ)",
        feedback="ไม่ได้ตอบข้อนี้",
        missed_key_points=[name for name, _, _ in subjective_criteria(question)],
    )


def finalize_subjective(question: Question, grade: SubjectiveGrade, answer: str) -> QuestionResult:
    """จำกัดคะแนนไม่ให้เกินเกณฑ์ และตรวจว่าหลักฐานที่ grader อ้างมีอยู่ในคำตอบจริง"""
    limits = {name: max_pts for name, _, max_pts in subjective_criteria(question)}
    criteria: list[CriterionScore] = []
    unverified: list[str] = []
    for c in grade.criteria:
        max_pts = limits.get(c.criterion, c.max_points)
        awarded = min(max(c.points_awarded, 0.0), max_pts)
        criteria.append(c.model_copy(update={"points_awarded": awarded, "max_points": max_pts}))
        if c.evidence.strip() and not quote_in_answer(c.evidence, answer):
            unverified.append(c.evidence)
    total = min(sum(c.points_awarded for c in criteria), question.points)
    return QuestionResult(
        question_id=question.id,
        type=question.type,
        concept=question.concept,
        source_ref=question.source_ref,
        points_awarded=round(total, 2),
        max_points=question.points,
        student_answer=answer,
        feedback=grade.feedback,
        criteria=criteria,
        missed_key_points=grade.missed_key_points,
        misconceptions=grade.misconceptions,
        unverified_evidence=unverified,
    )


def totals(results: list[QuestionResult]) -> dict[str, float]:
    objective = [r for r in results if r.type in OBJECTIVE_TYPES]
    subjective = [r for r in results if r.type not in OBJECTIVE_TYPES]
    return {
        "score": sum(r.points_awarded for r in results),
        "max": sum(r.max_points for r in results),
        "objective_score": sum(r.points_awarded for r in objective),
        "objective_max": sum(r.max_points for r in objective),
        "subjective_score": sum(r.points_awarded for r in subjective),
        "subjective_max": sum(r.max_points for r in subjective),
    }


def concept_mastery(results: list[QuestionResult]) -> list[ConceptMastery]:
    """คะแนนรายหัวข้อ เรียงจากอ่อนสุดไปเก่งสุด"""
    groups: dict[str, list[QuestionResult]] = defaultdict(list)
    for r in results:
        groups[r.concept].append(r)
    mastery = []
    for concept, rs in groups.items():
        awarded = sum(r.points_awarded for r in rs)
        max_pts = sum(r.max_points for r in rs)
        mastery.append(ConceptMastery(
            concept=concept,
            points_awarded=awarded,
            max_points=max_pts,
            percent=round(100 * awarded / max_pts, 1) if max_pts else 0.0,
            question_ids=[r.question_id for r in rs],
            source_refs=sorted({r.source_ref for r in rs}),
        ))
    return sorted(mastery, key=lambda m: m.percent)
