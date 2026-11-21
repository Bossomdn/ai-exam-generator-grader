"""แถบซ้าย (ใต้เมนู): สถานะโมเดล งานที่เปิดอยู่ และคำอธิบาย agent แบบพับเก็บ"""

import streamlit as st

from src import config

from .common import state


def render(ready: bool, ready_message: str) -> None:
    with st.sidebar:
        if ready:
            st.caption(f"🟢 {ready_message} · `{config.model_name()}`")
        else:
            st.error(ready_message)
        if state.exam:
            st.caption(f"📄 ข้อสอบที่ใช้อยู่: **{state.exam.title}**")
        if state.class_sitting:
            st.caption(f"🏫 การสอบที่เปิดอยู่: **{state.class_sitting.classroom.name}** "
                       f"ภาคเรียน {state.class_sitting.term}")
        with st.expander("ระบบนี้ทำงานอย่างไร"):
            st.markdown(
                "**Agent ฝั่งสร้างข้อสอบ**\n"
                "- Request Planner — แปลงคำสั่งเป็นสเปก\n"
                "- Question Generator — ออกข้อสอบ + เฉลย + เกณฑ์\n"
                f"- Quality Reviewer — ทำข้อสอบเองโดยไม่เห็นเฉลย หาจุดบกพร่อง แล้วส่งกลับแก้ "
                f"(สูงสุด {config.MAX_REVISIONS} รอบ)\n"
                "- Item Analyst — อ่านสถิติจากผลสอบจริงทั้งห้อง วินิจฉัยข้อที่มีปัญหา แล้วส่งให้ Generator แก้"
            )
            st.markdown(
                "**Agent ฝั่งตรวจเรียงความ**\n"
                "- ผู้ตรวจ 3 มุมมอง (เนื้อหา / เหตุผล / ยึดเกณฑ์) — ให้คะแนนพร้อมกันโดยไม่เห็นผลของกันและกัน\n"
                "- Moderator — ตัดสินเมื่อคะแนนต่างกันมาก ข้อที่ต่างกันมากเกินไปส่งให้ครูตรวจซ้ำ"
                + ("" if config.GRADER_MODE == "panel" else "\n\n(ปิดอยู่: `GRADER_MODE=single` ใช้ผู้ตรวจคนเดียว)")
            )
            st.caption(f"ผู้ให้บริการ: `{config.PROVIDER}` · เปลี่ยนได้ที่ `LLM_PROVIDER` ในไฟล์ `.env`")
        if st.button("เริ่มใหม่ทั้งหมด", help="ล้างข้อสอบและงานที่เปิดอยู่ในหน้านี้ (ข้อมูลในฐานข้อมูลยังอยู่)"):
            state.clear()
            st.rerun()
