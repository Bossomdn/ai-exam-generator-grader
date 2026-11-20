"""แท็บ 2: ทำข้อสอบในแอป หรืออัปโหลดกระดาษคำตอบ (PDF / รูปถ่าย) ให้ AI ถอดคำตอบ → ตรวจทาน → ส่งตรวจ"""

import streamlit as st

from src import answer_reader, scoring
from src.advisor import advise
from src.grader import grade_exam
from src.llm import LLMError
from src.schemas import Exam

from .answer_form import answer_fields
from .common import state


def render(ready: bool) -> None:
    exam = state.exam
    if not exam:
        st.info("สร้างข้อสอบที่แท็บ 1 ก่อน")
        return
    v = state.version
    st.subheader(exam.title)
    upload_mode = st.radio(
        "วิธีส่งคำตอบ", ["ตอบในแอป", "อัปโหลดกระดาษคำตอบ (PDF / รูปถ่าย)"], horizontal=True, key="answer_mode"
    ) != "ตอบในแอป"
    sheet = state.sheet if state.sheet and state.sheet["version"] == v else None
    if upload_mode:
        sheet = upload_answer_sheet(exam, v, sheet, ready)

    if not upload_mode or sheet is not None:
        extracted_by_id = {a.question_id: a for a in sheet["result"].answers} if upload_mode else {}
        if render_answer_form(exam, v, upload_mode, extracted_by_id, ready):
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


def upload_answer_sheet(exam: Exam, v: int, sheet: dict | None, ready: bool) -> dict | None:
    """อัปโหลด → AI ถอดคำตอบแล้วเติมลงฟอร์ม → แสดงภาพไว้เทียบตอนตรวจทาน คืนข้อมูลกระดาษคำตอบที่อ่านแล้ว"""
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
    return sheet


def render_answer_form(exam: Exam, v: int, upload_mode: bool, extracted_by_id: dict, ready: bool) -> bool:
    """ฟอร์มตอบข้อสอบ (ใช้ตรวจทานคำตอบที่ถอดจากไฟล์ด้วย) คืน True เมื่อกดส่งตรวจ"""
    with st.form(f"exam_form_{v}"):
        answer_fields(exam, f"ans_{v}", extracted_by_id if upload_mode else None)
        return st.form_submit_button("ส่งคำตอบให้ตรวจ", type="primary", disabled=not ready)
