"""ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ (multi-agent)

รัน: streamlit run app.py

ไฟล์นี้เป็นแค่โครงหน้า เนื้อหาแต่ละแท็บอยู่ในโฟลเดอร์ ui/ (แก้แท็บไหนให้แก้ไฟล์นั้น)
"""

import streamlit as st

from src.llm import check_ready
from ui import class_tab, common, create_tab, result_tab, school_tab, sidebar, take_tab

st.set_page_config(page_title="AI Exam Generator & Grader", page_icon="📝", layout="wide")

common.init_state()
ready, ready_message = check_ready()
sidebar.render(ready, ready_message)

st.title("📝 ระบบสร้างข้อสอบและตรวจประเมินอัตโนมัติ")
st.caption("Multi-Agent System for Automated Assessment Generation and Evaluative Essay Grading")

if common.state.notice:
    st.success(common.state.notice)
    common.state.notice = None

tab_create, tab_take, tab_result, tab_class, tab_school = st.tabs(
    ["1. สร้างข้อสอบ", "2. ทำข้อสอบ", "3. ผลและคำแนะนำ", "4. ทั้งห้อง (ครู)", "5. ห้องเรียนและคะแนน (ครู)"]
)
with tab_create:
    create_tab.render(ready)
with tab_take:
    take_tab.render(ready)
with tab_result:
    result_tab.render(ready)
with tab_class:
    class_tab.render(ready)
with tab_school:
    school_tab.render()
