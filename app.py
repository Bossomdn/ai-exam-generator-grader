"""ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ (multi-agent)

รัน: streamlit run app.py

ไฟล์นี้เป็นแค่โครงหน้า: เมนูอยู่ใน ui/nav.py แต่ละหน้าเป็นไฟล์ใน views/ ซึ่งเรียกโมดูลใน ui/ (แก้หน้าไหนให้แก้ไฟล์นั้น)
"""

import streamlit as st

from src.llm import check_ready
from ui import common, nav, sidebar

st.set_page_config(page_icon="📝", layout="wide")

common.init_state()
common.keep_inputs()
common.state.llm_ready, ready_message = check_ready()
page = st.navigation(nav.pages())
sidebar.render(common.state.llm_ready, ready_message)

st.caption("📝 ผู้ช่วยครู: ออกข้อสอบ · ตรวจ · วิเคราะห์ผลสอบ — Multi-Agent System for Automated Assessment "
           "Generation and Evaluative Essay Grading")
st.title(page.title)
if common.state.notice:
    st.success(common.state.notice)
    common.state.notice = None
page.run()
