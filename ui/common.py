"""สถานะของแอป (st.session_state) และฟังก์ชันที่หลายแท็บใช้ร่วมกัน"""

from typing import Callable, Optional

import streamlit as st

from src.generation_graph import GenerationResult, run_generation
from src.grading_graph import GRADER_LABELS
from src.llm import LLMError
from src.schemas import AgentStep, Exam, QuestionResult
from src.sources import Source

state = st.session_state

DEFAULTS = {
    "source": None, "exam": None, "version": 0,
    "plan": None,  # สเปกจาก Request Planner ที่รอผู้ใช้ยืนยัน {"key", "spec", "step", "n"}
    "quality": {}, "trace": [], "rounds": 0,  # ผลตรวจคุณภาพรายข้อ และบันทึกการทำงานของ agent
    "results": None, "mastery": None, "report": None, "notice": None,
    "files": {},  # ไฟล์ PDF ที่สร้างแล้ว {"exam" | "key" | "report": bytes}
    "sheet": None,  # กระดาษคำตอบที่อัปโหลด {"version", "file": AnswerSheetFile, "result": ExtractedAnswerSheet}
    # โหมดครู (แท็บ 4): ข้อมูลทั้งห้องผูกกับข้อสอบของตัวเอง จึงไม่ถูกล้างเมื่อสร้างข้อสอบชุดใหม่
    "class_session": None,  # ClassSession (บันทึกลงฐานข้อมูลทุกครั้งที่เปลี่ยน)
    "class_sitting": None,  # school_db.Sitting: การสอบนี้คือห้องไหน ภาคเรียนอะไร
    "class_import": None,  # สรุปการนำเข้าครั้งล่าสุด
    "class_upload_n": 0,  # เปลี่ยน key ของช่องอัปโหลดเพื่อล้างไฟล์หลังนำเข้าแล้ว
    "class_review_n": 0,  # เปลี่ยน key ของฟอร์มตรวจทานเมื่อข้อมูลห้องเปลี่ยน (กันค่าเก่าค้าง)
    "class_images": {},  # ภาพกระดาษคำตอบ {sheet_name: [PNG]} (ไม่บันทึกลงฐานข้อมูล เพราะใหญ่)
    "class_exam_pdf": None,  # PDF ข้อสอบของห้องนี้สำหรับพิมพ์
    "item_report": None,  # ผลของ Item Analyst {"key", "report", "seconds"} (key บอกว่าวิเคราะห์จากข้อมูลชุดไหน)
    "item_revision": None,  # ข้อสอบฉบับปรับปรุงจากผลสอบจริง {"key", "result": GenerationResult, "source"}
    # แท็บ 5: ห้องเรียนและคะแนน
    "school_notice": None,  # (ข้อความสำเร็จ, [คำเตือน]) จากการนำเข้ารายชื่อ / กู้คืน
    "school_upload_n": 0,
    "school_backup": None,  # ไฟล์สำรองฐานข้อมูลที่สร้างแล้ว (bytes)
}


def init_state() -> None:
    for key, default in DEFAULTS.items():
        state.setdefault(key, default)


def set_exam(result: GenerationResult, source: Source) -> None:
    """ใช้ข้อสอบชุดใหม่จากกราฟสร้างข้อสอบ"""
    use_exam(result.exam, source)
    state.quality = {q.question_id: q for q in result.quality}
    state.trace = result.trace
    state.rounds = result.rounds


def use_exam(exam: Exam, source: Optional[Source] = None) -> None:
    """ใช้ข้อสอบชุดใหม่ และล้างผลของชุดเก่าทั้งหมด (เช่น ข้อสอบจากไฟล์ข้อมูลห้องที่เปิดมา)"""
    state.exam = exam
    state.source = source
    state.quality, state.trace, state.rounds = {}, [], 0
    state.version += 1
    state.results = state.mastery = state.report = None
    state.files = {}
    state.sheet = None


def run_with_progress(source: Source, **kwargs) -> Optional[GenerationResult]:
    """รันกราฟสร้างข้อสอบ พร้อมแสดงว่า agent ไหนทำอะไรเสร็จแล้วบ้าง"""
    return show_progress(
        "กำลังสร้างข้อสอบ: Generator ออกข้อสอบ → Reviewer ตรวจ → ส่งกลับแก้ข้อที่ไม่ผ่าน", "สร้างข้อสอบ",
        lambda on_step: run_generation(source, **kwargs, on_step=on_step),
    )


def show_progress(
    label: str, task: str, run: Callable[[Callable[[AgentStep], None]], GenerationResult]
) -> Optional[GenerationResult]:
    """รันกราฟฝั่งสร้างข้อสอบ (run รับ on_step) และแสดงทีละขั้นว่า agent ไหนทำอะไรเสร็จแล้ว"""
    with st.status(label, expanded=True) as status:
        try:
            result = run(lambda s: status.write(f"**{s.agent}** · {s.summary} ({s.seconds:g} วินาที)"))
        except LLMError as e:
            status.update(label=f"{task}ไม่สำเร็จ", state="error")
            st.error(str(e))
            return None
        status.update(label=f"{task}เสร็จแล้ว ({sum(s.seconds for s in result.trace):.0f} วินาที)",
                      state="complete", expanded=False)
    return result


def panel_text(result: QuestionResult) -> str:
    """สรุปคณะผู้ตรวจแบบสั้นสำหรับตาราง เช่น "6 / 7 / 5 → ตรงกัน" """
    panel = result.panel
    if panel is None:
        return ""
    scores = " / ".join(f"{g.points:g}" for g in panel.grades)
    text = f"{scores} → " + ("ตรงกัน" if panel.decision == "agreed" else "Moderator ตัดสิน")
    return text + (" ⚠️ ให้ครูดู" if panel.needs_teacher else "")


def show_panel(result: QuestionResult) -> None:
    """ผลของคณะผู้ตรวจเรียงความ: คะแนนของแต่ละคน และสรุปอย่างไร"""
    panel = result.panel
    if panel is None:
        return
    scores = " · ".join(f"{GRADER_LABELS.get(g.grader, g.grader)} {g.points:g}" for g in panel.grades)
    if panel.decision == "agreed":
        st.caption(f"คณะผู้ตรวจ: {scores} — เห็นตรงกัน (ต่างกัน {panel.spread:g} คะแนน) ใช้ผลของผู้ตรวจที่ให้คะแนนกลาง")
    else:
        st.caption(f"คณะผู้ตรวจ: {scores} — เห็นต่าง {panel.spread:g} คะแนน จึงให้ Moderator ตัดสิน")
        st.markdown(f"**Moderator ตัดสิน {result.points_awarded:g}/{result.max_points:g}:** {panel.rationale}")
    if panel.needs_teacher:
        st.warning("ผู้ตรวจให้คะแนนต่างกันมาก ควรให้ผู้สอนตรวจข้อนี้ซ้ำ")
