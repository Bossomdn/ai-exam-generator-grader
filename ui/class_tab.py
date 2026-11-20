"""แท็บ 4 (ครู): ส่งข้อสอบเป็น Google Form → นำเข้าคำตอบทั้งห้อง (CSV) → ตรวจทั้งห้อง → ตารางคะแนน / บันทึกข้อมูลห้อง"""

import streamlit as st

from src import class_session, forms, scoring
from src.class_session import ClassFileError
from src.llm import LLMError
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, ClassSession, Exam, StudentRecord

from .common import state, use_exam

FORM_STEPS = """
1. เปิด https://script.google.com แล้วกด **New project** (โปรเจ็กต์ใหม่)
2. ลบโค้ดเดิมทิ้ง วางโค้ดทั้งหมดจากไฟล์ที่ดาวน์โหลด แล้วกด **Save**
3. เลือกฟังก์ชัน `createExamForm` แล้วกด **Run**
4. ครั้งแรก Google จะขอสิทธิ์: **Review permissions** → เลือกบัญชี → **Advanced** → **Go to ... (unsafe)** → **Allow**
   (มีคำเตือนเพราะเป็นสคริปต์ที่เราสร้างเอง)
5. ดูที่ **Execution log** จะมี "ลิงก์ส่งให้ผู้สอบ" ส่งลิงก์นั้นให้ผู้สอบทำ
6. เก็บคำตอบครบแล้ว: เปิดฟอร์ม → แท็บ **Responses** → **⋮** → **Download responses (.csv)**
   แล้วนำไฟล์มาใส่ในขั้นที่ 2 (ไฟล์ `.zip` ที่ได้ใส่ได้เลย ไม่ต้องแตกไฟล์)

- ฟอร์มเป็นแบบธรรมดา ผู้สอบไม่เห็นเฉลยหลังส่ง (ระบบนี้เป็นผู้ตรวจเอง)
- **ห้ามแก้ชื่อข้อ** ("ข้อ 1." ...) และช่อง "ชื่อ-นามสกุล" ในฟอร์ม เพราะใช้จับคู่คำตอบตอนนำเข้า
"""


def render(ready: bool) -> None:
    st.caption("เก็บคำตอบของผู้สอบทั้งห้องผ่าน Google Forms หรือไฟล์ CSV แล้วให้ระบบตรวจทีละคน")
    if state.class_session is None:
        start_session()
        return
    show_header(state.class_session)
    st.markdown("#### 1. ส่งข้อสอบให้ผู้สอบทำผ่าน Google Forms")
    google_form_section(state.class_session.exam)
    st.markdown("#### 2. นำเข้าคำตอบ")
    import_section(state.class_session)
    st.markdown("#### 3. ตรวจและดูคะแนน")
    grade_section(ready)
    st.markdown("#### บันทึกข้อมูลห้อง")
    save_section(state.class_session)


def start_session() -> None:
    exam = state.exam
    if exam:
        if st.button(f"เริ่มเก็บคำตอบทั้งห้องด้วยข้อสอบ “{exam.title}”", type="primary"):
            state.class_session = class_session.new_session(exam)
            state.class_import = None
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
        state.class_session = loaded
        state.class_import = None
        if state.exam is None:  # ให้แท็บอื่นเห็นข้อสอบชุดนี้ด้วย (ดูเฉลย / พิมพ์ PDF)
            use_exam(loaded.exam)
        st.rerun()


def show_header(session: ClassSession) -> None:
    exam = session.exam
    graded = len(session.students) - len(class_session.ungraded(session))
    st.markdown(
        f"**ข้อสอบ:** {exam.title} · {len(exam.questions)} ข้อ · "
        f"ผู้สอบ {len(session.students)} คน (ตรวจแล้ว {graded} คน)"
    )
    if state.exam and state.exam != exam:
        st.info("ข้อมูลห้องนี้เป็นของข้อสอบชุดก่อน ไม่ใช่ชุดล่าสุดในแท็บ 1")
        if st.button("เริ่มห้องใหม่ด้วยข้อสอบชุดล่าสุด (ข้อมูลห้องนี้จะถูกล้าง — บันทึกไฟล์ด้านล่างก่อน)"):
            state.class_session = class_session.new_session(state.exam)
            state.class_import = None
            st.rerun()


def google_form_section(exam: Exam) -> None:
    st.download_button(
        "⬇️ ดาวน์โหลดสคริปต์สร้าง Google Form (.gs)", forms.apps_script(exam),
        file_name="create_exam_form.gs", mime="text/plain",
    )
    with st.expander("วิธีสร้าง Google Form จากสคริปต์"):
        st.markdown(FORM_STEPS)


def import_section(session: ClassSession) -> None:
    uploaded = st.file_uploader(
        "ไฟล์คำตอบ (.csv หรือ .zip ที่ดาวน์โหลดจาก Google Forms)", type=["csv", "zip"],
        key=f"class_csv_{state.class_upload_n}",
    )
    st.download_button(
        "⬇️ เทมเพลต CSV สำหรับกรอกคำตอบเอง", forms.template_csv(session.exam),
        file_name="answers_template.csv", mime="text/csv",
    )
    st.caption("กรอกเทมเพลต: ปรนัยใส่ ก ข ค ง หรือข้อความตัวเลือก · ถูก/ผิด ใส่ “ถูก” หรือ “ผิด” · อัตนัยใส่ข้อความคำตอบ")
    if st.button("นำเข้าคำตอบ", type="primary", disabled=uploaded is None):
        try:
            result = forms.parse_responses(session.exam, uploaded.getvalue(), uploaded.name)
        except forms.FormsError as e:
            st.error(str(e))
            return
        merged, added, updated = class_session.merge_students(session, result.students)
        state.class_session = merged
        state.class_import = {
            "source": "Google Forms" if result.source == "google_forms" else "CSV",
            "added": added, "updated": updated,
            "matched": len(result.matched_questions), "total": len(session.exam.questions),
            "warnings": result.warnings + [f"{s.name} — {n}" for s in result.students for n in s.import_notes],
        }
        state.class_upload_n += 1  # ล้างช่องอัปโหลด กันกดนำเข้าซ้ำแล้วผลตรวจหาย
        st.rerun()

    report = state.class_import
    if report:
        st.success(
            f"นำเข้าจาก {report['source']}: เพิ่ม {report['added']} คน · อัปเดต {report['updated']} คน · "
            f"จับคู่คอลัมน์ได้ {report['matched']}/{report['total']} ข้อ"
        )
        if report["warnings"]:
            with st.expander(f"⚠️ คำเตือนจากการนำเข้า ({len(report['warnings'])})"):
                st.markdown("\n".join(f"- {w}" for w in report["warnings"]))


def grade_section(ready: bool) -> None:
    session = state.class_session
    if not session.students:
        st.info("ยังไม่มีคำตอบ นำเข้าไฟล์ในขั้นที่ 2 ก่อน")
        return
    pending = class_session.ungraded(session)
    needs_llm = class_session.needs_llm(session.exam)
    if pending:
        if st.button(f"ตรวจคำตอบที่ยังไม่ได้ตรวจ ({len(pending)} คน)", type="primary", disabled=needs_llm and not ready):
            grade_all(pending)
        st.caption(
            "ข้ออัตนัยใช้โมเดลตรวจทีละข้อ (Ollama ประมาณ 10 วินาทีต่อข้อต่อคน)" if needs_llm
            else "ข้อสอบปรนัยล้วน ตรวจด้วยโค้ดทันที ไม่ต้องใช้โมเดล"
        )

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


def grade_all(pending: list[StudentRecord]) -> None:
    """ตรวจทีละคน เก็บผลทันทีหลังตรวจแต่ละคน ถ้าล้มกลางทาง คนที่ตรวจแล้วไม่หาย"""
    bar = st.progress(0.0)
    for i, student in enumerate(pending):
        bar.progress(i / len(pending), text=f"กำลังตรวจ {student.name} ({i + 1}/{len(pending)})")
        try:
            graded = class_session.grade_student(state.class_session.exam, student)
        except LLMError as e:
            st.error(f"ตรวจของ {student.name} ไม่สำเร็จ: {e} — คนที่ตรวจแล้วเก็บไว้แล้ว กดตรวจต่อได้")
            return
        state.class_session = class_session.replace_student(state.class_session, graded)
    bar.progress(1.0, text=f"ตรวจครบ {len(pending)} คนแล้ว")


def show_student(exam: Exam, student: StudentRecord) -> None:
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


def save_section(session: ClassSession) -> None:
    st.download_button(
        "💾 บันทึกข้อมูลห้อง (.json)", class_session.to_json(session),
        file_name="class_data.json", mime="application/json",
    )
    st.caption(
        "ไฟล์นี้มีชื่อและคำตอบของผู้สอบ เปิดกลับมาทำต่อได้ภายหลัง · เก็บไว้ในเครื่อง ห้าม commit ขึ้น GitHub "
        "(ถ้าเก็บในโปรเจกต์ ให้ไว้ในโฟลเดอร์ data/ ซึ่ง git ไม่สนใจ)"
    )
    with st.expander("เปิดไฟล์ข้อมูลห้องอื่น / ปิดห้องนี้"):
        open_class_file()
        if st.button("ปิดห้องนี้ (ล้างข้อมูลห้องออกจากแอป)"):
            state.class_session = None
            state.class_import = None
            st.rerun()
