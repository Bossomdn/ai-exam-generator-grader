"""แท็บ 1: เลือกเนื้อหา → วิเคราะห์คำสั่ง (Request Planner) → ตรวจ/แก้สเปก → สร้างข้อสอบผ่านกราฟ → ดูเฉลย / พิมพ์ PDF"""

import math
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import streamlit as st

from src import config, export, scoring
from src.llm import LLMError
from src.planner import describe_spec, normalize_spec, plan_exam, spec_problems
from src.schemas import (
    DIFFICULTY_LABELS, OBJECTIVE_TYPES, TYPE_LABELS, AgentStep, Exam, ExamSpec, Question, QuestionQuality,
    SectionSpec,
)
from src.sources import Source, from_topic, load_text, load_upload

from .common import run_with_progress, set_exam, state

SAMPLE = Path(__file__).parent.parent / "samples" / "sample_lecture.txt"
EXAMPLE_PROMPT = (
    "ขอข้อสอบปรนัย 4 ตัวเลือก 5 ข้อ ถูก/ผิด 2 ข้อ อัตนัยตอบสั้น 1 ข้อ และเรียงความ 1 ข้อ (10 คะแนน) "
    "ระดับกลาง เน้นความเข้าใจและการนำไปประยุกต์ใช้"
)
QUALITY_LABELS = {
    "passed": "✅ ผ่านการตรวจคุณภาพ",
    "fixed": "🔧 แก้ตามผลตรวจแล้วผ่าน",
    "flagged": "⚠️ ผู้ตรวจยังพบปัญหา — ควรแก้เองก่อนนำไปใช้",
    "unchecked": "❔ ผู้ตรวจไม่ได้ตรวจข้อนี้",
}
TYPE_BY_LABEL = {label: t for t, label in TYPE_LABELS.items()}
DIFFICULTY_BY_LABEL = {label: d for d, label in DIFFICULTY_LABELS.items()}


def render(ready: bool) -> None:
    source = choose_source()
    user_prompt = st.text_area("อยากได้ข้อสอบแนวไหน แบบไหน กี่ข้อ", value=EXAMPLE_PROMPT, height=100)
    plan_and_generate(source, user_prompt, ready)
    if state.exam:
        show_exam(state.exam)


def choose_source(*, key_prefix: str = "create") -> Optional[Source]:
    mode = st.radio("แหล่งเนื้อหา", ["อัปโหลดสไลด์/เอกสาร", "ใช้เนื้อหาตัวอย่าง", "พิมพ์หัวข้ออย่างเดียว"], horizontal=True, key=f"{key_prefix}_source_mode")
    source = None
    if mode == "อัปโหลดสไลด์/เอกสาร":
        uploaded = st.file_uploader("ไฟล์สไลด์หรือเอกสาร", type=["pptx", "pdf", "txt", "md"], key=f"{key_prefix}_source_file")
        if uploaded:
            try:
                source = load_upload(uploaded.name, uploaded.getvalue())
            except Exception as e:  # ไฟล์เสียหรืออ่านไม่ได้
                st.error(f"อ่านไฟล์ไม่ได้: {e}")
        if source and source.kind == "pptx":
            with st.expander("ข้อความที่อ่านได้จากสไลด์"):
                st.text(source.text)
    elif mode == "ใช้เนื้อหาตัวอย่าง":
        source = load_text(SAMPLE.read_bytes(), SAMPLE.name)
        with st.expander("ดูเนื้อหาตัวอย่าง (Machine Learning เบื้องต้น)"):
            st.text(source.text)
    else:
        topic = st.text_input("หัวข้อที่จะออกสอบ", placeholder="เช่น ระบบสุริยะ, Python เบื้องต้น, เศรษฐศาสตร์จุลภาค",
                              key=f"{key_prefix}_source_topic")
        if topic.strip():
            source = from_topic(topic)
    return source


def plan_key(source: Source, prompt: str) -> tuple:
    """ใช้ตรวจว่าสเปกที่วิเคราะห์ไว้ยังตรงกับเนื้อหาและคำสั่งปัจจุบันไหม"""
    return source.kind, source.name, hash(source.text or source.pdf_b64), prompt.strip()


def plan_and_generate(source: Optional[Source], user_prompt: str, ready: bool) -> None:
    """ขั้นที่ 1 วิเคราะห์คำสั่งเป็นสเปก → ขั้นที่ 2 ผู้ใช้ตรวจสเปกแล้วสั่งสร้าง"""
    if st.button("วิเคราะห์คำสั่ง", type="primary", disabled=source is None or not ready):
        with st.spinner("Request Planner กำลังแปลงคำสั่งเป็นสเปกข้อสอบ..."):
            started = time.perf_counter()
            try:
                planned = plan_exam(source, user_prompt)
                total = sum(s.count for s in planned.sections)
                state.plan = {
                    "key": plan_key(source, user_prompt),
                    "spec": planned,
                    "step": AgentStep(
                        agent="Request Planner",
                        summary=f"แปลงคำสั่งเป็นสเปก: {total} ข้อ, {len(planned.concepts)} หัวข้อ (ผู้ใช้ตรวจแล้ว)",
                        seconds=round(time.perf_counter() - started, 1),
                    ),
                    "n": (state.plan or {}).get("n", 0) + 1,
                }
            except LLMError as e:
                st.error(str(e))

    plan = state.plan
    if not (plan and source):
        return
    if plan["key"] != plan_key(source, user_prompt):
        st.info("เนื้อหาหรือคำสั่งเปลี่ยนไปแล้ว กด **วิเคราะห์คำสั่ง** อีกครั้งเพื่อทำสเปกใหม่")
        return
    spec = render_spec_editor(plan["spec"], plan["n"])
    problems = spec_problems(spec)
    for problem in problems:
        st.error(problem)
    if spec.sections:
        st.caption("สรุปสเปก: " + describe_spec(spec).split("\n")[0])
    if st.button("สร้างข้อสอบตามสเปกนี้", type="primary", disabled=bool(problems) or not ready):
        result = run_with_progress(source, spec=spec, prior_steps=[plan["step"]])
        if result:
            set_exam(result, source)


def table_number(value) -> float:
    """ค่าตัวเลขจากตาราง (ช่องที่ว่างอาจเป็น None หรือ NaN) — ว่าง = 0"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def render_spec_editor(spec: ExamSpec, n: int) -> ExamSpec:
    """ให้ผู้ใช้ตรวจ/แก้สเปกจาก Request Planner ก่อนสร้างข้อสอบ (human-in-the-loop)"""
    st.markdown("#### ตรวจสเปกข้อสอบก่อนสร้าง")
    st.caption("Request Planner แปลงคำสั่งเป็นสเปกด้านล่าง แก้ได้ทุกช่อง (เพิ่ม/ลบแถวในตารางได้)")
    if spec.assumptions:
        st.info("**คำสั่งไม่ได้ระบุสิ่งต่อไปนี้ ระบบจึงตั้งค่าให้:**\n" + "\n".join(f"- {a}" for a in spec.assumptions))
    title = st.text_input("ชื่อชุดข้อสอบ", value=spec.title, key=f"spec_title_{n}")
    rows = st.data_editor(
        [{
            "ชนิด": TYPE_LABELS[s.type],
            "จำนวนข้อ": s.count,
            "คะแนนต่อข้อ": s.points_each,
            "ความยาก": DIFFICULTY_LABELS[s.difficulty],
        } for s in spec.sections],
        column_config={
            "ชนิด": st.column_config.SelectboxColumn(options=list(TYPE_LABELS.values()), required=True),
            "จำนวนข้อ": st.column_config.NumberColumn(min_value=1, max_value=config.MAX_QUESTIONS, step=1, required=True),
            "คะแนนต่อข้อ": st.column_config.NumberColumn(min_value=0.5, step=0.5, required=True),
            "ความยาก": st.column_config.SelectboxColumn(options=list(DIFFICULTY_LABELS.values()), required=True),
        },
        num_rows="dynamic",
        hide_index=True,
        key=f"spec_sections_{n}",
    )
    focus = st.text_area("แนวข้อสอบ / สิ่งที่เน้น", value=spec.focus, key=f"spec_focus_{n}", height=70)
    concepts = st.text_area(
        "หัวข้อที่จะออกสอบ (บรรทัดละ 1 หัวข้อ)", value="\n".join(spec.concepts), key=f"spec_concepts_{n}", height=150
    )
    sections = [  # แถวที่กรอกไม่ครบจะถูกข้าม (normalize_spec ตัดแถว 0 ข้อ และเติมคะแนนที่ว่าง)
        SectionSpec(
            type=TYPE_BY_LABEL[r["ชนิด"]],
            count=int(table_number(r.get("จำนวนข้อ"))),
            points_each=table_number(r.get("คะแนนต่อข้อ")),
            difficulty=DIFFICULTY_BY_LABEL[r["ความยาก"]],
        )
        for r in rows
        if r.get("ชนิด") in TYPE_BY_LABEL and r.get("ความยาก") in DIFFICULTY_BY_LABEL
    ]
    return normalize_spec(ExamSpec(
        title=title, sections=sections, focus=focus, concepts=concepts.splitlines(), assumptions=spec.assumptions,
    ))


def show_exam(exam: Exam) -> None:
    """สรุปข้อสอบ ผลตรวจคุณภาพ บันทึกการทำงานของ agent เฉลย และปุ่มพิมพ์ PDF"""
    st.divider()
    st.subheader(exam.title)
    st.info(f"**สเปก:** {exam.spec_summary}")
    if state.quality:
        counts = Counter(q.status for q in state.quality.values())
        parts = [f"{QUALITY_LABELS[s].split(' —')[0]} {counts[s]} ข้อ" for s in QUALITY_LABELS if counts[s]]
        rounds = f" · ส่งกลับแก้ {state.rounds} รอบ" if state.rounds else ""
        st.markdown("**ผลตรวจคุณภาพโดย Quality Reviewer:** " + " · ".join(parts) + rounds)
        flagged = [i for i, q in state.quality.items() if q.status == "flagged"]
        if flagged:
            st.warning(
                f"ข้อ {', '.join(map(str, flagged))} ยังมีปัญหาหลังแก้ครบ {config.MAX_REVISIONS} รอบ "
                "ดูรายละเอียดในเฉลยด้านล่าง แล้วแก้หรือตัดทิ้งก่อนนำไปใช้จริง"
            )
    if state.trace:
        with st.expander("ขั้นตอนการทำงานของ agent"):
            st.dataframe(
                [{"ขั้น": i, "Agent": s.agent, "ทำอะไร": s.summary, "เวลา (วินาที)": s.seconds}
                 for i, s in enumerate(state.trace, 1)],
                hide_index=True,
            )
    if state.source and state.source.is_topic_only:
        st.warning("ข้อสอบนี้สร้างจากความรู้ของ AI (ไม่มีเอกสารอ้างอิง) ควรตรวจทานเฉลยก่อนนำไปใช้จริง")
    type_counts: dict[str, int] = {}
    for q in exam.questions:
        type_counts[q.type] = type_counts.get(q.type, 0) + 1
    total_points = sum(q.points for q in exam.questions)
    st.markdown(
        " · ".join(f"{TYPE_LABELS[t]} {n} ข้อ" for t, n in type_counts.items())
        + f" · รวม {total_points:g} คะแนน"
    )
    st.caption("หัวข้อที่วัด: " + ", ".join(exam.concepts))
    with st.expander("ดูข้อสอบพร้อมเฉลย (สำหรับผู้สอน)"):
        for q in exam.questions:
            render_answer_key(q, state.quality.get(q.id))
    st.markdown("#### พิมพ์ข้อสอบ")
    if st.button("📄 สร้างไฟล์ PDF (ข้อสอบ + เฉลย)"):
        with st.spinner("กำลังสร้าง PDF..."):
            try:
                state.files["exam"] = export.exam_pdf(exam)
                state.files["key"] = export.answer_key_pdf(exam)
            except export.ExportError as e:
                st.error(str(e))
    if "exam" in state.files:
        col_exam, col_key = st.columns(2)
        col_exam.download_button(
            "⬇️ ข้อสอบ (ไม่มีเฉลย) — แจกผู้สอบ", state.files["exam"],
            file_name="exam.pdf", mime="application/pdf",
        )
        col_key.download_button(
            "⬇️ เฉลย + เกณฑ์ให้คะแนน — สำหรับผู้สอน", state.files["key"],
            file_name="answer_key.pdf", mime="application/pdf",
        )
    st.download_button(
        "ดาวน์โหลดข้อมูลข้อสอบ (JSON)",
        exam.model_dump_json(indent=2),
        file_name="exam.json",
        mime="application/json",
    )
    st.success("ข้อสอบพร้อมแล้ว ไปทำข้อสอบที่แท็บ **2. ทำข้อสอบ**")


def render_answer_key(q: Question, quality: Optional[QuestionQuality] = None) -> None:
    st.markdown(
        f"**ข้อ {q.id}** · {TYPE_LABELS[q.type]} · {q.points:g} คะแนน · "
        f"หัวข้อ: {q.concept} · ที่มา: {q.source_ref} · Bloom: {q.bloom_level} · ความยาก: {q.difficulty}"
    )
    if quality:
        issues = "\n".join(f"- {i}" for i in quality.issues)
        if quality.status == "flagged":
            st.warning(f"{QUALITY_LABELS['flagged']}\n{issues}")
        elif quality.status == "fixed":
            st.caption(f"{QUALITY_LABELS['fixed']} ({quality.revisions} ครั้ง) · ปัญหาที่เคยพบ: " + " / ".join(quality.issues))
        else:
            st.caption(QUALITY_LABELS[quality.status])
    st.markdown(q.question)
    if q.type in OBJECTIVE_TYPES:
        for i in range(len(q.choices)):
            mark = "✅" if i == q.correct_choice else "▫️"
            st.markdown(f"{mark} {scoring.choice_label(q, i)}")
    else:
        st.markdown(f"**คำตอบตัวอย่าง:** {q.model_answer}")
        if q.key_points:
            st.markdown("**ประเด็นที่ต้องมี:**\n" + "\n".join(f"- {kp}" for kp in q.key_points))
        if q.rubric:
            st.markdown("**Rubric:**\n" + "\n".join(
                f"- {c.name} ({c.max_points:g} คะแนน): {c.description}" for c in q.rubric
            ))
    st.caption(f"คำอธิบาย: {q.explanation}")
    st.divider()
