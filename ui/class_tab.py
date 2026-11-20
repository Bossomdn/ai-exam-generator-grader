"""แท็บ 4 (ครู): พิมพ์ข้อสอบ → สอบบนกระดาษ → อัปโหลดกระดาษคำตอบทีละหลายใบ (หรือ CSV) → AI อ่านคำตอบ
→ ครูตรวจทาน → ตรวจทั้งห้อง → ตารางคะแนน / บันทึกข้อมูลห้อง"""

import streamlit as st

from src import answer_reader, class_session, csv_import, export, scoring
from src.class_session import ClassFileError
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, ClassSession, Exam, StudentRecord

from .answer_form import answer_fields, collect, prefill
from .common import state, use_exam

SHEETS = "สแกนกระดาษคำตอบ (PDF / รูปถ่าย)"
CSV = "ไฟล์ CSV (กรอกคำตอบเอง)"


def render(ready: bool) -> None:
    st.caption("สอบบนกระดาษ แล้วสแกนหรือถ่ายรูปกระดาษคำตอบอัปโหลดทีละหลายใบ ให้ AI อ่านคำตอบ ครูตรวจทาน แล้วตรวจทั้งห้อง")
    if state.class_session is None:
        start_session()
        return
    show_header(state.class_session)
    st.markdown("#### 1. พิมพ์ข้อสอบไปสอบ")
    print_section(state.class_session.exam)
    st.markdown("#### 2. นำเข้าคำตอบ")
    import_section(ready)
    if class_session.awaiting_review(state.class_session):
        st.markdown("#### 3. ตรวจทานคำตอบที่ AI อ่าน")
        review_section()
    st.markdown("#### 4. ตรวจและดูคะแนน")
    grade_section(ready)
    st.markdown("#### บันทึกข้อมูลห้อง")
    save_section(state.class_session)


def reset_class(session: ClassSession | None) -> None:
    state.class_session = session
    state.class_import = None
    state.class_images = {}
    state.class_exam_pdf = None
    state.class_review_n += 1


def start_session() -> None:
    exam = state.exam
    if exam:
        if st.button(f"เริ่มเก็บคำตอบทั้งห้องด้วยข้อสอบ “{exam.title}”", type="primary"):
            reset_class(class_session.new_session(exam))
            st.rerun()
    else:
        st.info("สร้างข้อสอบที่แท็บ 1 ก่อน หรือเปิดไฟล์ข้อมูลห้องที่บันทึกไว้")
    open_class_file()


def open_class_file() -> None:
    uploaded = st.file_uploader("เปิดไฟล์ข้อมูลห้องที่บันทึกไว้ (.json)", type=["json"], key="class_file")
    if uploaded and st.button("เปิดไฟล์ข้อมูลห้อง"):
        try:
            loaded = class_session.from_json(uploaded.getvalue())
        except ClassFileError as e:
            st.error(str(e))
            return
        reset_class(loaded)
        if state.exam is None:  # ให้แท็บอื่นเห็นข้อสอบชุดนี้ด้วย (ดูเฉลย)
            use_exam(loaded.exam)
        st.rerun()


def show_header(session: ClassSession) -> None:
    exam = session.exam
    graded = sum(1 for s in session.students if s.results is not None)
    waiting = len(class_session.awaiting_review(session))
    st.markdown(
        f"**ข้อสอบ:** {exam.title} · {len(exam.questions)} ข้อ · ผู้สอบ {len(session.students)} คน "
        f"(รอตรวจทาน {waiting} · ตรวจแล้ว {graded})"
    )
    if state.exam and state.exam != exam:
        st.info("ข้อมูลห้องนี้เป็นของข้อสอบชุดก่อน ไม่ใช่ชุดล่าสุดในแท็บ 1")
        if st.button("เริ่มห้องใหม่ด้วยข้อสอบชุดล่าสุด (ข้อมูลห้องนี้จะถูกล้าง — บันทึกไฟล์ด้านล่างก่อน)"):
            reset_class(class_session.new_session(state.exam))
            st.rerun()


def print_section(exam: Exam) -> None:
    st.caption("ให้ผู้สอบเขียนชื่อ เลขที่ และคำตอบลงในข้อสอบ (ปรนัยให้วงกลมตัวเลือก) AI จะอ่านชื่อและเลขที่จากกระดาษด้วย")
    if st.button("📄 สร้าง PDF ข้อสอบ (ไม่มีเฉลย)"):
        with st.spinner("กำลังสร้าง PDF..."):
            try:
                state.class_exam_pdf = export.exam_pdf(exam)
            except export.ExportError as e:
                st.error(str(e))
    if state.class_exam_pdf:
        st.download_button("⬇️ ข้อสอบสำหรับพิมพ์", state.class_exam_pdf, file_name="exam.pdf", mime="application/pdf")


# ---------------- นำเข้าคำตอบ ----------------

def import_section(ready: bool) -> None:
    method = st.radio("นำเข้าจาก", [SHEETS, CSV], horizontal=True, key="class_import_method")
    if method == SHEETS:
        sheet_import(ready)
    else:
        csv_upload()
    report = state.class_import
    if report:
        st.success(f"นำเข้าจาก{report['source']}: เพิ่ม {report['added']} คน · อัปเดต {report['updated']} คน" + report["extra"])
        if report["warnings"]:
            with st.expander(f"⚠️ คำเตือนจากการนำเข้า ({len(report['warnings'])})"):
                st.markdown("\n".join(f"- {w}" for w in report["warnings"]))


def sheet_import(ready: bool) -> None:
    files = st.file_uploader(
        "กระดาษคำตอบ เลือกได้หลายไฟล์", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True,
        key=f"class_sheets_{state.class_upload_n}",
    )
    combined = st.radio(
        "รูปแบบไฟล์", ["1 ไฟล์ = ผู้สอบ 1 คน", "PDF ไฟล์เดียวรวมทุกคน (สแกนทั้งปึก)"], horizontal=True,
        key="class_sheet_layout",
    ) != "1 ไฟล์ = ผู้สอบ 1 คน"
    pages_per = None
    if combined:
        pages_per = int(st.number_input(
            "กระดาษคำตอบคนละกี่หน้า", min_value=1, max_value=answer_reader.MAX_PAGES, value=1, key="class_pages_per"
        ))
    st.caption("AI อ่านประมาณ 5–15 วินาทีต่อคน (Ollama · คนแรกช้ากว่าเพราะต้องโหลดโมเดล) "
               "ทุกคนต้องผ่านการตรวจทานในขั้นที่ 3 ก่อนตรวจคะแนน")
    if st.button("อ่านคำตอบด้วย AI", type="primary", disabled=not files or not ready):
        read_sheets(files, pages_per)


def read_sheets(files, pages_per: int | None) -> None:
    exam = state.class_session.exam
    sheets, problems = [], []
    for f in files:
        try:
            if pages_per:
                if not f.name.lower().endswith(".pdf"):
                    raise answer_reader.SheetError("แบบรวมทั้งปึกใช้ได้กับไฟล์ PDF เท่านั้น")
                sheets += answer_reader.split_pdf(f.name, f.getvalue(), pages_per)
            else:
                sheets.append(answer_reader.load_answer_sheet(f.name, f.getvalue()))
        except answer_reader.SheetError as e:
            problems.append(f"{f.name}: {e}")

    students = []
    bar = st.progress(0.0)
    for i, sheet in enumerate(sheets):
        bar.progress(i / len(sheets), text=f"AI กำลังอ่าน {sheet.name} ({i + 1}/{len(sheets)})")
        try:
            students.append(class_session.read_sheet(exam, sheet))
        except LLMError as e:
            problems.append(f"{sheet.name}: {e} — หยุดอ่านใบที่เหลือ อ่านใหม่ได้ภายหลัง")
            break
        state.class_images[sheet.name] = sheet.images
    students = class_session.dedupe(students)
    merged, added, updated = class_session.merge_students(state.class_session, students)
    flagged = sum(1 for s in students if s.import_notes)
    state.class_session = merged
    state.class_import = {
        "source": "กระดาษคำตอบ", "added": added, "updated": updated,
        "extra": f" · มีคำเตือน {flagged} คน (ดูในขั้นที่ 3)" if flagged else "",
        "warnings": problems,
    }
    state.class_upload_n += 1  # ล้างช่องอัปโหลด กันกดอ่านซ้ำ
    state.class_review_n += 1
    st.rerun()


def csv_upload() -> None:
    exam = state.class_session.exam
    uploaded = st.file_uploader("ไฟล์คำตอบ (.csv)", type=["csv"], key=f"class_csv_{state.class_upload_n}")
    st.download_button(
        "⬇️ เทมเพลต CSV", csv_import.template_csv(exam), file_name="answers_template.csv", mime="text/csv",
    )
    st.caption("กรอกเทมเพลต: ปรนัยใส่ ก ข ค ง หรือข้อความตัวเลือก · ถูก/ผิด ใส่ “ถูก” หรือ “ผิด” · อัตนัยใส่ข้อความคำตอบ")
    if st.button("นำเข้าคำตอบ", type="primary", disabled=uploaded is None):
        try:
            result = csv_import.parse_responses(exam, uploaded.getvalue())
        except csv_import.CsvImportError as e:
            st.error(str(e))
            return
        merged, added, updated = class_session.merge_students(state.class_session, result.students)
        state.class_session = merged
        state.class_import = {
            "source": " CSV", "added": added, "updated": updated,
            "extra": f" · จับคู่คอลัมน์ได้ {len(result.matched_questions)}/{len(exam.questions)} ข้อ",
            "warnings": result.warnings + [f"{s.name} — {n}" for s in result.students for n in s.import_notes],
        }
        state.class_upload_n += 1  # ล้างช่องอัปโหลด กันกดนำเข้าซ้ำแล้วผลตรวจหาย
        state.class_review_n += 1
        st.rerun()


# ---------------- ตรวจทาน ----------------

def review_section() -> None:
    session = state.class_session
    pending = class_session.awaiting_review(session)
    clean = [i for i in pending if not session.students[i].import_notes]
    st.caption(
        f"รอตรวจทาน {len(pending)} คน (มีคำเตือน {len(pending) - len(clean)} คน) — "
        "เทียบคำตอบกับภาพกระดาษ แก้ส่วนที่ AI อ่านผิด แล้วกดยืนยัน"
    )
    if clean:
        if st.button(f"ยืนยันทุกคนที่ไม่มีคำเตือน ({len(clean)} คน)"):
            for i in clean:
                session = class_session.update_student(
                    session, i, session.students[i].model_copy(update={"reviewed": True}))
            state.class_session = session
            state.class_review_n += 1
            st.rerun()
        st.caption("AI อาจอ่านผิดโดยไม่มีคำเตือนได้ (เช่น วงตัวเลือกไม่ชัด) ควรสุ่มเปิดดูบางคนก่อนยืนยันรวด")

    def label(i: int) -> str:
        s = session.students[i]
        mark = f"⚠️ {len(s.import_notes)} คำเตือน" if s.import_notes else "✓ ไม่มีคำเตือน"
        return f"{s.student_no} {s.name}".strip() + f" — {mark}"

    index = st.selectbox("เลือกผู้สอบที่จะตรวจทาน", pending, format_func=label, key=f"review_pick_{state.class_review_n}")
    if index is not None:
        review_student(index)


def review_student(index: int) -> None:
    session = state.class_session
    exam, student = session.exam, session.students[index]
    prefix = f"rev_{state.class_review_n}_{index}"
    state.setdefault(f"{prefix}_name", student.name)
    state.setdefault(f"{prefix}_no", student.student_no)
    prefill(exam, prefix, student.answers)

    col_image, col_form = st.columns(2)
    with col_image:
        images = state.class_images.get(student.sheet_name)
        if images:
            for page, image in enumerate(images, start=1):
                st.image(image, caption=f"{student.sheet_name} · หน้า {page}")
        else:
            st.info("ไม่มีภาพกระดาษ (ภาพไม่ได้บันทึกในไฟล์ข้อมูลห้อง) — อัปโหลดกระดาษใบนี้ใหม่ถ้าต้องการเทียบ")
    with col_form:
        for note in student.import_notes:
            if not note.startswith("ข้อ "):  # คำเตือนรายข้อแสดงในฟอร์มอยู่แล้ว
                st.warning(f"⚠️ {note}")
        extracted = {a.question_id: a for a in student.extracted.answers} if student.extracted else None
        with st.form(f"{prefix}_form"):
            st.text_input("ชื่อ-นามสกุล", key=f"{prefix}_name")
            st.text_input("เลขที่", key=f"{prefix}_no")
            answer_fields(exam, prefix, extracted)
            if st.form_submit_button("ยืนยันคำตอบของคนนี้", type="primary"):
                confirmed = student.model_copy(update={
                    "name": state[f"{prefix}_name"].strip() or student.name,
                    "student_no": state[f"{prefix}_no"].strip(),
                    "answers": collect(exam, prefix),
                    "reviewed": True,
                    "results": None,
                })
                state.class_session = class_session.update_student(session, index, confirmed)
                state.class_review_n += 1
                st.rerun()


# ---------------- ตรวจและดูคะแนน ----------------

def grade_section(ready: bool) -> None:
    session = state.class_session
    if not session.students:
        st.info("ยังไม่มีคำตอบ นำเข้าในขั้นที่ 2 ก่อน")
        return
    pending = class_session.ready_to_grade(session)
    waiting = class_session.awaiting_review(session)
    needs_llm = class_session.needs_llm(session.exam)
    if pending:
        if st.button(f"ตรวจคำตอบที่ยังไม่ได้ตรวจ ({len(pending)} คน)", type="primary", disabled=needs_llm and not ready):
            grade_all(pending)
        st.caption(
            "ข้ออัตนัยใช้โมเดลตรวจทีละข้อ (Ollama ประมาณ 2–10 วินาทีต่อข้อต่อคน)" if needs_llm
            else "ข้อสอบปรนัยล้วน ตรวจด้วยโค้ดทันที ไม่ต้องใช้โมเดล"
        )
    if waiting:
        st.caption(f"ยังมี {len(waiting)} คนรอตรวจทาน — ต้องตรวจทานในขั้นที่ 3 ก่อนจึงจะตรวจคะแนนได้")

    session = state.class_session
    rows = class_session.score_rows(session)
    st.dataframe(rows, hide_index=True)
    percents = [r["ร้อยละ"] for r in rows if r["ร้อยละ"] is not None]
    if percents:
        st.caption(
            f"ตรวจแล้ว {len(percents)} คน · เฉลี่ย {sum(percents) / len(percents):.1f}% · "
            f"สูงสุด {max(percents):g}% · ต่ำสุด {min(percents):g}%"
        )
        st.download_button(
            "⬇️ คะแนนรายคน (CSV สำหรับลงสมุดคะแนน)", class_session.scores_csv(session),
            file_name="scores.csv", mime="text/csv",
        )
    choice = st.selectbox(
        "ดูรายละเอียดรายคน", range(len(session.students)), index=None, placeholder="เลือกผู้สอบ",
        format_func=lambda i: f"{session.students[i].student_no} {session.students[i].name}".strip(),
    )
    if choice is not None:
        show_student(session.exam, session.students[choice])


def grade_all(indices: list[int]) -> None:
    """ตรวจทีละคน เก็บผลทันทีหลังตรวจแต่ละคน ถ้าล้มกลางทาง คนที่ตรวจแล้วไม่หาย"""
    bar = st.progress(0.0)
    for n, i in enumerate(indices):
        student = state.class_session.students[i]
        bar.progress(n / len(indices), text=f"กำลังตรวจ {student.name} ({n + 1}/{len(indices)})")
        try:
            graded = class_session.grade_student(state.class_session.exam, student)
        except LLMError as e:
            st.error(f"ตรวจของ {student.name} ไม่สำเร็จ: {e} — คนที่ตรวจแล้วเก็บไว้แล้ว กดตรวจต่อได้")
            return
        state.class_session = class_session.update_student(state.class_session, i, graded)
    bar.progress(1.0, text=f"ตรวจครบ {len(indices)} คนแล้ว")


def show_student(exam: Exam, student: StudentRecord) -> None:
    st.caption(f"สถานะ: {class_session.status(student)} · ที่มา: "
               + ("กระดาษคำตอบ " + student.sheet_name if student.source == "answer_sheet" else "CSV"))
    for note in student.import_notes:
        st.warning(note)
    results = {r.question_id: r for r in student.results or []}
    rows = []
    for q in exam.questions:
        answer = student.answers.get(q.id)
        if q.type in OBJECTIVE_TYPES:
            shown = scoring.choice_label(q, answer if isinstance(answer, int) else None)
        else:
            shown = str(answer or "").strip() or "(ไม่ได้ตอบ)"
        r = results.get(q.id)
        rows.append({
            "ข้อ": q.id,
            "ชนิด": TYPE_LABELS[q.type],
            "คำตอบ": shown,
            "คะแนน": f"{r.points_awarded:g}/{r.max_points:g}" if r else f"–/{q.points:g}",
        })
    st.dataframe(rows, hide_index=True)


# ---------------- บันทึก ----------------

def save_section(session: ClassSession) -> None:
    st.download_button(
        "💾 บันทึกข้อมูลห้อง (.json)", class_session.to_json(session),
        file_name="class_data.json", mime="application/json",
    )
    st.caption(
        "ไฟล์นี้มีชื่อและคำตอบของผู้สอบ (ไม่รวมภาพกระดาษ) เปิดกลับมาทำต่อได้ภายหลัง · เก็บไว้ในเครื่อง ห้าม commit ขึ้น GitHub "
        "(ถ้าเก็บในโปรเจกต์ ให้ไว้ในโฟลเดอร์ data/ ซึ่ง git ไม่สนใจ)"
    )
    with st.expander("เปิดไฟล์ข้อมูลห้องอื่น / ปิดห้องนี้"):
        open_class_file()
        if st.button("ปิดห้องนี้ (ล้างข้อมูลห้องออกจากแอป)"):
            reset_class(None)
            st.rerun()
