"""ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ (multi-agent)

รัน: streamlit run app.py

ไฟล์นี้เป็นแค่โครงหน้า เนื้อหาแต่ละแท็บอยู่ในโฟลเดอร์ ui/ (แก้แท็บไหนให้แก้ไฟล์นั้น)
"""

import streamlit as st

from src.llm import check_ready
from ui import common, create_tab, result_tab, sidebar, take_tab

st.set_page_config(page_title="AI Exam Generator & Grader", page_icon="📝", layout="wide")

common.init_state()
ready, ready_message = check_ready()
sidebar.render(ready, ready_message)

st.title("📝 ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ")
st.caption("Multi-Agent System for Automated Assessment Generation and Evaluative Essay Grading")

if common.state.notice:
    st.success(common.state.notice)
    common.state.notice = None

tab_create, tab_take, tab_result = st.tabs(["1. สร้างข้อสอบ", "2. ทำข้อสอบ", "3. ผลและคำแนะนำ"])
with tab_create:
    create_tab.render(ready)
with tab_take:
    take_tab.render(ready)
with tab_result:
    result_tab.render(ready)
