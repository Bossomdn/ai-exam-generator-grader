"""แถบซ้าย: สถานะโมเดล ขั้นตอนการใช้งาน และรายชื่อ agent"""

import streamlit as st

from src import config

from .common import state


def render(ready: bool, ready_message: str) -> None:
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
            "3. ดูคะแนนและเรื่องที่ต้องเรียนเพิ่ม\n"
            "4. (ครู) เลือกห้อง → สอบบนกระดาษ → อัปโหลดกระดาษคำตอบทั้งห้อง → ตรวจทาน → ตรวจทั้งห้อง\n"
            "5. (ครู) เพิ่มห้องและรายชื่อนักเรียน · ดูคะแนนรายห้อง รายคน รายชั้น · สำรองข้อมูล"
        )
        st.markdown(
            "**Agent ฝั่งสร้างข้อสอบ**\n"
            "- Request Planner — แปลงคำสั่งเป็นสเปก\n"
            "- Question Generator — ออกข้อสอบ + เฉลย + เกณฑ์\n"
            f"- Quality Reviewer — ทำข้อสอบเองโดยไม่เห็นเฉลย หาจุดบกพร่อง แล้วส่งกลับแก้ (สูงสุด {config.MAX_REVISIONS} รอบ)\n"
            "- Item Analyst — อ่านสถิติจากผลสอบจริงทั้งห้อง วินิจฉัยข้อที่มีปัญหา แล้วส่งให้ Generator แก้"
        )
        st.markdown(
            "**Agent ฝั่งตรวจเรียงความ**\n"
            "- ผู้ตรวจ 3 มุมมอง (เนื้อหา / เหตุผล / ยึดเกณฑ์) — ให้คะแนนพร้อมกันโดยไม่เห็นผลของกันและกัน\n"
            "- Moderator — ตัดสินเมื่อคะแนนต่างกันมาก ข้อที่ต่างกันมากเกินไปส่งให้ครูตรวจซ้ำ"
            + ("" if config.GRADER_MODE == "panel" else "\n\n(ปิดอยู่: `GRADER_MODE=single` ใช้ผู้ตรวจคนเดียว)")
        )
        if st.button("เริ่มใหม่ทั้งหมด"):
            state.clear()
            st.rerun()
