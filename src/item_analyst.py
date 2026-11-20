"""Item Analyst (LLM): อ่านสถิติข้อสอบจากผลสอบจริง แล้ววินิจฉัยว่าข้อที่ผิดปกติเป็นเพราะอะไร ควรแก้อย่างไร

โค้ดคำนวณตัวเลขทั้งหมด (src/item_analysis.py) agent ทำหน้าที่ตีความเท่านั้น
ข้อที่ควรแก้จะถูกส่งต่อเข้าลูป Question Generator ⇄ Quality Reviewer เดิม (generation_graph.run_revision)
"""

from . import scoring
from .config import EFFORT
from .item_analysis import ExamStats, ItemStat
from .llm import call_structured
from .schemas import Exam, ItemAnalystReport, ItemDiagnosis, Question

SYSTEM_PROMPT = """คุณคือ Item Analyst ผู้เชี่ยวชาญการวัดผลการศึกษา หน้าที่คือวิเคราะห์ข้อสอบจากผลสอบจริงของนักเรียน
ข้อมูลที่ได้รับคือข้อที่ระบบพบความผิดปกติ พร้อมเฉลย ค่าความยาก (p) ค่าอำนาจจำแนก (r) และจำนวนคนที่เลือกแต่ละตัวเลือก
แยกกลุ่มเก่ง (upper) และกลุ่มอ่อน (lower) กลุ่มละ 27% ตามคะแนนรวม

แนวการวินิจฉัย
- r ติดลบ หรือตัวลวงที่กลุ่มเก่งเลือกมากกว่ากลุ่มอ่อน: ตรวจก่อนว่าเฉลยผิด หรือมีคำตอบที่ถูกได้มากกว่า 1 ตัวเลือกหรือไม่ แล้วจึงดูว่าคำถามกำกวมหรือไม่
  ถ้าตัวเลือกที่กลุ่มเก่งเลือกเป็นคำตอบที่ถูกจริงตามหลักวิชา ให้เขียนใน diagnosis ตรง ๆ ว่า "เฉลยผิด" และเสนอให้แก้เฉลย (revise) ไม่ต้องเขียนข้อใหม่
- มีคนเลือกตัวลวงมากกว่าเฉลย: อาจเป็นความเข้าใจผิดที่พบบ่อย (บอกครูว่านักเรียนเข้าใจผิดเรื่องอะไร) หรือเฉลยผิด
- ตัวลวงไม่มีคนเลือก: ผิดชัดเกินไป ให้เสนอตัวลวงใหม่ที่สมเหตุสมผลกว่า
- ยากมาก: คำถามกำกวม เนื้อหาเกินที่สอน หรือเฉลยผิด · ง่ายมาก: มีคำใบ้ในคำถามหรือตัวลวงอ่อน (ข้อพื้นฐานที่ตั้งใจให้ง่ายเก็บไว้ได้)
- อัตนัย: p ต่ำมากหรือ r ต่ำ อาจเพราะคำถามไม่ชัดว่าต้องการอะไร หรือเกณฑ์ให้คะแนนไม่สอดคล้องกับคำถาม
- ผู้สอบน้อย ตัวเลขแกว่งได้ อย่าสรุปเกินข้อมูล ถ้าหลักฐานอ่อนให้เลือก keep และบอกว่าควรดูผลรอบถัดไป

action: keep = ใช้ต่อได้, revise = ปรับบางส่วน, rewrite = เขียนใหม่ทั้งข้อโดยวัดหัวข้อเดิม
เขียน summary, diagnosis และ suggestion เป็นภาษาไทยให้ครูอ่านเข้าใจ อ้างตัวเลขจริง และ suggestion ต้องเจาะจงพอที่ผู้ออกข้อสอบแก้ตามได้"""


def _item_block(q: Question, stat: ItemStat) -> str:
    lines = [f'<item id="{q.id}" type="{q.type}" concept="{q.concept}" p="{stat.p:.2f}" r="{stat.r:.2f}">',
             f"<question>{q.question}</question>"]
    if stat.choices:
        lines.append("<choices>")
        for c in stat.choices:
            key = ' key="true"' if c.is_key else ""
            lines.append(f'<choice{key} total="{c.total}" upper="{c.upper}" lower="{c.lower}">{c.label}</choice>')
        lines.append("</choices>")
        lines.append(f"<explanation>{q.explanation}</explanation>")
    else:
        criteria = "; ".join(f"{name} ({max_pts:g} คะแนน)" for name, _, max_pts in scoring.subjective_criteria(q))
        lines.append(f"<criteria>{criteria}</criteria>")
        lines.append(f"<model_answer>{q.model_answer}</model_answer>")
    lines.append("<flags>" + " / ".join(stat.flags) + "</flags>")
    lines.append("</item>")
    return "\n".join(lines)


def analyze_items(exam: Exam, stats: ExamStats) -> ItemAnalystReport:
    """ให้ Item Analyst วินิจฉัยเฉพาะข้อที่โค้ดพบความผิดปกติ (ไม่มีข้อผิดปกติ = ไม่เรียก LLM)"""
    flagged = {s.question_id: s for s in stats.flagged}
    if not flagged:
        return ItemAnalystReport(summary="ไม่พบข้อที่ค่าสถิติผิดปกติ ข้อสอบชุดนี้ใช้ต่อได้", items=[])
    questions = {q.id: q for q in exam.questions}
    reliability = f"{stats.reliability:.2f}" if stats.reliability is not None else "คำนวณไม่ได้"
    header = (f'<exam title="{exam.title}" students="{stats.n}" group_size="{stats.group_size}" '
              f'mean_percent="{stats.mean_percent:g}" reliability="{reliability}" items="{len(exam.questions)}">')
    blocks = "\n".join(_item_block(questions[i], s) for i, s in flagged.items())
    report = call_structured(
        system=SYSTEM_PROMPT,
        content=[{"type": "text", "text": f"{header}\n{blocks}\n</exam>"}],
        schema=ItemAnalystReport,
        effort=EFFORT["analyze"],
    )
    # เก็บเฉพาะข้อที่ส่งไป ข้อละ 1 รายการ ข้อที่ agent ตกหล่นให้ใช้ข้อสังเกตของโค้ดแทน
    found: dict[int, ItemDiagnosis] = {}
    for d in report.items:
        if d.question_id in flagged and d.question_id not in found:
            found[d.question_id] = d
    items = [
        found.get(i) or ItemDiagnosis(question_id=i, diagnosis=" / ".join(s.flags), action="revise", suggestion="")
        for i, s in flagged.items()
    ]
    return report.model_copy(update={"items": items})


def revision_request(report: ItemAnalystReport, stats: ExamStats) -> tuple[dict[int, list[str]], dict[int, str]]:
    """แปลงผลวินิจฉัยเป็นงานแก้สำหรับ Question Generator: ({ข้อ: ปัญหา}, {ข้อ: วิธีแก้}) เฉพาะข้อที่ไม่ใช่ keep"""
    flags = {s.question_id: s.flags for s in stats.items}
    issues: dict[int, list[str]] = {}
    suggestions: dict[int, str] = {}
    for d in report.items:
        if d.action == "keep":
            continue
        issues[d.question_id] = [f"จากผลสอบจริง: {d.diagnosis}"] + flags.get(d.question_id, [])
        prefix = "เขียนข้อใหม่ทั้งข้อ โดยวัดหัวข้อเดิม: " if d.action == "rewrite" else ""
        suggestions[d.question_id] = prefix + d.suggestion
    return issues, suggestions
