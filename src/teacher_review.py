"""คะแนนที่ครูยืนยันและแนวทางการตรวจสำหรับการสอบเดิม"""

from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import scoring
from .schemas import ClassSession, CriterionScore, Question, QuestionResult, TeacherReview


def pending(session: ClassSession) -> list[tuple[int, QuestionResult]]:
    return [
        (index, result)
        for index, student in enumerate(session.students)
        for result in student.results or []
        if result.needs_teacher_review
    ]


def _apply(
    question: Question, result: QuestionResult, scores: list[float], note: str, guidance: str, anchor_answer: str
) -> QuestionResult:
    rubric = scoring.subjective_criteria(question)
    if len(scores) != len(rubric) or any(
        not 0 <= points <= maximum for points, (_, _, maximum) in zip(scores, rubric)
    ):
        raise ValueError("ตรวจสอบคะแนนของแต่ละเกณฑ์")
    total = round(sum(scores), 2)
    if total > question.points:
        raise ValueError("คะแนนรวมเกินคะแนนเต็มของข้อ")
    if not note.strip() or any(len(text) > 600 for text in (note, guidance, anchor_answer)):
        raise ValueError("ระบุเหตุผลในการตรวจ และจำกัดข้อความให้ไม่เกินช่องละ 600 ตัวอักษร")
    criteria = [
        CriterionScore(criterion=name, max_points=maximum, points_awarded=points, evidence="", comment="")
        for (name, _, maximum), points in zip(rubric, scores)
    ]
    return result.model_copy(update={
        "points_awarded": total,
        "criteria": criteria,
        "feedback": note.strip(),
        "missed_key_points": [],
        "misconceptions": [],
        "unverified_evidence": [],
        "teacher_review": TeacherReview(
            ai_points=result.points_awarded, note=note.strip(),
            guidance=guidance.strip(), anchor_answer=anchor_answer.strip(),
        ),
    })


class ReviewState(TypedDict):
    question: Question
    result: QuestionResult


def _teacher_decision(state: ReviewState) -> dict:
    question, result = state["question"], state["result"]
    decision = interrupt({"question_id": question.id, "ai_points": result.points_awarded})
    return {"result": _apply(question, result, **decision)}


BUILDER = StateGraph(ReviewState)
BUILDER.add_node("teacher_decision", _teacher_decision)
BUILDER.add_edge(START, "teacher_decision")
BUILDER.add_edge("teacher_decision", END)
REVIEW_CONFIG = {"configurable": {"thread_id": "essay_review"}}


def _start(question: Question, result: QuestionResult):
    if question.id != result.question_id or not result.needs_teacher_review:
        raise ValueError("ข้อนี้ไม่มีคะแนนที่รอครูตรวจ")
    # ผลที่รอครูอยู่ใน SQLite แล้ว สร้างจุดหยุดจากผลเดิมได้แม้ปิดแอป โดยไม่ต้องเรียกผู้ตรวจใหม่
    graph = BUILDER.compile(checkpointer=InMemorySaver())
    paused = graph.invoke({"question": question, "result": result}, REVIEW_CONFIG)
    assert paused["__interrupt__"]
    return graph, paused["result"]


def pause(question: Question, result: QuestionResult) -> QuestionResult:
    return _start(question, result)[1]


def resolve(
    question: Question, result: QuestionResult, scores: list[float], note: str,
    guidance: str = "", anchor_answer: str = "",
) -> QuestionResult:
    graph, _ = _start(question, result)
    decision = {"scores": scores, "note": note, "guidance": guidance, "anchor_answer": anchor_answer}
    return graph.invoke(Command(resume=decision), REVIEW_CONFIG)["result"]


def grading_guidance(session: ClassSession) -> dict[int, tuple[str, ...]]:
    by_question: dict[int, list[str]] = {}
    for student in session.students:
        for result in student.results or []:
            review = result.teacher_review
            if review is None:
                continue
            notes = [review.guidance] if review.guidance else []
            if review.anchor_answer:
                scores = ", ".join(f"{c.criterion}: {c.points_awarded:g}/{c.max_points:g}" for c in result.criteria)
                notes.append(f"ตัวอย่างคำตอบที่ครูตรวจแล้ว: {review.anchor_answer} — คะแนนตามเกณฑ์: {scores}")
            if notes:
                by_question.setdefault(result.question_id, []).append("\n".join(notes))
    # shortcut: ใช้ไม่เกิน 5 แนวทางต่อข้อในหนึ่งห้อง ถ้าต้องจัดการมาตรฐานข้ามห้องให้เพิ่มคลังเกณฑ์แยก
    return {question_id: tuple(dict.fromkeys(notes))[-5:] for question_id, notes in by_question.items()}
