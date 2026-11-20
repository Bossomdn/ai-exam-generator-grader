"""ช่องตอบข้อสอบที่ใช้ร่วมกัน: แท็บ 2 (ทำข้อสอบ / ตรวจทานกระดาษ 1 คน) และแท็บ 4 (ตรวจทานกระดาษทั้งห้อง)

ค่าของแต่ละข้อเก็บใน st.session_state ที่ key "<key_prefix>_<question_id>"
ปรนัย = index ตัวเลือก (None = ไม่ได้ตอบ), อัตนัย = ข้อความ
"""

from typing import Optional, Union

import streamlit as st

from src import answer_reader, scoring
from src.schemas import OBJECTIVE_TYPES, TYPE_LABELS, Exam, ExtractedAnswer

from .common import state


def prefill(exam: Exam, key_prefix: str, answers: dict[int, Union[int, str, None]]) -> None:
    """ใส่คำตอบตั้งต้นลงฟอร์ม (ไม่ทับค่าที่ผู้ใช้แก้ไปแล้ว)"""
    for q in exam.questions:
        value = answers.get(q.id)
        state.setdefault(f"{key_prefix}_{q.id}", value if q.type in OBJECTIVE_TYPES else (value or ""))


def collect(exam: Exam, key_prefix: str) -> dict[int, Union[int, str, None]]:
    return {q.id: state.get(f"{key_prefix}_{q.id}") for q in exam.questions}


def answer_fields(
    exam: Exam, key_prefix: str, extracted_by_id: Optional[dict[int, ExtractedAnswer]] = None
) -> None:
    """ช่องตอบทุกข้อ (เรียกภายใน st.form) ถ้าส่ง extracted_by_id มา จะแสดงคำเตือนจากการที่ AI อ่านกระดาษรายข้อ"""
    for q in exam.questions:
        st.markdown(f"**ข้อ {q.id}.** {q.question}")
        st.caption(f"{TYPE_LABELS[q.type]} · {q.points:g} คะแนน")
        key = f"{key_prefix}_{q.id}"
        if extracted_by_id is not None and (warning := answer_reader.review_warning(q, extracted_by_id.get(q.id))):
            st.warning(f"⚠️ {warning}")
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
