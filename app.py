"""ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ (multi-agent)

รัน: streamlit run app.py
"""

import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Optional

import streamlit as st

from src import answer_reader, config, export, scoring
from src.advisor import advise
from src.generation_graph import GenerationResult, run_generation
from src.grader import grade_exam
from src.llm import LLMError, check_ready
from src.planner import describe_spec, normalize_spec, plan_exam, spec_problems
from src.schemas import (
    DIFFICULTY_LABELS, OBJECTIVE_TYPES, TYPE_LABELS, AgentStep, Exam, ExamSpec, Question, QuestionQuality,
    SectionSpec,
)
from src.sources import Source, from_topic, load_text, load_upload

SAMPLE = Path(__file__).parent / "samples" / "sample_lecture.txt"
EXAMPLE_PROMPT = (
    "ขอข้อสอบปรนัย 4 ตัวเลือก 5 ข้อ ถูก/ผิด 2 ข้อ อัตนัยตอบสั้น 1 ข้อ และเรียงความ 1 ข้อ (10 คะแนน) "
    "ระดับกลาง เน้นความเข้าใจและการนำไปประยุกต์ใช้"
)

st.set_page_config(page_title="AI Exam Generator & Grader", page_icon="📝", layout="wide")

QUALITY_LABELS = {
    "passed": "✅ ผ่านการตรวจคุณภาพ",
    "fixed": "🔧 แก้ตามผลตรวจแล้วผ่าน",
    "flagged": "⚠️ ผู้ตรวจยังพบปัญหา — ควรแก้เองก่อนนำไปใช้",
    "unchecked": "❔ ผู้ตรวจไม่ได้ตรวจข้อนี้",
}
TYPE_BY_LABEL = {label: t for t, label in TYPE_LABELS.items()}
DIFFICULTY_BY_LABEL = {label: d for d, label in DIFFICULTY_LABELS.items()}

state = st.session_state
for key, default in {
    "source": None, "exam": None, "version": 0,
    "plan": None,  # สเปกจาก Request Planner ที่รอผู้ใช้ยืนยัน {"key", "spec", "step", "n"}
    "quality": {}, "trace": [], "rounds": 0,  # ผลตรวจคุณภาพรายข้อ และบันทึกการทำงานของ agent
    "results": None, "mastery": None, "report": None, "notice": None,
    "files": {},  # ไฟล์ PDF ที่สร้างแล้ว {"exam" | "key" | "report": bytes}
    "sheet": None,  # กระดาษคำตอบที่อัปโหลด {"version", "file": AnswerSheetFile, "result": ExtractedAnswerSheet}
}.items():
    state.setdefault(key, default)

ready, ready_message = check_ready()


def set_exam(result: GenerationResult, source: Source) -> None:
    state.exam = result.exam
    state.source = source
    state.quality = {q.question_id: q for q in result.quality}
    state.trace = result.trace
    state.rounds = result.rounds
    state.version += 1
    state.results = state.mastery = state.report = None
    state.files = {}
    state.sheet = None


def run_with_progress(source: Source, **kwargs) -> Optional[GenerationResult]:
    """รันกราฟสร้างข้อสอบ พร้อมแสดงว่า agent ไหนทำอะไรเสร็จแล้วบ้าง"""
    with st.status("กำลังสร้างข้อสอบ: Generator ออกข้อสอบ → Reviewer ตรวจ → ส่งกลับแก้ข้อที่ไม่ผ่าน", expanded=True) as status:
        try:
            result = run_generation(
                source, **kwargs,
                on_step=lambda s: status.write(f"**{s.agent}** · {s.summary} ({s.seconds:g} วินาที)"),
            )
        except LLMError as e:
            status.update(label="สร้างข้อสอบไม่สำเร็จ", state="error")
            st.error(str(e))
            return None
        status.update(label=f"สร้างข้อสอบเสร็จแล้ว ({sum(s.seconds for s in result.trace):.0f} วินาที)",
                      state="complete", expanded=False)
    return result


def plan_key(source: Source, prompt: str) -> tuple:
    """ใช้ตรวจว่าสเปกที่วิเคราะห์ไว้ยังตรงกับเนื้อหาและคำสั่งปัจจุบันไหม"""
    return source.kind, source.name, hash(source.text or source.pdf_b64), prompt.strip()


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


def render_answer_form(exam: Exam, v: int, upload_mode: bool, extracted_by_id: dict) -> bool:
    """ฟอร์มตอบข้อสอบ (ใช้ตรวจทานคำตอบที่ถอดจากไฟล์ด้วย) คืน True เมื่อกดส่งตรวจ"""
    with st.form(f"exam_form_{v}"):
        for q in exam.questions:
            st.markdown(f"**ข้อ {q.id}.** {q.question}")
            st.caption(f"{TYPE_LABELS[q.type]} · {q.points:g} คะแนน")
            key = f"ans_{v}_{q.id}"
            if upload_mode:
                a = extracted_by_id.get(q.id)
                if a is None:
                    st.warning("⚠️ ไม่พบคำตอบข้อนี้ในไฟล์ — ตรวจดูว่าผู้สอบไม่ได้ตอบ หรือระบบอ่านไม่เจอ")
                elif a.answer and q.type in OBJECTIVE_TYPES and answer_reader.to_form_value(q, a) is None:
                    st.warning(f"⚠️ อ่านได้ว่า “{a.answer}” แต่จับคู่กับตัวเลือกไม่ได้ กรุณาเลือกเอง")
                elif mismatch := answer_reader.consistency_warning(q, a):
                    st.warning(f"⚠️ {mismatch} — อาจอ่านเครื่องหมายผิด กรุณาเทียบกับภาพ")
                elif a.confidence != "high":
                    level = "อ่านไม่ค่อยออก" if a.confidence == "low" else "ไม่แน่ใจบางส่วน"
                    st.warning(f"⚠️ {level}" + (f" — {a.note}" if a.note else "") + " · กรุณาตรวจทาน")
            if q.type in OBJECTIVE_TYPES:
                # ไม่ส่ง index ถ้ามีค่าใน session_state แล้ว (เช่น ค่าที่ถอดจากไฟล์) เพื่อไม่ให้ Streamlit เตือนค่าซ้อน
                default = {} if key in state else {"index": None}
                st.radio(
                    f"คำตอบข้อ {q.id}",
                    options=list(range(len(q.choices))),
                    format_func=lambda i, q=q: scoring.choice_label(q, i),
                    key=key,
                    label_visibility="collapsed",
                    **default,
                )
            else:
                st.text_area(
                    f"คำตอบข้อ {q.id}",
                    key=key,
                    height=120 if q.type == "short_answer" else 260,
                    placeholder="พิมพ์คำตอบที่นี่",
                    label_visibility="collapsed",
                )
            st.write("")
        return st.form_submit_button("ส่งคำตอบให้ตรวจ", type="primary", disabled=not ready)


# ---------------- Sidebar ----------------
with st.sidebar:
    st.header("สถานะระบบ")
    if ready:
        st.success(f"✅ {ready_message}")
    else:
        st.error(ready_message)
    st.caption(f"ผู้ให้บริการ: `{config.PROVIDER}` · โมเดล: `{config.model_name()}`")
    st.caption("เปลี่ยนได้ที่ `LLM_PROVIDER` ในไฟล์ `.env`")
    st.markdown(
        "**ขั้นตอน**\n"
        "1. ใส่เนื้อหา + สั่งว่าอยากได้ข้อสอบแบบไหน → ตรวจสเปก → สร้าง\n"
        "2. ทำข้อสอบแล้วส่งตรวจ\n"
        "3. ดูคะแนนและเรื่องที่ต้องเรียนเพิ่ม"
    )
    st.markdown(
        "**Agent ฝั่งสร้างข้อสอบ**\n"
        "- Request Planner — แปลงคำสั่งเป็นสเปก\n"
        "- Question Generator — ออกข้อสอบ + เฉลย + เกณฑ์\n"
        f"- Quality Reviewer — ทำข้อสอบเองโดยไม่เห็นเฉลย หาจุดบกพร่อง แล้วส่งกลับแก้ (สูงสุด {config.MAX_REVISIONS} รอบ)"
    )
    if st.button("เริ่มใหม่ทั้งหมด"):
        state.clear()
        st.rerun()

st.title("📝 ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ")
st.caption("Multi-Agent System for Automated Assessment Generation and Evaluative Essay Grading")

if state.notice:
    st.success(state.notice)
    state.notice = None

tab_create, tab_take, tab_result = st.tabs(["1. สร้างข้อสอบ", "2. ทำข้อสอบ", "3. ผลและคำแนะนำ"])

# ---------------- Tab 1: สร้างข้อสอบ ----------------
with tab_create:
    mode = st.radio("แหล่งเนื้อหา", ["อัปโหลดสไลด์/เอกสาร", "ใช้เนื้อหาตัวอย่าง", "พิมพ์หัวข้ออย่างเดียว"], horizontal=True)
    source = None
    if mode == "อัปโหลดสไลด์/เอกสาร":
        uploaded = st.file_uploader("ไฟล์สไลด์หรือเอกสาร", type=["pptx", "pdf", "txt", "md"])
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
        topic = st.text_input("หัวข้อที่จะออกสอบ", placeholder="เช่น ระบบสุริยะ, Python เบื้องต้น, เศรษฐศาสตร์จุลภาค")
        if topic.strip():
            source = from_topic(topic)

    user_prompt = st.text_area("อยากได้ข้อสอบแนวไหน แบบไหน กี่ข้อ", value=EXAMPLE_PROMPT, height=100)

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
    if plan and source:
        if plan["key"] != plan_key(source, user_prompt):
            st.info("เนื้อหาหรือคำสั่งเปลี่ยนไปแล้ว กด **วิเคราะห์คำสั่ง** อีกครั้งเพื่อทำสเปกใหม่")
        else:
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

    exam: Exam | None = state.exam
    if exam:
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
        counts: dict[str, int] = {}
        for q in exam.questions:
            counts[q.type] = counts.get(q.type, 0) + 1
        total_points = sum(q.points for q in exam.questions)
        st.markdown(
            " · ".join(f"{TYPE_LABELS[t]} {n} ข้อ" for t, n in counts.items())
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

# ---------------- Tab 2: ทำข้อสอบ ----------------
with tab_take:
    exam = state.exam
    if not exam:
        st.info("สร้างข้อสอบที่แท็บ 1 ก่อน")
    else:
        v = state.version
        st.subheader(exam.title)
        upload_mode = st.radio(
            "วิธีส่งคำตอบ", ["ตอบในแอป", "อัปโหลดกระดาษคำตอบ (PDF / รูปถ่าย)"], horizontal=True, key="answer_mode"
        ) != "ตอบในแอป"
        sheet = state.sheet if state.sheet and state.sheet["version"] == v else None

        if upload_mode:
            uploaded = st.file_uploader(
                "กระดาษคำตอบของผู้สอบ 1 คน", type=["pdf", "png", "jpg", "jpeg"], key=f"sheet_upload_{v}"
            )
            if st.button("อ่านคำตอบจากไฟล์", disabled=uploaded is None or not ready):
                with st.spinner("กำลังอ่านกระดาษคำตอบด้วย AI..."):
                    try:
                        sheet_file = answer_reader.load_answer_sheet(uploaded.name, uploaded.getvalue())
                        extracted = answer_reader.read_answers(exam, sheet_file)
                        by_id = {a.question_id: a for a in extracted.answers}
                        for q in exam.questions:  # เติมคำตอบลงฟอร์มให้ตรวจทาน
                            state[f"ans_{v}_{q.id}"] = answer_reader.to_form_value(q, by_id.get(q.id))
                        if extracted.student_name:
                            state["student_name"] = extracted.student_name
                        state.sheet = sheet = {"version": v, "file": sheet_file, "result": extracted}
                    except (answer_reader.SheetError, LLMError) as e:
                        st.error(str(e))
            if sheet:
                sheet_file, extracted = sheet["file"], sheet["result"]
                name_text = f" · ชื่อผู้สอบ: **{extracted.student_name}**" if extracted.student_name else ""
                st.info(
                    f"อ่านจาก **{sheet_file.name}** ({sheet_file.pages} หน้า){name_text}\n\n"
                    "ตรวจทานคำตอบด้านล่างเทียบกับภาพกระดาษคำตอบ แก้ส่วนที่อ่านผิด แล้วกด **ส่งคำตอบให้ตรวจ**"
                )
                if sheet_file.truncated:
                    st.warning(f"ไฟล์มีมากกว่า {answer_reader.MAX_PAGES} หน้า ระบบอ่านเฉพาะ {answer_reader.MAX_PAGES} หน้าแรก")
                if sheet_file.images:
                    with st.expander("ดูภาพกระดาษคำตอบ (ไว้เทียบตอนตรวจทาน)"):
                        for page_no, image in enumerate(sheet_file.images, start=1):
                            st.image(image, caption=f"หน้า {page_no}")

        if not upload_mode or sheet is not None:
            extracted_by_id = {a.question_id: a for a in sheet["result"].answers} if upload_mode else {}
            if render_answer_form(exam, v, upload_mode, extracted_by_id):
                answers = {q.id: state.get(f"ans_{v}_{q.id}") for q in exam.questions}
                state.files.pop("report", None)
                try:
                    with st.spinner("กำลังตรวจคำตอบ..."):
                        state.results = grade_exam(exam, answers)
                        state.mastery = scoring.concept_mastery(state.results)
                    with st.spinner("กำลังวิเคราะห์จุดที่ต้องเรียนเพิ่ม..."):
                        state.report = advise(exam, state.results, state.mastery)
                    st.success("ตรวจเสร็จแล้ว ดูผลที่แท็บ **3. ผลและคำแนะนำ**")
                except LLMError as e:
                    st.error(str(e))

# ---------------- Tab 3: ผลและคำแนะนำ ----------------
with tab_result:
    exam, results, mastery, report = state.exam, state.results, state.mastery, state.report
    if not results:
        st.info("ทำข้อสอบและส่งตรวจที่แท็บ 2 ก่อน")
    else:
        t = scoring.totals(results)
        pct = 100 * t["score"] / t["max"] if t["max"] else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("คะแนนรวม", f"{t['score']:g} / {t['max']:g} ({pct:.0f}%)")
        c2.metric("ตอนปรนัย", f"{t['objective_score']:g} / {t['objective_max']:g}" if t["objective_max"] else "–")
        c3.metric("ตอนอัตนัย", f"{t['subjective_score']:g} / {t['subjective_max']:g}" if t["subjective_max"] else "–")

        st.subheader("คะแนนรายหัวข้อ")
        st.caption(f"หัวข้อที่ต่ำกว่า {config.WEAK_THRESHOLD:g}% ถือว่าต้องเรียนเพิ่ม")
        for m in mastery:
            flag = "⚠️ ต้องเรียนเพิ่ม · " if m.percent < config.WEAK_THRESHOLD else "✓ "
            st.progress(
                min(m.percent / 100, 1.0),
                text=f"{flag}{m.concept} — {m.percent:.0f}% ({m.points_awarded:g}/{m.max_points:g}) · {', '.join(m.source_refs)}",
            )
        with st.expander("ดูเป็นตาราง"):
            st.dataframe(
                [{
                    "หัวข้อ": m.concept,
                    "คะแนน (%)": m.percent,
                    "ได้": m.points_awarded,
                    "เต็ม": m.max_points,
                    "ข้อ": ", ".join(map(str, m.question_ids)),
                    "ที่มา": ", ".join(m.source_refs),
                } for m in mastery],
                hide_index=True,
            )

        if report:
            st.subheader("สรุปและคำแนะนำ")
            st.write(report.overall_summary)
            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("**จุดแข็ง**\n" + "\n".join(f"- {s}" for s in report.strengths))
            with col_b:
                st.markdown("**แผนการทบทวน**\n" + "\n".join(f"{i}. {export.strip_numbering(s)}" for i, s in enumerate(report.study_plan, 1)))

            st.markdown("#### เรื่องที่ต้องเรียนเพิ่ม / ปรับปรุง")
            pct_by_concept = {m.concept: m.percent for m in mastery}
            for w in report.weak_topics:
                with st.container(border=True):
                    concept_pct = pct_by_concept.get(w.concept)
                    st.markdown(f"**{w.concept}**" + (f" — {concept_pct:.0f}%" if concept_pct is not None else ""))
                    st.write(w.why)
                    if w.review:
                        st.markdown("📖 **ควรทบทวน:** " + ", ".join(w.review))
                    st.markdown("\n".join(f"- {a}" for a in w.actions))

            if report.answer_writing_tips:
                st.markdown("#### คำแนะนำการเขียนตอบอัตนัย\n" + "\n".join(f"- {tip}" for tip in report.answer_writing_tips))

            st.markdown("#### ฝึกต่อ")
            practice_prompt = st.text_area(
                "คำสั่งสำหรับแบบฝึกชุดถัดไป (แก้ได้)", value=report.practice_prompt, key=f"practice_{state.version}"
            )
            if st.button("สร้างแบบฝึกเพิ่มเรื่องที่อ่อน", disabled=state.source is None or not ready):
                # ใช้กราฟเดียวกับแท็บ 1 แต่ให้ Request Planner วางสเปกเองโดยไม่ต้องรอยืนยัน
                result = run_with_progress(state.source, request=practice_prompt)
                if result:
                    set_exam(result, state.source)
                    state.notice = "สร้างแบบฝึกชุดใหม่แล้ว ไปทำที่แท็บ 2. ทำข้อสอบ"
                    st.rerun()
        else:
            st.warning("ยังไม่มีรายงานคำแนะนำ (การวิเคราะห์อาจล้มเหลว) ลองส่งตรวจใหม่อีกครั้ง")

        st.subheader("รายละเอียดรายข้อ")
        questions = {q.id: q for q in exam.questions}
        for r in results:
            q = questions[r.question_id]
            if r.points_awarded >= r.max_points:
                icon = "✅"
            elif r.points_awarded == 0:
                icon = "❌"
            else:
                icon = "🟡"
            with st.expander(f"{icon} ข้อ {r.question_id} · {r.points_awarded:g}/{r.max_points:g} คะแนน · {r.concept}"):
                st.markdown(q.question)
                st.markdown(f"**คำตอบของคุณ:** {r.student_answer}")
                if q.type in OBJECTIVE_TYPES:
                    st.markdown(f"**เฉลย:** {scoring.choice_label(q, q.correct_choice)}")
                    st.caption(q.explanation)
                else:
                    if r.criteria:
                        st.dataframe(
                            [{
                                "เกณฑ์": c.criterion,
                                "คะแนน": f"{c.points_awarded:g}/{c.max_points:g}",
                                "หลักฐานจากคำตอบ": c.evidence,
                                "ความเห็น": c.comment,
                            } for c in r.criteria],
                            hide_index=True,
                        )
                    if r.unverified_evidence:
                        st.warning(
                            "ผู้ตรวจอ้างข้อความที่ไม่พบในคำตอบจริง ควรให้ผู้สอนตรวจซ้ำ: "
                            + " / ".join(f"“{e}”" for e in r.unverified_evidence)
                        )
                    st.markdown(f"**Feedback:** {r.feedback}")
                    if r.missed_key_points:
                        st.markdown("**ประเด็นที่ขาด:**\n" + "\n".join(f"- {p}" for p in r.missed_key_points))
                    if r.misconceptions:
                        st.markdown("**ความเข้าใจที่คลาดเคลื่อน:**\n" + "\n".join(f"- {p}" for p in r.misconceptions))
                    st.markdown(f"**คำตอบตัวอย่าง:** {q.model_answer}")

        st.markdown("#### พิมพ์รายงานผล")
        student_name = st.text_input("ชื่อผู้สอบ (แสดงในรายงาน ไม่บังคับ)", key="student_name")
        if st.button("📄 สร้างรายงานผล (PDF)"):
            with st.spinner("กำลังสร้าง PDF..."):
                try:
                    state.files["report"] = export.report_pdf(exam, results, mastery, report, student_name)
                except export.ExportError as e:
                    st.error(str(e))
        if "report" in state.files:
            st.download_button(
                "⬇️ รายงานผลการสอบ (PDF)", state.files["report"],
                file_name="report.pdf", mime="application/pdf",
            )
        st.download_button(
            "ดาวน์โหลดผลการประเมิน (JSON)",
            json.dumps({
                "exam": exam.model_dump(),
                "results": [r.model_dump() for r in results],
                "mastery": [m.model_dump() for m in mastery],
                "report": report.model_dump() if report else None,
            }, ensure_ascii=False, indent=2),
            file_name="assessment_report.json",
            mime="application/json",
        )
