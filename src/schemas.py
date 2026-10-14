"""โครงสร้างข้อมูลทั้งหมดของระบบ (ใช้เป็น structured output ของ Claude ด้วย)

ใช้ Question แบบเดียวที่มีฟิลด์ `type` แทนการแยกหลาย class
เพื่อให้ JSON schema เรียบง่ายและ structured output เสถียร
ทุกฟิลด์เป็น required (ฟิลด์ที่ไม่เกี่ยวกับชนิดข้อนั้นให้เป็น list ว่าง / null)
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

QuestionType = Literal["mcq", "true_false", "short_answer", "essay"]
OBJECTIVE_TYPES = ("mcq", "true_false")

TYPE_LABELS = {
    "mcq": "ปรนัย (เลือกตอบ)",
    "true_false": "ปรนัย (ถูก/ผิด)",
    "short_answer": "อัตนัย (ตอบสั้น)",
    "essay": "อัตนัย (เรียงความ)",
}


# ---------- ข้อสอบ (ผลลัพธ์จาก generator) ----------

class RubricCriterion(BaseModel):
    name: str = Field(description="ชื่อเกณฑ์ เช่น 'ความถูกต้องของเนื้อหา'")
    description: str = Field(description="คำตอบแบบไหนได้เต็ม / ได้บางส่วน / ไม่ได้คะแนน")
    max_points: float


class Question(BaseModel):
    id: int
    type: QuestionType
    concept: str = Field(description="หัวข้อ/แนวคิดที่ข้อนี้วัด ต้องตรงกับชื่อใน Exam.concepts")
    source_ref: str = Field(description="ที่มาในเนื้อหา เช่น 'สไลด์ 3' หรือ 'ความรู้ทั่วไป'")
    bloom_level: Literal["remember", "understand", "apply", "analyze", "evaluate", "create"]
    difficulty: Literal["easy", "medium", "hard"]
    points: float
    question: str
    choices: list[str] = Field(description="mcq: 4 ตัวเลือก, true_false: ['ถูก','ผิด'], อัตนัย: []")
    correct_choice: Optional[int] = Field(description="index ของตัวเลือกที่ถูก (เริ่มที่ 0) สำหรับปรนัย, อัตนัยเป็น null")
    explanation: str = Field(description="ทำไมคำตอบนี้ถูก และ (สำหรับ mcq) ทำไมตัวเลือกอื่นผิด")
    model_answer: str = Field(description="คำตอบตัวอย่างสำหรับอัตนัย, ปรนัยเป็น ''")
    key_points: list[str] = Field(description="ประเด็นที่คำตอบต้องมี (short_answer), อื่น ๆ เป็น []")
    rubric: list[RubricCriterion] = Field(description="เกณฑ์ให้คะแนน (essay), อื่น ๆ เป็น []")


class Exam(BaseModel):
    title: str
    spec_summary: str = Field(description="สรุปว่าเข้าใจคำสั่งอย่างไร: ชนิด จำนวน ความยาก คะแนน และค่าที่ใช้ default")
    concepts: list[str]
    questions: list[Question]


# ---------- ผลการตรวจอัตนัย (ผลลัพธ์จาก grader) ----------

class CriterionScore(BaseModel):
    criterion: str
    points_awarded: float
    max_points: float
    evidence: str = Field(description="ข้อความที่คัดลอกตรงตัวจากคำตอบนักเรียน หรือ '' ถ้าไม่มี")
    comment: str


class SubjectiveGrade(BaseModel):
    criteria: list[CriterionScore]
    missed_key_points: list[str]
    misconceptions: list[str]
    feedback: str


# ---------- คำตอบที่ถอดจากกระดาษคำตอบ (ผลลัพธ์จาก answer reader) ----------

class ExtractedAnswer(BaseModel):
    question_id: int
    answer: str = Field(
        description="ปรนัย: ตัวอักษรที่เลือก (ก/ข/ค/ง) หรือ 'ถูก'/'ผิด'; อัตนัย: ข้อความตามที่เขียนจริง; ไม่ได้ตอบ: ''"
    )
    choice_text: str = Field(description="เลือกตอบ (mcq): ข้อความของตัวเลือกที่ถูกทำเครื่องหมาย; ชนิดอื่น: ''")
    confidence: Literal["high", "medium", "low"]
    note: str = Field(description="ข้อสังเกต เช่น ขีดฆ่าแก้คำตอบ หรือส่วนที่อ่านไม่ออก")


class ExtractedAnswerSheet(BaseModel):
    student_name: str
    answers: list[ExtractedAnswer]


# ---------- ผลรายข้อ (โค้ดสร้าง ไม่ใช่ LLM) ----------

class QuestionResult(BaseModel):
    question_id: int
    type: QuestionType
    concept: str
    source_ref: str
    points_awarded: float
    max_points: float
    is_correct: Optional[bool] = None  # เฉพาะปรนัย
    student_answer: str
    feedback: str
    criteria: list[CriterionScore] = []
    missed_key_points: list[str] = []
    misconceptions: list[str] = []
    unverified_evidence: list[str] = []  # quote ที่ grader อ้าง แต่หาไม่พบในคำตอบจริง


class ConceptMastery(BaseModel):
    concept: str
    points_awarded: float
    max_points: float
    percent: float
    question_ids: list[int]
    source_refs: list[str]


# ---------- รายงานคำแนะนำ (ผลลัพธ์จาก advisor) ----------

class WeakTopic(BaseModel):
    concept: str
    why: str = Field(description="อ่อนเพราะอะไร: ผิดข้อไหน เข้าใจผิดเรื่องอะไร")
    review: list[str] = Field(description="ส่วนของเนื้อหาที่ควรกลับไปทบทวน เช่น 'สไลด์ 4'")
    actions: list[str] = Field(description="สิ่งที่ควรทำเพื่อปรับปรุง แบบเจาะจง")


class LearningReport(BaseModel):
    overall_summary: str
    strengths: list[str]
    weak_topics: list[WeakTopic]
    answer_writing_tips: list[str] = Field(description="คำแนะนำการเขียนตอบอัตนัย ถ้ามี")
    study_plan: list[str]
    practice_prompt: str = Field(description="คำสั่งสำหรับสร้างแบบฝึกชุดใหม่ที่เน้นหัวข้อที่อ่อน")
