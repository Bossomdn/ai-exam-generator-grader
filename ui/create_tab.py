"""หน้าออกข้อสอบ: เลือกเนื้อหา → วิเคราะห์คำสั่ง (Request Planner) → ตรวจ/แก้สเปก → สร้างข้อสอบผ่านกราฟ
→ ดูเฉลย / พิมพ์ PDF / นำไปใช้ (สร้างเสร็จแล้วข้อสอบขึ้นก่อน ฟอร์มสร้างชุดใหม่ซ่อนไว้จนกดเปิด)"""

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

from . import nav
from .common import run_with_progress, set_exam, state

SAMPLE = Path(__file__).parent.parent / "samples" / "sample_lecture.txt"
UPLOAD, SAMPLE_TEXT, TOPIC = "อัปโหลดสไลด์/เอกสาร", "ใช้เนื้อหาตัวอย่าง", "พิมพ์หัวข้ออย่างเดียว"
EXAMPLES = {  # กดแล้วใส่ลงช่องคำสั่ง แก้ต่อได้
    "ปรนัยล้วน": "ขอข้อสอบปรนัย 4 ตัวเลือก 10 ข้อ ระดับกลาง เน้นความเข้าใจ",
    "ปรนัย + อัตนัย": (
        "ขอข้อสอบปรนัย 4 ตัวเลือก 5 ข้อ ถูก/ผิด 2 ข้อ อัตนัยตอบสั้น 1 ข้อ และเรียงความ 1 ข้อ (10 คะแนน) "
        "ระดับกลาง เน้นความเข้าใจและการนำไปประยุกต์ใช้"
    ),
    "เรียงความ": "ขอข้อสอบเรียงความ 2 ข้อ ข้อละ 10 คะแนน ระดับยาก เน้นการวิเคราะห์และยกตัวอย่างประกอบ",
    "ทบทวนสั้น ๆ": "ขอแบบทดสอบสั้น ถูก/ผิด 5 ข้อ และปรนัย 4 ตัวเลือก 5 ข้อ ระดับง่าย ทบทวนเนื้อหาหลัก",
}
QUALITY_LABELS = {
    "passed": "✅ ผ่านการตรวจคุณภาพ",
    "fixed": "🔧 แก้ตามผลตรวจแล้วผ่าน",
    "flagged": "⚠️ ผู้ตรวจยังพบปัญหา — ควรแก้เองก่อนนำไปใช้",
    "unchecked": "❔ ผู้ตรวจไม่ได้ตรวจข้อนี้",
}
TYPE_BY_LABEL = {label: t for t, label in TYPE_LABELS.items()}
DIFFICULTY_BY_LABEL = {label: d for d, label in DIFFICULTY_LABELS.items()}


def render(ready: bool) -> None:
    if state.exam:
        show_exam(state.exam)
        st.divider()
        if not state.create_open:
            if st.button("✏️ สร้างข้อสอบชุดใหม่"):
                state.create_open = True
                st.rerun()
            return
        st.subheader("สร้างข้อสอบชุดใหม่")
    else:
        st.caption("ใส่เนื้อหาและบอกว่าอยากได้ข้อสอบแบบไหน → ตรวจสเปก → ให้ agent ออกข้อสอบ ตรวจคุณภาพ และแก้ข้อที่ไม่ผ่าน")
    st.markdown("#### 1. เนื้อหาที่จะออกสอบ")
    source = choose_source()
    st.markdown("#### 2. อยากได้ข้อสอบแบบไหน")
    user_prompt = prompt_input()
    plan_and_generate(source, user_prompt, ready)


def choose_source() -> Optional[Source]:
    mode = st.radio("แหล่งเนื้อหา", [UPLOAD, SAMPLE_TEXT, TOPIC], horizontal=True, key="source_mode")
    source = None
    if mode == UPLOAD:
        source = uploaded_source()
        if source and source.kind == "pptx":
            with st.expander("ข้อความที่อ่านได้จากสไลด์"):
                st.text(source.text)
    elif mode == SAMPLE_TEXT:
        source = load_text(SAMPLE.read_bytes(), SAMPLE.name)
        with st.expander("ดูเนื้อหาตัวอย่าง (Machine Learning เบื้องต้น)"):
            st.text(source.text)
    else:
        topic = st.text_input("หัวข้อที่จะออกสอบ", placeholder="เช่น ระบบสุริยะ, Python เบื้องต้น, เศรษฐศาสตร์จุลภาค",
                              key="source_topic")
        if topic.strip():
            source = from_topic(topic)
    return source


def uploaded_source() -> Optional[Source]:
    """ไฟล์ที่อัปโหลด (จำไว้ เพราะช่องอัปโหลดจะว่างเมื่อสลับไปหน้าอื่นแล้วกลับมา)"""
    uploaded = st.file_uploader("ไฟล์สไลด์หรือเอกสาร", type=["pptx", "pdf", "txt", "md"])
    if uploaded:
        try:
            state.uploaded_source = load_upload(uploaded.name, uploaded.getvalue())
        except Exception as e:  # ไฟล์เสียหรืออ่านไม่ได้
            state.uploaded_source = None
            st.error(f"อ่านไฟล์ไม่ได้: {e}")
    elif state.uploaded_source:
        st.caption(f"ใช้ไฟล์ **{state.uploaded_source.name}** ที่อัปโหลดไว้ (อัปโหลดไฟล์ใหม่เพื่อเปลี่ยน)")
    return state.uploaded_source


def prompt_input() -> str:
    def use_example() -> None:
        if state.create_example:
            state.create_prompt = EXAMPLES[state.create_example]
            state.create_example = None  # ทำตัวเป็นปุ่ม ไม่ค้างสถานะเลือก

    st.pills("ตัวอย่างคำสั่ง (กดเพื่อใส่ลงช่องด้านล่าง แล้วแก้ต่อได้)", list(EXAMPLES), key="create_example",
             on_change=use_example)
    return st.text_area("อยากได้ข้อสอบแนวไหน แบบไหน กี่ข้อ", key="create_prompt", height=100,
                        placeholder="เช่น " + EXAMPLES["ปรนัย + อัตนัย"])


def plan_key(source: Source, prompt: str) -> tuple:
    """ใช้ตรวจว่าสเปกที่วิเคราะห์ไว้ยังตรงกับเนื้อหาและคำสั่งปัจจุบันไหม"""
    return source.kind, source.name, hash(source.text or source.pdf_b64), prompt.strip()


def plan_and_generate(source: Optional[Source], user_prompt: str, ready: bool) -> None:
    """วิเคราะห์คำสั่งเป็นสเปก → ผู้ใช้ตรวจสเปกแล้วสั่งสร้าง"""
    missing = [name for name, ok in (("เนื้อหา", source), ("คำสั่ง", user_prompt.strip())) if not ok]
    if st.button("วิเคราะห์คำสั่ง", type="primary", disabled=bool(missing) or not ready):
        with st.spinner("Request Planner กำลังแปลงคำสั่งเป็นสเปกข้อสอบ..."):
            started = time.perf_counter()
            try:
                planned = plan_exam(source, user_prompt)
                total = sum(s.count for s in planned.sections)
                for key in [k for k in state if k.startswith("spec_")]:  # ค่าในช่องสเปกของรอบก่อน
                    del state[key]
                state.plan_n += 1
                state.plan = {
                    "key": plan_key(source, user_prompt),
                    "spec": planned,
                    "step": AgentStep(
                        agent="Request Planner",
                        summary=f"แปลงคำสั่งเป็นสเปก: {total} ข้อ, {len(planned.concepts)} หัวข้อ (ผู้ใช้ตรวจแล้ว)",
                        seconds=round(time.perf_counter() - started, 1),
                    ),
                    "n": state.plan_n,
                }
            except LLMError as e:
                st.error(str(e))
    if missing:
        st.caption(f"ใส่{'และ'.join(missing)}ก่อน จึงจะกดวิเคราะห์คำสั่งได้")

    plan = state.plan
    if not (plan and source):
        return
    if plan["key"] != plan_key(source, user_prompt):
        st.info("เนื้อหาหรือคำสั่งเปลี่ยนไปแล้ว กด **วิเคราะห์คำสั่ง** อีกครั้งเพื่อทำสเปกใหม่")
        return
    st.markdown("#### 3. ตรวจสเปกข้อสอบก่อนสร้าง")
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
            state.plan = None
            state.create_open = False
            state.notice = f"สร้าง “{result.exam.title}” เสร็จแล้ว ({sum(s.seconds for s in result.trace):.0f} วินาที)"
            st.rerun()


def table_number(value) -> float:
    """ค่าตัวเลขจากตาราง (ช่องที่ว่างอาจเป็น None หรือ NaN) — ว่าง = 0"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(number) else number


def render_spec_editor(spec: ExamSpec, n: int) -> ExamSpec:
    """ให้ผู้ใช้ตรวจ/แก้สเปกจาก Request Planner ก่อนสร้างข้อสอบ (human-in-the-loop)"""
    st.caption("Request Planner แปลงคำสั่งเป็นสเปกด้านล่าง แก้ได้ทุกช่อง (เพิ่ม/ลบแถวในตารางได้)")
    if spec.assumptions:
        st.info("**คำสั่งไม่ได้ระบุสิ่งต่อไปนี้ ระบบจึงตั้งค่าให้:**\n" + "\n".join(f"- {a}" for a in spec.assumptions))
    # ตั้งค่าเริ่มผ่าน state (ไม่ใช้ value=) เพื่อให้ค่าที่แก้อยู่ต่อเมื่อสลับหน้า (common.keep_inputs)
    state.setdefault(f"spec_title_{n}", spec.title)
    state.setdefault(f"spec_focus_{n}", spec.focus)
    state.setdefault(f"spec_concepts_{n}", "\n".join(spec.concepts))
    title = st.text_input("ชื่อชุดข้อสอบ", key=f"spec_title_{n}")
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
    focus = st.text_area("แนวข้อสอบ / สิ่งที่เน้น", key=f"spec_focus_{n}", height=70)
    concepts = st.text_area("หัวข้อที่จะออกสอบ (บรรทัดละ 1 หัวข้อ)", key=f"spec_concepts_{n}", height=150)
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
    """สรุปข้อสอบ ผลตรวจคุณภาพ บันทึกการทำงานของ agent เฉลย ปุ่มพิมพ์ PDF และทางไปใช้ข้อสอบต่อ"""
    st.subheader(exam.title)
    type_counts = Counter(q.type for q in exam.questions)
    col_n, col_points, col_quality = st.columns(3)
    col_n.metric("จำนวนข้อ", len(exam.questions))
    col_points.metric("คะแนนเต็ม", f"{sum(q.points for q in exam.questions):g}")
    if state.quality:
        good = sum(1 for q in state.quality.values() if q.status in ("passed", "fixed"))
        col_quality.metric("ผ่าน Quality Reviewer", f"{good}/{len(exam.questions)} ข้อ")
    st.caption(" · ".join(f"{TYPE_LABELS[t]} {n} ข้อ" for t, n in type_counts.items())
               + " · หัวข้อที่วัด: " + ", ".join(exam.concepts))
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
    with st.expander("ดูข้อสอบพร้อมเฉลย (สำหรับผู้สอน)"):
        for q in exam.questions:
            render_answer_key(q, state.quality.get(q.id))

    st.markdown("#### นำข้อสอบไปใช้")
    col_class, col_practice = st.columns(2)
    with col_class:
        nav.link(nav.CLASS, "สอบทั้งห้องบนกระดาษ แล้วให้ AI ตรวจ")
    with col_practice:
        nav.link(nav.PRACTICE, "ให้ผู้เรียนทำในแอป")
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
