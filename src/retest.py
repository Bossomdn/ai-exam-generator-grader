"""สอบซ่อม: เลือกคนที่ไม่ผ่านเกณฑ์ → ออกข้อสอบชุดซ่อม (ข้อสอบคู่ขนาน) → คะแนนหลังซ่อม

ข้อสอบชุดซ่อมวัดหัวข้อเดิม ชนิด จำนวนข้อ คะแนน และความยากเท่าครั้งแรก แต่ไม่ใช้คำถามเดิม:
Question Generator ออกตามสเปกของข้อสอบครั้งแรก → Quality Reviewer ตรวจ → โค้ดเทียบกับข้อเดิม
ข้อที่ซ้ำส่งกลับเข้าลูป Generator ⇄ Reviewer อีกรอบ (แบบเดียวกับคำแนะนำจาก Item Analyst)
คะแนนหลังซ่อม = คะแนนสอบซ่อมแต่ไม่เกินเกณฑ์ผ่าน และไม่ต่ำกว่าครั้งแรก (scoring.after_retest)
"""

from difflib import SequenceMatcher
from typing import Callable, Optional

from . import planner, scoring
from .generation_graph import GenerationResult, run_generation, run_revision
from .schemas import AgentStep, ClassSession, Exam, StudentRecord
from .sources import Source

DUPLICATE_RATIO = 0.75  # คำถามคล้ายข้อเดิมตั้งแต่นี้ขึ้นไป = ซ้ำ
TITLE_SUFFIX = " (ชุดสอบซ่อม)"


def percent(student: StudentRecord) -> Optional[float]:
    if student.results is None:
        return None
    t = scoring.totals(student.results)
    return round(100 * t["score"] / t["max"], 1) if t["max"] else 0.0


def below_pass(session: ClassSession, pass_percent: float) -> list[StudentRecord]:
    """ผู้สอบที่ตรวจแล้วได้ต่ำกว่าเกณฑ์ผ่าน (ตามลำดับในตารางคะแนน)"""
    return [s for s in session.students if (p := percent(s)) is not None and p < pass_percent]


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def duplicates(original: Exam, retest: Exam) -> dict[int, int]:
    """{ข้อในชุดซ่อม: ข้อเดิมที่คำถามคล้ายกันมาก}"""
    found = {}
    for q in retest.questions:
        for old in original.questions:
            if SequenceMatcher(None, _normalize(q.question), _normalize(old.question)).ratio() >= DUPLICATE_RATIO:
                found[q.id] = old.id
                break
    return found


def retest_spec(exam: Exam):
    """สเปกของข้อสอบครั้งแรก + คำสั่งให้เป็นข้อสอบคู่ขนานที่ไม่ใช้คำถามเดิม"""
    spec = planner.spec_from_exam(exam)
    stems = "\n".join(f"- {q.question[:100]}" for q in exam.questions)
    focus = (
        f"{spec.focus}\n\nข้อสอบชุดสอบซ่อม (ข้อสอบคู่ขนาน): วัดหัวข้อเดิม ชนิด จำนวนข้อ คะแนน และระดับความยากเท่าข้อสอบครั้งแรก "
        "แต่ห้ามใช้คำถาม ตัวอย่าง ตัวเลข หรือสถานการณ์เดิม คำถามในข้อสอบครั้งแรก:\n" + stems
    ).strip()
    return spec.model_copy(update={"title": exam.title + TITLE_SUFFIX, "focus": focus})


def make_retest(
    source: Source, exam: Exam, *, on_step: Optional[Callable[[AgentStep], None]] = None
) -> GenerationResult:
    """ออกข้อสอบชุดซ่อมผ่านกราฟสร้างข้อสอบ แล้วส่งข้อที่ซ้ำกับข้อเดิมกลับไปแก้ (เรียก LLM อาจโยน LLMError)"""
    spec = retest_spec(exam)
    result = run_generation(source, spec=spec, on_step=on_step)
    same = duplicates(exam, result.exam)
    if not same:
        return result
    issues = {i: [f"คำถามซ้ำหรือเกือบซ้ำกับข้อ {old} ของข้อสอบครั้งแรก"] for i, old in same.items()}
    suggestions = {i: "เขียนข้อใหม่ที่วัดหัวข้อเดิมในระดับเดิม แต่ใช้คำถาม ตัวอย่าง และสถานการณ์ต่างจากเดิม" for i in same}
    step = AgentStep(agent="ตรวจข้อซ้ำ (โค้ด)", seconds=0.0,
                     summary=f"ข้อ {', '.join(map(str, same))} ซ้ำกับข้อสอบครั้งแรก ส่งกลับให้ Generator เขียนใหม่")
    if on_step:
        on_step(step)
    return run_revision(source, spec, result.exam, issues, suggestions, prior_steps=result.trace + [step],
                        on_step=on_step)


def summary(original: ClassSession, retests: list[ClassSession], pass_percent: float) -> list[dict]:
    """คะแนนของคนที่สอบซ่อมแล้ว: ครั้งแรก / สอบซ่อม (รอบที่ดีที่สุด) / หลังซ่อม / ผ่านเกณฑ์ไหม"""
    best: dict[str, float] = {}
    for session in retests:
        for s in session.students:
            if (p := percent(s)) is not None:
                best[s.key] = max(p, best.get(s.key, p))
    rows = []
    for s in original.students:
        first = percent(s)
        if s.key not in best or first is None:
            continue
        after = scoring.after_retest(first, best[s.key], pass_percent)
        rows.append({"เลขที่": s.student_no, "ชื่อ": s.name, "ครั้งแรก": first, "สอบซ่อม": best[s.key],
                     "หลังซ่อม": after, "ผ่านเกณฑ์": "✓" if after >= pass_percent else "✗"})
    return rows
