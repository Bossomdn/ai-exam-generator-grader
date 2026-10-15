"""ลำดับการทำงานของ agent ฝั่งสร้างข้อสอบ (LangGraph)

    START ─┬─ (ยังไม่มีสเปก) → request_planner ─┐
           └─ (ผู้ใช้ยืนยันสเปกแล้ว) ────────────┴→ question_generator → quality_reviewer ─┬→ END (ผ่านหมด / ครบรอบ)
                                                                  ↑                        │
                                                                  └──── revise_questions ←─┘ (มีข้อไม่ผ่าน, ≤ MAX_REVISIONS รอบ)

Reviewer รอบแรกตรวจทุกข้อ รอบถัดไปตรวจเฉพาะข้อที่เพิ่งแก้
"""

import operator
import time
from dataclasses import dataclass, field
from typing import Annotated, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from . import generator, planner, reviewer
from .config import MAX_REVISIONS
from .llm import LLMError
from .schemas import AgentStep, Exam, ExamSpec, QuestionQuality
from .sources import Source


class GenerationState(TypedDict, total=False):
    source: Source
    request: str
    spec: ExamSpec
    exam: Exam
    pending: list[int]            # ข้อที่ Reviewer ต้องตรวจในรอบนี้
    issues: dict[int, list[str]]  # ปัญหาล่าสุดของข้อที่ยังไม่ผ่าน
    suggestions: dict[int, str]   # วิธีแก้ที่ Reviewer เสนอ
    found: dict[int, list[str]]   # ปัญหาทั้งหมดที่เคยพบ (ไว้แสดงว่าแก้อะไรไปแล้ว)
    revised: dict[int, int]       # จำนวนครั้งที่แต่ละข้อถูกแก้
    checked: list[int]            # ข้อที่เวอร์ชันล่าสุดได้รับการตรวจแล้ว
    rounds: int                   # จำนวนรอบที่ส่งกลับไปแก้
    trace: Annotated[list[AgentStep], operator.add]


def _step(agent: str, summary: str, started: float) -> dict:
    return {"trace": [AgentStep(agent=agent, summary=summary, seconds=round(time.perf_counter() - started, 1))]}


def _ids(ids) -> str:
    return ", ".join(str(i) for i in sorted(ids))


def request_planner(state: GenerationState) -> dict:
    started = time.perf_counter()
    spec = planner.plan_exam(state["source"], state.get("request", ""))
    if problems := planner.spec_problems(spec):
        raise LLMError("สเปกข้อสอบที่ได้ใช้ไม่ได้: " + " / ".join(problems))
    total = sum(s.count for s in spec.sections)
    return {"spec": spec, **_step("Request Planner", f"แปลงคำสั่งเป็นสเปก: {total} ข้อ, {len(spec.concepts)} หัวข้อ", started)}


def question_generator(state: GenerationState) -> dict:
    started = time.perf_counter()
    exam, missing = generator.generate_from_spec(state["source"], state["spec"])
    if not exam.questions:
        raise LLMError("สร้างข้อสอบไม่สำเร็จ (ได้ข้อสอบ 0 ข้อ) ลองใหม่อีกครั้ง")
    summary = f"ออกข้อสอบ {len(exam.questions)} ข้อ พร้อมเฉลยและเกณฑ์ให้คะแนน"
    if missing:
        summary += f" (ยังขาด {generator.describe_missing(missing)})"
    return {
        "exam": exam,
        "pending": [q.id for q in exam.questions],
        "issues": {}, "suggestions": {}, "found": {}, "revised": {}, "checked": [], "rounds": 0,
        **_step("Question Generator", summary, started),
    }


def quality_reviewer(state: GenerationState) -> dict:
    started = time.perf_counter()
    pending = state["pending"]
    issues = dict(state["issues"])
    if not pending:
        return {"issues": issues, **_step("Quality Reviewer", "ไม่มีข้อที่แก้สำเร็จให้ตรวจซ้ำ", started)}

    outcome = reviewer.review_questions(state["source"], state["exam"], pending)
    found = {i: list(v) for i, v in state["found"].items()}
    for i in pending:
        issues.pop(i, None)
    for i, problems in outcome.issues.items():
        if problems:
            issues[i] = problems
            found[i] = list(dict.fromkeys(found.get(i, []) + problems))
    checked = [i for i in state["checked"] if i not in pending] + list(outcome.issues)

    failed = [i for i in pending if outcome.issues.get(i)]
    skipped = [i for i in pending if i not in outcome.issues]
    summary = f"ตรวจ {len(pending)} ข้อ: ผ่าน {len(pending) - len(failed) - len(skipped)}"
    summary += f", ต้องแก้ {len(failed)} (ข้อ {_ids(failed)})" if failed else ", ไม่มีข้อที่ต้องแก้"
    if skipped:
        summary += f", ไม่ได้ตรวจ {len(skipped)} (ข้อ {_ids(skipped)})"
    return {
        "issues": issues,
        "suggestions": {**state["suggestions"], **outcome.suggestions},
        "found": found,
        "checked": checked,
        **_step("Quality Reviewer", summary, started),
    }


def revise_questions(state: GenerationState) -> dict:
    started = time.perf_counter()
    issues = state["issues"]
    exam, changed = generator.revise_questions(
        state["source"], state["spec"], state["exam"], issues, state["suggestions"]
    )
    revised = dict(state["revised"])
    for i in changed:
        revised[i] = revised.get(i, 0) + 1
    rounds = state["rounds"] + 1
    summary = f"แก้ข้อ {_ids(changed)} ตามผลตรวจ (รอบที่ {rounds})" if changed else f"แก้ไม่สำเร็จ (รอบที่ {rounds})"
    if unchanged := [i for i in issues if i not in changed]:
        summary += f" · ข้อ {_ids(unchanged)} ไม่ได้รับการแก้"
    return {
        "exam": exam,
        "pending": changed,
        "revised": revised,
        "checked": [i for i in state["checked"] if i not in changed],
        "rounds": rounds,
        **_step("Question Generator (แก้ไข)", summary, started),
    }


def _start_route(state: GenerationState) -> str:
    return "question_generator" if state.get("spec") else "request_planner"


def _review_route(state: GenerationState) -> str:
    return "revise_questions" if state["issues"] and state["rounds"] < MAX_REVISIONS else END


def build_graph():
    graph = StateGraph(GenerationState)
    graph.add_node("request_planner", request_planner)
    graph.add_node("question_generator", question_generator)
    graph.add_node("quality_reviewer", quality_reviewer)
    graph.add_node("revise_questions", revise_questions)
    graph.add_conditional_edges(START, _start_route, ["request_planner", "question_generator"])
    graph.add_edge("request_planner", "question_generator")
    graph.add_edge("question_generator", "quality_reviewer")
    graph.add_conditional_edges("quality_reviewer", _review_route, ["revise_questions", END])
    graph.add_edge("revise_questions", "quality_reviewer")
    return graph.compile()


GRAPH = build_graph()


@dataclass
class GenerationResult:
    exam: Exam
    spec: ExamSpec
    quality: list[QuestionQuality]
    rounds: int
    trace: list[AgentStep] = field(default_factory=list)


def quality_report(state: GenerationState) -> list[QuestionQuality]:
    report = []
    for q in state["exam"].questions:
        revisions = state["revised"].get(q.id, 0)
        if q.id in state["issues"]:
            status, issues = "flagged", state["issues"][q.id]
        elif q.id not in state["checked"]:
            status, issues = "unchecked", state["found"].get(q.id, [])
        elif revisions:
            status, issues = "fixed", state["found"].get(q.id, [])
        else:
            status, issues = "passed", []
        report.append(QuestionQuality(question_id=q.id, status=status, issues=issues, revisions=revisions))
    return report


def run_generation(
    source: Source,
    *,
    request: str = "",
    spec: Optional[ExamSpec] = None,
    prior_steps: list[AgentStep] = (),
    on_step: Optional[Callable[[AgentStep], None]] = None,
) -> GenerationResult:
    """รันกราฟ: ถ้ามี spec (ผู้ใช้ยืนยันแล้ว) จะข้าม Request Planner; on_step ถูกเรียกทุกครั้งที่ agent ทำงานเสร็จ"""
    initial: GenerationState = {"source": source, "request": request, "trace": list(prior_steps)}
    if spec is not None:
        initial["spec"] = spec
    final: GenerationState = initial
    for mode, chunk in GRAPH.stream(initial, stream_mode=["updates", "values"]):
        if mode == "values":
            final = chunk
        elif on_step:
            for update in chunk.values():
                for step in (update or {}).get("trace", []):
                    on_step(step)
    return GenerationResult(
        exam=final["exam"], spec=final["spec"], quality=quality_report(final),
        rounds=final["rounds"], trace=final["trace"],
    )
