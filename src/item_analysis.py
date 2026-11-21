"""วิเคราะห์ข้อสอบจากผลสอบจริง (คำนวณด้วยโค้ด ไม่ใช้ LLM)

- ค่าความยาก p: สัดส่วนคะแนนเฉลี่ยของข้อ (ปรนัย = สัดส่วนคนตอบถูก)
- ค่าอำนาจจำแนก r: p ของกลุ่มเก่ง − p ของกลุ่มอ่อน (กลุ่มละ 27% ตามคะแนนรวม)
- ตัวลวง (ปรนัย): จำนวนคนเลือกแต่ละตัวเลือก แยกกลุ่มเก่ง/อ่อน
- ค่าความเชื่อมั่นทั้งฉบับ: KR-20 (ปรนัยล้วน) หรือ Cronbach's alpha (มีอัตนัย) ซึ่งเป็นสูตรเดียวกันแบบทั่วไป

เกณฑ์แปลผลตามแนวทางการวิเคราะห์ข้อสอบที่ใช้ในโรงเรียนไทย: p ที่ใช้ได้ 0.20–0.80, r ตั้งแต่ 0.20 ขึ้นไป
"""

from dataclasses import dataclass, field
from statistics import mean, pstdev, pvariance
from typing import Optional

from . import scoring
from .schemas import OBJECTIVE_TYPES, Exam, Question, StudentRecord

GROUP_SHARE = 0.27  # สัดส่วนกลุ่มเก่ง / กลุ่มอ่อน
MIN_STUDENTS = 2
RELIABLE_STUDENTS = 20  # ผู้สอบน้อยกว่านี้ ค่าสถิติแกว่งง่าย
P_RANGE = (0.20, 0.80)
R_MIN = 0.20
RELIABILITY_MIN = 0.70


@dataclass
class ChoiceStat:
    label: str  # "ก. ข้อความ" / "(ไม่ได้ตอบ)"
    is_key: bool
    total: int
    upper: int
    lower: int


@dataclass
class ItemStat:
    question_id: int
    type: str
    concept: str
    p: float
    r: float
    choices: list[ChoiceStat] = field(default_factory=list)  # เฉพาะปรนัย
    flags: list[str] = field(default_factory=list)  # ข้อสังเกตที่ควรตรวจ (ว่าง = ใช้ได้)

    @property
    def p_label(self) -> str:
        return difficulty_label(self.p)

    @property
    def r_label(self) -> str:
        return discrimination_label(self.r)


@dataclass
class ExamStats:
    n: int  # จำนวนผู้สอบที่ตรวจแล้ว
    group_size: int  # จำนวนคนในกลุ่มเก่ง / กลุ่มอ่อน
    mean_percent: float
    sd: float  # ส่วนเบี่ยงเบนมาตรฐานของคะแนนรวม
    reliability: Optional[float]
    reliability_name: str
    items: list[ItemStat]
    warnings: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> list[ItemStat]:
        return [i for i in self.items if i.flags]


def difficulty_label(p: float) -> str:
    if p < 0.20:
        return "ยากมาก"
    if p < 0.40:
        return "ค่อนข้างยาก"
    if p <= 0.60:
        return "ปานกลาง"
    if p <= 0.80:
        return "ค่อนข้างง่าย"
    return "ง่ายมาก"


def discrimination_label(r: float) -> str:
    if r < 0:
        return "ติดลบ"
    if r < 0.20:
        return "ต่ำ"
    if r < 0.30:
        return "พอใช้"
    if r < 0.40:
        return "ดี"
    return "ดีมาก"


def reliability_label(value: float) -> str:
    if value >= 0.80:
        return "สูง"
    return "ใช้ได้" if value >= RELIABILITY_MIN else "ต่ำ"


def _ratio(points: float, max_points: float) -> float:
    return points / max_points if max_points else 0.0


def _choice_stats(q: Question, answers: list, upper: set[int], lower: set[int]) -> list[ChoiceStat]:
    """answers[i] = index ตัวเลือกที่ผู้สอบคนที่ i เลือก (None = ไม่ได้ตอบ)"""
    options: list[Optional[int]] = list(range(len(q.choices))) + [None]
    stats = []
    for option in options:
        chose = {i for i, a in enumerate(answers) if a == option}
        if option is None and not chose:
            continue
        stats.append(ChoiceStat(
            label=scoring.choice_label(q, option), is_key=option == q.correct_choice,
            total=len(chose), upper=len(chose & upper), lower=len(chose & lower),
        ))
    return stats


def _flags(q: Question, p: float, r: float, choices: list[ChoiceStat]) -> list[str]:
    flags = []
    if p < P_RANGE[0]:
        flags.append(f"ยากมาก (p = {p:.2f})")
    elif p > P_RANGE[1]:
        flags.append(f"ง่ายมาก (p = {p:.2f})")
    if r < 0:
        flags.append(f"อำนาจจำแนกติดลบ (r = {r:.2f}) — กลุ่มเก่งได้คะแนนน้อยกว่ากลุ่มอ่อน เฉลยอาจผิดหรือคำถามกำกวม")
    elif r < R_MIN:
        flags.append(f"อำนาจจำแนกต่ำ (r = {r:.2f})")
    key = next((c for c in choices if c.is_key), None)
    for c in choices:
        if c.is_key or c.label == "(ไม่ได้ตอบ)":
            continue
        if q.type == "mcq" and c.total == 0:
            flags.append(f"ตัวลวง “{c.label}” ไม่มีคนเลือก")
        elif c.upper > c.lower:
            flags.append(f"ตัวลวง “{c.label}” ดึงกลุ่มเก่งได้มากกว่ากลุ่มอ่อน")
        if key is not None and c.total > key.total:
            flags.append(f"มีคนเลือก “{c.label}” มากกว่าเฉลย")
    return flags


def analyze(exam: Exam, students: list[StudentRecord]) -> Optional[ExamStats]:
    """วิเคราะห์จากผู้สอบที่ตรวจแล้ว (คืน None ถ้าตรวจแล้วไม่ถึง MIN_STUDENTS คน)"""
    graded = [s for s in students if s.results is not None]
    n = len(graded)
    if n < MIN_STUDENTS:
        return None
    by_id = [{r.question_id: r for r in s.results} for s in graded]
    points = {q.id: [res[q.id].points_awarded if q.id in res else 0.0 for res in by_id] for q in exam.questions}
    totals = [sum(points[q.id][i] for q in exam.questions) for i in range(n)]
    full = sum(q.points for q in exam.questions)

    group_size = max(1, round(GROUP_SHARE * n))
    ranked = sorted(range(n), key=lambda i: totals[i], reverse=True)
    upper, lower = set(ranked[:group_size]), set(ranked[-group_size:])

    items = []
    for q in exam.questions:
        ratios = [_ratio(pts, q.points) for pts in points[q.id]]
        p = mean(ratios)
        r = mean(ratios[i] for i in upper) - mean(ratios[i] for i in lower)
        choices = []
        if q.type in OBJECTIVE_TYPES:
            answers = [s.answers.get(q.id) for s in graded]
            choices = _choice_stats(q, [a if isinstance(a, int) else None for a in answers], upper, lower)
        items.append(ItemStat(question_id=q.id, type=q.type, concept=q.concept, p=round(p, 2), r=round(r, 2),
                              choices=choices, flags=_flags(q, p, r, choices)))

    # alpha = k/(k−1) × (1 − Σ ความแปรปรวนรายข้อ / ความแปรปรวนคะแนนรวม) — ข้อ 0/1 ล้วนจะเท่ากับ KR-20
    k = len(exam.questions)
    total_var = pvariance(totals)
    reliability = None
    if k > 1 and total_var > 0:
        item_var = sum(pvariance(points[q.id]) for q in exam.questions)
        reliability = round(k / (k - 1) * (1 - item_var / total_var), 2)
    objective_only = all(q.type in OBJECTIVE_TYPES for q in exam.questions)

    warnings = []
    if n < RELIABLE_STUDENTS:
        warnings.append(f"ผู้สอบ {n} คน (น้อยกว่า {RELIABLE_STUDENTS}) ค่าสถิติเปลี่ยนแปลงง่าย ใช้ประกอบการพิจารณาเท่านั้น")
    if reliability is None:
        warnings.append("คำนวณค่าความเชื่อมั่นไม่ได้ (ทุกคนได้คะแนนรวมเท่ากัน หรือมีข้อเดียว)")
    return ExamStats(
        n=n, group_size=group_size, mean_percent=round(100 * mean(totals) / full, 1) if full else 0.0,
        sd=round(pstdev(totals), 2), reliability=reliability,
        reliability_name="KR-20" if objective_only else "Cronbach's alpha", items=items, warnings=warnings,
    )
