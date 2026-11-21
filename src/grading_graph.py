"""ลำดับการทำงานของ agent ฝั่งตรวจเรียงความ (LangGraph): คณะผู้ตรวจ 3 มุมมอง + Moderator

    START ─┬→ ผู้ตรวจด้านเนื้อหา ─┐
           ├→ ผู้ตรวจด้านเหตุผล ──┼→ compare ─┬→ END        (ต่างกัน ≤ PANEL_AGREE: ใช้ผลของผู้ตรวจที่ให้คะแนนกลาง)
           └→ ผู้ตรวจยึดเกณฑ์ ───┘           └→ moderator → END (ต่างกันมาก: Moderator ตัดสิน
                                                               ถ้าต่างกันเกิน PANEL_ESCALATE ให้ครูตรวจซ้ำด้วย)

ผู้ตรวจ 3 คนทำงานพร้อมกันโดยไม่เห็นผลของกันและกัน (กันการคล้อยตาม) ส่วน Moderator เห็นความเห็นของทุกคน
ปรนัยตรวจด้วยโค้ด ตอบสั้นใช้ผู้ตรวจคนเดียว (src/grader.py) · GRADER_MODE=single ปิดคณะผู้ตรวจ (ใช้เทียบในการประเมินผล)
"""

import operator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Literal, Optional, TypedDict, Union

from langgraph.graph import END, START, StateGraph

from . import config, grader, scoring, teacher_review
from .schemas import OBJECTIVE_TYPES, Exam, PanelGrade, PanelVerdict, Question, QuestionResult, SubjectiveGrade

Answer = Union[int, str, None]  # ปรนัย = index ตัวเลือก, อัตนัย = ข้อความ

GRADER_LABELS = {"เนื้อหา": "ผู้ตรวจด้านเนื้อหา", "เหตุผล": "ผู้ตรวจด้านเหตุผล", "เกณฑ์": "ผู้ตรวจยึดเกณฑ์"}
NODE_IDS = {"เนื้อหา": "grader_content", "เหตุผล": "grader_reasoning", "เกณฑ์": "grader_rubric"}


class PanelState(TypedDict, total=False):
    question: Question
    answer: str
    guidance: tuple[str, ...]
    grades: Annotated[list[tuple[str, SubjectiveGrade]], operator.add]  # (มุมมอง, ผลตรวจ) ของผู้ตรวจแต่ละคน
    panel: list[PanelGrade]  # คะแนนของแต่ละคนหลังโค้ดตรวจเกณฑ์/คะแนนเต็มแล้ว
    spread: float
    result: QuestionResult


def _grader_node(perspective: str):
    def node(state: PanelState) -> dict:
        grade = grader.call_grader(state["question"], state["answer"], perspective, guidance=state["guidance"])
        return {"grades": [(perspective, grade)]}
    return node


def _ordered(state: PanelState) -> dict[str, SubjectiveGrade]:
    """ผลของคณะเรียงตามลำดับมุมมอง (ผู้ตรวจทำเสร็จไม่พร้อมกัน)"""
    found = dict(state["grades"])
    return {p: found[p] for p in grader.PERSPECTIVES if p in found}


def compare(state: PanelState) -> dict:
    """โค้ดเทียบคะแนนของคณะ: ต่างกันน้อย = ใช้ผลของผู้ตรวจที่ให้คะแนนกลาง ไม่ต้องเรียก Moderator"""
    question, answer = state["question"], state["answer"]
    results = {p: scoring.finalize_subjective(question, g, answer) for p, g in _ordered(state).items()}
    panel = [PanelGrade(grader=p, points=r.points_awarded, criteria=r.criteria) for p, r in results.items()]
    points = [g.points for g in panel]
    spread = round(max(points) - min(points), 2)
    update: dict = {"panel": panel, "spread": spread}
    if spread <= config.PANEL_AGREE * question.points:
        median = sorted(results.values(), key=lambda r: r.points_awarded)[len(results) // 2]
        verdict = PanelVerdict(grades=panel, spread=spread, decision="agreed")
        update["result"] = median.model_copy(update={"panel": verdict})
    return update


def moderator(state: PanelState) -> dict:
    question, answer, spread = state["question"], state["answer"], state["spread"]
    decided = grader.moderate(question, answer, _ordered(state), guidance=state["guidance"])
    grade = SubjectiveGrade(criteria=decided.criteria, missed_key_points=decided.missed_key_points,
                            misconceptions=decided.misconceptions, feedback=decided.feedback)
    verdict = PanelVerdict(
        grades=state["panel"], spread=spread, decision="moderated", rationale=decided.rationale,
        needs_teacher=spread > config.PANEL_ESCALATE * question.points,
    )
    return {"result": scoring.finalize_subjective(question, grade, answer).model_copy(update={"panel": verdict})}


def _after_compare(state: PanelState) -> str:
    return END if state.get("result") is not None else "moderator"


def build_graph():
    builder = StateGraph(PanelState)
    for perspective, node_id in NODE_IDS.items():
        builder.add_node(node_id, _grader_node(perspective))
        builder.add_edge(START, node_id)
    builder.add_node("compare", compare)
    builder.add_node("moderator", moderator)
    builder.add_edge(list(NODE_IDS.values()), "compare")  # รอผู้ตรวจครบทั้ง 3 คน
    builder.add_conditional_edges("compare", _after_compare, ["moderator", END])
    builder.add_edge("moderator", END)
    return builder.compile()


GRAPH = build_graph()


def grade_answer(
    question: Question, answer: str, *,
    mode: Literal["single", "panel"] | None = None,
    guidance: tuple[str, ...] = (),
    review_with_teacher: bool = False,
) -> QuestionResult:
    """ตรวจอัตนัย 1 ข้อ: เรียงความใช้คณะผู้ตรวจ (ถ้าเปิดไว้) ชนิดอื่นใช้ผู้ตรวจคนเดียว — อาจโยน LLMError"""
    if not answer.strip():
        return scoring.unanswered_result(question)
    if (mode or config.GRADER_MODE) != "panel" or question.type not in config.PANEL_TYPES:
        return grader.grade_subjective(question, answer)
    result = GRAPH.invoke({"question": question, "answer": answer, "guidance": guidance})["result"]
    if review_with_teacher and result.needs_teacher_review:
        return teacher_review.pause(question, result)
    return result


def grade_exam(
    exam: Exam, answers: dict[int, Answer], *, guidance: dict[int, tuple[str, ...]] | None = None,
    review_with_teacher: bool = False,
) -> list[QuestionResult]:
    """ตรวจทั้งชุด: ปรนัยด้วยโค้ด อัตนัยหลายข้อพร้อมกัน"""
    results = []
    subjective = []
    for q in exam.questions:
        if q.type in OBJECTIVE_TYPES:
            answer: Optional[int] = answers.get(q.id)  # type: ignore[assignment]
            results.append(scoring.grade_objective(q, answer))
        else:
            subjective.append(q)
    with ThreadPoolExecutor(max_workers=config.GRADING_WORKERS) as pool:
        futures = [
            pool.submit(
                grade_answer, q, str(answers.get(q.id) or ""),
                guidance=(guidance or {}).get(q.id, ()), review_with_teacher=review_with_teacher,
            )
            for q in subjective
        ]
        results.extend(f.result() for f in futures)
    return sorted(results, key=lambda r: r.question_id)
