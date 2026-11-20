"""ฐานข้อมูลคะแนนของโรงเรียน (SQLite ไฟล์เดียว ค่าเริ่มต้น data/school.db เปลี่ยนที่ DATA_DIR ใน .env)

เก็บห้องเรียน รายชื่อนักเรียน ข้อสอบ และผลสอบรายคนรายข้อ เพื่อดูคะแนนรายคน รายห้อง รายชั้น
และใช้วิเคราะห์ข้อสอบจากผลจริงต่อไป (ตาราง question_scores)

- ข้อมูลนักเรียนเป็นข้อมูลส่วนบุคคล: อยู่ในเครื่องเท่านั้น (โฟลเดอร์ data/ ไม่ขึ้น git)
- โครงสร้างตารางมีเลขเวอร์ชัน (PRAGMA user_version) เปิดไฟล์ของโค้ดเวอร์ชันเก่าแล้วระบบอัปเกรดให้เอง
  โดยสำรองไฟล์เดิมไว้ใน backups/ ก่อน · จะเพิ่มตาราง/คอลัมน์ ให้เพิ่มสคริปต์ต่อท้าย MIGRATIONS ห้ามแก้ของเดิม
- ย้ายเครื่อง: ดาวน์โหลดไฟล์สำรองจากแอป (backup_bytes) แล้วกู้คืนที่เครื่องใหม่ (restore)
"""

import hashlib
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from . import config, scoring
from .schemas import OBJECTIVE_TYPES, ClassSession, Exam, RosterEntry, StudentRecord

MIGRATIONS = [
    # เวอร์ชัน 1
    """
    CREATE TABLE classrooms (
        id INTEGER PRIMARY KEY,
        year INTEGER NOT NULL,              -- ปีการศึกษา (พ.ศ.)
        grade TEXT NOT NULL,                -- ชั้น เช่น ม.4
        room TEXT NOT NULL,                 -- ห้อง เช่น 1
        UNIQUE (year, grade, room)
    );
    CREATE TABLE students (
        id INTEGER PRIMARY KEY,
        code TEXT UNIQUE,                   -- รหัสนักเรียน (เชื่อมคนเดิมข้ามปี) NULL = ไม่มี
        name TEXT NOT NULL
    );
    CREATE TABLE enrollments (              -- รายชื่อห้อง: ใครอยู่ห้องไหน เลขที่อะไร
        classroom_id INTEGER NOT NULL REFERENCES classrooms(id),
        number TEXT NOT NULL,
        student_id INTEGER NOT NULL REFERENCES students(id),
        PRIMARY KEY (classroom_id, number),
        UNIQUE (classroom_id, student_id)
    );
    CREATE TABLE exams (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,   -- ข้อสอบชุดเดียวกันใช้กับหลายห้องได้
        title TEXT NOT NULL,
        exam_json TEXT NOT NULL,            -- ข้อสอบ + เฉลย + หัวข้อของแต่ละข้อ
        created_at TEXT NOT NULL
    );
    CREATE TABLE sittings (                 -- การสอบ 1 ครั้ง = ข้อสอบ 1 ชุดกับห้อง 1 ห้อง
        id INTEGER PRIMARY KEY,
        exam_id INTEGER NOT NULL REFERENCES exams(id),
        classroom_id INTEGER NOT NULL REFERENCES classrooms(id),
        term INTEGER NOT NULL,              -- ภาคเรียน
        created_at TEXT NOT NULL,
        UNIQUE (exam_id, classroom_id)
    );
    CREATE TABLE submissions (              -- คำตอบของผู้สอบ 1 คนในการสอบ 1 ครั้ง
        id INTEGER PRIMARY KEY,
        sitting_id INTEGER NOT NULL REFERENCES sittings(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,          -- ลำดับในตารางคะแนน
        student_id INTEGER REFERENCES students(id),  -- NULL = เลขที่ไม่อยู่ในรายชื่อห้อง
        number TEXT NOT NULL,
        name TEXT NOT NULL,
        source TEXT NOT NULL,               -- csv / answer_sheet
        reviewed INTEGER NOT NULL,
        score REAL,                         -- NULL = ยังไม่ได้ตรวจ
        max_score REAL,
        record_json TEXT NOT NULL           -- StudentRecord ทั้งก้อน (คำตอบ คำเตือน สิ่งที่ AI อ่าน ผลตรวจ)
    );
    CREATE INDEX submissions_by_student ON submissions(student_id);
    CREATE TABLE question_scores (          -- คะแนนรายข้อ: ตารางหลักของการวิเคราะห์
        submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
        question_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        concept TEXT NOT NULL,
        choice INTEGER,                     -- ปรนัย: ตัวเลือกที่ตอบ (เริ่มที่ 0) NULL = ไม่ได้ตอบ / อัตนัย
        is_correct INTEGER,                 -- ปรนัย 1/0 อัตนัย NULL
        points REAL NOT NULL,
        max_points REAL NOT NULL,
        PRIMARY KEY (submission_id, question_id)
    );
    """,
]

SQLITE_HEADER = b"SQLite format 3\x00"


class SchoolDbError(ValueError):
    """ข้อความแสดงให้ผู้ใช้เห็นได้ตรง ๆ"""


@dataclass(frozen=True)
class Classroom:
    id: int
    year: int
    grade: str
    room: str

    @property
    def name(self) -> str:
        return f"{self.grade}/{self.room}"

    @property
    def label(self) -> str:
        return f"{self.name} ปีการศึกษา {self.year}"


@dataclass(frozen=True)
class Sitting:
    id: int
    classroom: Classroom
    term: int
    exam_title: str
    students: int
    graded: int

    @property
    def label(self) -> str:
        return f"{self.exam_title} · {self.classroom.name} ภาคเรียน {self.term}/{self.classroom.year}"


# ---------------- เปิดฐานข้อมูล / อัปเกรดโครงสร้าง ----------------

@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """เปิดฐานข้อมูล (ยังไม่มี = สร้างใหม่, โครงสร้างเก่า = อัปเกรด) ใช้กับ with แล้วปิดให้เอง"""
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            _migrate(conn, path)
        except sqlite3.DatabaseError as e:
            raise SchoolDbError(f"เปิดฐานข้อมูล {path} ไม่ได้ ({e}) — กู้คืนจากไฟล์สำรองได้ที่แท็บ 5") from e
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = len(MIGRATIONS)
    if version > latest:
        raise SchoolDbError(
            f"ฐานข้อมูลนี้มาจากโปรแกรมเวอร์ชันใหม่กว่า (โครงสร้างเวอร์ชัน {version} แต่โค้ดนี้รู้จักถึง {latest}) "
            "— อัปเดตโค้ดด้วย git pull ก่อน"
        )
    if version == latest:
        return
    if version > 0:  # มีข้อมูลอยู่แล้ว สำรองก่อนอัปเกรด
        _copy(conn, _backup_path(path, f"before-v{latest}"))
    for n in range(version, latest):
        try:
            conn.executescript(f"BEGIN;\n{MIGRATIONS[n]}\nPRAGMA user_version = {n + 1};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise


def _backup_path(path: Path, label: str) -> Path:
    return path.parent / "backups" / f"{path.stem}-{label}-{datetime.now():%Y%m%d-%H%M%S}.db"


def _copy(conn: sqlite3.Connection, target: Path) -> None:
    """สำเนาฐานข้อมูลที่เปิดอยู่ไปเป็นไฟล์ใหม่ (ใช้ backup API ของ SQLite ปลอดภัยแม้มีคนใช้อยู่)"""
    target.parent.mkdir(parents=True, exist_ok=True)
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_term(today: Optional[date] = None) -> tuple[int, int]:
    """(ปีการศึกษา พ.ศ., ภาคเรียน) โดยประมาณ: ภาค 1 = พ.ค.–ต.ค., ภาค 2 = พ.ย.–เม.ย."""
    today = today or date.today()
    year = today.year + 543 - (1 if today.month < 5 else 0)
    return year, 1 if 5 <= today.month <= 10 else 2


def _number_order(number: str) -> tuple:
    """เรียงเลขที่แบบตัวเลข (2 ก่อน 10) เลขที่ที่ไม่ใช่ตัวเลขไว้ท้าย"""
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _percent(score: Optional[float], max_score: Optional[float]) -> Optional[float]:
    return round(100 * score / max_score, 1) if score is not None and max_score else None


# ---------------- ห้องเรียนและรายชื่อ ----------------

def add_classroom(conn: sqlite3.Connection, year: int, grade: str, room: str) -> Classroom:
    """เพิ่มห้อง (ถ้ามีอยู่แล้วคืนห้องเดิม) — ตัดช่องว่างออกเพื่อให้ "ม. 4" กับ "ม.4" เป็นชั้นเดียวกัน"""
    grade, room = "".join(grade.split()), "".join(room.split())
    if not grade or not room:
        raise SchoolDbError("กรอกชั้นและห้องให้ครบ")
    with conn:
        conn.execute("INSERT OR IGNORE INTO classrooms (year, grade, room) VALUES (?, ?, ?)", (year, grade, room))
    row = conn.execute(
        "SELECT * FROM classrooms WHERE year = ? AND grade = ? AND room = ?", (year, grade, room)
    ).fetchone()
    return Classroom(row["id"], row["year"], row["grade"], row["room"])


def classrooms(conn: sqlite3.Connection) -> list[Classroom]:
    rows = conn.execute("SELECT * FROM classrooms").fetchall()
    rooms = [Classroom(r["id"], r["year"], r["grade"], r["room"]) for r in rows]
    return sorted(rooms, key=lambda c: (-c.year, c.grade, _number_order(c.room)))


def roster(conn: sqlite3.Connection, classroom_id: int) -> list[RosterEntry]:
    rows = conn.execute(
        """SELECT e.number, s.name, COALESCE(s.code, '') AS code
           FROM enrollments e JOIN students s ON s.id = e.student_id WHERE e.classroom_id = ?""",
        (classroom_id,),
    ).fetchall()
    entries = [RosterEntry(number=r["number"], name=r["name"], code=r["code"]) for r in rows]
    return sorted(entries, key=lambda e: _number_order(e.number))


def import_roster(conn: sqlite3.Connection, classroom_id: int, entries: list[RosterEntry]) -> tuple[int, int]:
    """เพิ่ม/แก้รายชื่อห้อง คืน (เพิ่มใหม่, มีอยู่แล้ว)
    คนเดิม = เลขที่เดิมในห้องนี้ หรือรหัสนักเรียนเดิม (เช่น ขึ้นชั้นใหม่ ชื่อจะเชื่อมกับผลสอบปีก่อน)"""
    added = existing = 0
    with conn:
        for e in entries:
            code = e.code.strip() or None
            enrolled = conn.execute(
                "SELECT student_id FROM enrollments WHERE classroom_id = ? AND number = ?", (classroom_id, e.number)
            ).fetchone()
            by_code = conn.execute("SELECT id FROM students WHERE code = ?", (code,)).fetchone() if code else None
            student_id = by_code["id"] if by_code else enrolled["student_id"] if enrolled else None
            if student_id is None:
                student_id = conn.execute(
                    "INSERT INTO students (code, name) VALUES (?, ?)", (code, e.name)
                ).lastrowid
            else:
                conn.execute(
                    "UPDATE students SET name = ?, code = COALESCE(?, code) WHERE id = ?", (e.name, code, student_id)
                )
            # เลขที่หนึ่งมีได้คนเดียว และคนหนึ่งมีได้เลขที่เดียวในห้อง
            conn.execute(
                "DELETE FROM enrollments WHERE classroom_id = ? AND (number = ? OR student_id = ?)",
                (classroom_id, e.number, student_id),
            )
            conn.execute(
                "INSERT INTO enrollments (classroom_id, number, student_id) VALUES (?, ?, ?)",
                (classroom_id, e.number, student_id),
            )
            if enrolled:
                existing += 1
            else:
                added += 1
        # ผลสอบที่บันทึกไว้ก่อนมีรายชื่อ ให้เชื่อมกับนักเรียนตามเลขที่
        conn.execute(
            """UPDATE submissions SET student_id = (
                   SELECT e.student_id FROM enrollments e JOIN sittings s ON s.classroom_id = e.classroom_id
                   WHERE s.id = submissions.sitting_id AND e.number = submissions.number)
               WHERE sitting_id IN (SELECT id FROM sittings WHERE classroom_id = ?)""",
            (classroom_id,),
        )
    return added, existing


# ---------------- การสอบ (ข้อสอบ 1 ชุด × ห้อง 1 ห้อง) ----------------

def open_sitting(conn: sqlite3.Connection, exam: Exam, classroom_id: int, term: int) -> int:
    """id ของการสอบข้อสอบชุดนี้ในห้องนี้ (ยังไม่มี = สร้างใหม่, มีแล้ว = ใช้ของเดิม)"""
    data = exam.model_dump_json()
    fingerprint = hashlib.sha256(data.encode("utf-8")).hexdigest()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO exams (fingerprint, title, exam_json, created_at) VALUES (?, ?, ?, ?)",
            (fingerprint, exam.title, data, _now()),
        )
        exam_id = conn.execute("SELECT id FROM exams WHERE fingerprint = ?", (fingerprint,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO sittings (exam_id, classroom_id, term, created_at) VALUES (?, ?, ?, ?)",
            (exam_id, classroom_id, term, _now()),
        )
        return conn.execute(
            "SELECT id FROM sittings WHERE exam_id = ? AND classroom_id = ?", (exam_id, classroom_id)
        ).fetchone()["id"]


def sittings(conn: sqlite3.Connection, sitting_id: Optional[int] = None) -> list[Sitting]:
    """การสอบทั้งหมด (ล่าสุดก่อน) หรือเฉพาะ sitting_id"""
    rows = conn.execute(
        f"""SELECT s.id, s.term, e.title, c.id AS classroom_id, c.year, c.grade, c.room,
                   COUNT(sub.id) AS students, COUNT(sub.score) AS graded
            FROM sittings s JOIN exams e ON e.id = s.exam_id JOIN classrooms c ON c.id = s.classroom_id
            LEFT JOIN submissions sub ON sub.sitting_id = s.id
            {"WHERE s.id = ?" if sitting_id is not None else ""}
            GROUP BY s.id ORDER BY s.created_at DESC, s.id DESC""",
        () if sitting_id is None else (sitting_id,),
    ).fetchall()
    return [
        Sitting(r["id"], Classroom(r["classroom_id"], r["year"], r["grade"], r["room"]), r["term"], r["title"],
                r["students"], r["graded"])
        for r in rows
    ]


def same_exam_sittings(conn: sqlite3.Connection, sitting_id: int) -> list[Sitting]:
    """การสอบทุกห้องที่ใช้ข้อสอบชุดเดียวกับ sitting_id (รวมตัวเอง) ใช้รวมผลหลายห้องตอนวิเคราะห์ข้อสอบ"""
    rows = conn.execute(
        """SELECT other.id FROM sittings s JOIN sittings other ON other.exam_id = s.exam_id
           WHERE s.id = ? ORDER BY other.id""",
        (sitting_id,),
    ).fetchall()
    return [sittings(conn, r["id"])[0] for r in rows]


def _sitting_classroom(conn: sqlite3.Connection, sitting_id: int) -> int:
    row = conn.execute("SELECT classroom_id FROM sittings WHERE id = ?", (sitting_id,)).fetchone()
    if row is None:
        raise SchoolDbError("ไม่พบการสอบนี้ในฐานข้อมูล (อาจเพิ่งกู้คืนฐานข้อมูลอื่นมาแทน) — ปิดแล้วเปิดการสอบใหม่")
    return row["classroom_id"]


def load_session(conn: sqlite3.Connection, sitting_id: int) -> tuple[Sitting, ClassSession]:
    _sitting_classroom(conn, sitting_id)
    exam_json = conn.execute(
        "SELECT e.exam_json FROM sittings s JOIN exams e ON e.id = s.exam_id WHERE s.id = ?", (sitting_id,)
    ).fetchone()["exam_json"]
    rows = conn.execute(
        "SELECT record_json FROM submissions WHERE sitting_id = ? ORDER BY position", (sitting_id,)
    ).fetchall()
    session = ClassSession(
        exam=Exam.model_validate_json(exam_json),
        students=[StudentRecord.model_validate_json(r["record_json"]) for r in rows],
    )
    return sittings(conn, sitting_id)[0], session


def save_session(conn: sqlite3.Connection, sitting_id: int, session: ClassSession) -> None:
    """บันทึกผู้สอบทั้งห้องของการสอบนี้ (แทนที่ของเดิมทั้งหมดในครั้งเดียว)"""
    with conn:
        classroom_id = _sitting_classroom(conn, sitting_id)
        enrolled = {
            r["number"]: r["student_id"]
            for r in conn.execute("SELECT number, student_id FROM enrollments WHERE classroom_id = ?", (classroom_id,))
        }
        conn.execute("DELETE FROM submissions WHERE sitting_id = ?", (sitting_id,))
        for position, s in enumerate(session.students):
            totals = scoring.totals(s.results) if s.results is not None else None
            submission_id = conn.execute(
                """INSERT INTO submissions (sitting_id, position, student_id, number, name, source, reviewed,
                                            score, max_score, record_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sitting_id, position, enrolled.get(s.student_no), s.student_no, s.name, s.source, s.reviewed,
                 totals["score"] if totals else None, totals["max"] if totals else None, s.model_dump_json()),
            ).lastrowid
            conn.executemany(
                """INSERT INTO question_scores (submission_id, question_id, type, concept, choice, is_correct,
                                                points, max_points) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (submission_id, r.question_id, r.type, r.concept,
                     s.answers.get(r.question_id) if r.type in OBJECTIVE_TYPES else None,
                     r.is_correct, r.points_awarded, r.max_points)
                    for r in s.results or []
                ],
            )


# ---------------- ดูคะแนน: รายห้อง / รายคน / รายชั้น (ร้อยละของคะแนนเต็ม เฉพาะคนที่ตรวจแล้ว) ----------------

def classroom_table(conn: sqlite3.Connection, classroom_id: int) -> list[dict]:
    """แถว = นักเรียน (ตามรายชื่อ + ผู้สอบที่ไม่อยู่ในรายชื่อ), คอลัมน์ = การสอบแต่ละครั้ง, ค่า = ร้อยละ"""
    rows = conn.execute(
        """SELECT s.id AS sitting_id, e.title, s.term, sub.number, sub.name, sub.score, sub.max_score
           FROM sittings s JOIN exams e ON e.id = s.exam_id JOIN submissions sub ON sub.sitting_id = s.id
           WHERE s.classroom_id = ? ORDER BY s.created_at, s.id, sub.position""",
        (classroom_id,),
    ).fetchall()
    columns: dict[int, str] = {}
    for r in rows:
        if r["sitting_id"] not in columns:
            title = f"{r['title']} (ภาค {r['term']})"
            columns[r["sitting_id"]] = title if title not in columns.values() else f"{title} #{len(columns) + 1}"
    table = {e.number: {"เลขที่": e.number, "ชื่อ": e.name} for e in roster(conn, classroom_id)}
    for r in rows:
        row = table.setdefault(r["number"] or r["name"], {"เลขที่": r["number"], "ชื่อ": r["name"]})
        row[columns[r["sitting_id"]]] = _percent(r["score"], r["max_score"])
    result = []
    for row in sorted(table.values(), key=lambda row: (_number_order(row["เลขที่"]), row["ชื่อ"])):
        scores = [row.get(c) for c in columns.values()]
        done = [s for s in scores if s is not None]
        result.append({
            "เลขที่": row["เลขที่"], "ชื่อ": row["ชื่อ"], **dict(zip(columns.values(), scores)),
            "เฉลี่ย": round(sum(done) / len(done), 1) if done else None,
        })
    return result


# นักเรียนคนเดียวกัน: ลิงก์กับรายชื่อ (ข้ามห้อง/ข้ามปีได้) หรือเลขที่ (ชื่อ ถ้าไม่มีเลขที่) เดียวกันในห้องนี้
_SAME_STUDENT = """(sub.student_id = :sid OR (s.classroom_id = :cid
                   AND (CASE WHEN sub.number != '' THEN sub.number ELSE sub.name END) = :key))"""


def _student_params(conn: sqlite3.Connection, classroom_id: int, key: str) -> dict:
    row = conn.execute(
        "SELECT student_id FROM enrollments WHERE classroom_id = ? AND number = ?", (classroom_id, key)
    ).fetchone()
    return {"sid": row["student_id"] if row else None, "cid": classroom_id, "key": key}


def student_history(conn: sqlite3.Connection, classroom_id: int, key: str) -> list[dict]:
    """ผลสอบทุกครั้งที่ตรวจแล้วของนักเรียน (key = เลขที่ในห้องนี้ หรือชื่อถ้าไม่มีเลขที่)"""
    rows = conn.execute(
        f"""SELECT c.year, s.term, c.grade, c.room, e.title, sub.score, sub.max_score
            FROM submissions sub JOIN sittings s ON s.id = sub.sitting_id
            JOIN classrooms c ON c.id = s.classroom_id JOIN exams e ON e.id = s.exam_id
            WHERE sub.score IS NOT NULL AND {_SAME_STUDENT}
            ORDER BY c.year, s.term, s.created_at""",
        _student_params(conn, classroom_id, key),
    ).fetchall()
    return [
        {"ปีการศึกษา": r["year"], "ภาคเรียน": r["term"], "ห้อง": f"{r['grade']}/{r['room']}", "ข้อสอบ": r["title"],
         "คะแนน": r["score"], "เต็ม": r["max_score"], "ร้อยละ": _percent(r["score"], r["max_score"])}
        for r in rows
    ]


def student_concepts(conn: sqlite3.Connection, classroom_id: int, key: str) -> list[dict]:
    """คะแนนรายหัวข้อรวมทุกการสอบ เรียงจากหัวข้อที่อ่อนที่สุด"""
    rows = conn.execute(
        f"""SELECT qs.concept, SUM(qs.points) AS points, SUM(qs.max_points) AS max_points, COUNT(*) AS n
            FROM question_scores qs JOIN submissions sub ON sub.id = qs.submission_id
            JOIN sittings s ON s.id = sub.sitting_id
            WHERE {_SAME_STUDENT} GROUP BY qs.concept""",
        _student_params(conn, classroom_id, key),
    ).fetchall()
    result = [
        {"หัวข้อ": r["concept"], "ได้": r["points"], "เต็ม": r["max_points"],
         "ร้อยละ": _percent(r["points"], r["max_points"]), "จำนวนข้อ": r["n"]}
        for r in rows
    ]
    return sorted(result, key=lambda row: (row["ร้อยละ"] is None, row["ร้อยละ"]))


def grades(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(ปีการศึกษา, ชั้น) ที่มีห้องเรียน"""
    rows = conn.execute("SELECT DISTINCT year, grade FROM classrooms ORDER BY year DESC, grade").fetchall()
    return [(r["year"], r["grade"]) for r in rows]


def _grade_rooms(conn: sqlite3.Connection, year: int, grade: str) -> list[Classroom]:
    return [c for c in classrooms(conn) if c.year == year and c.grade == grade]


def grade_exam_table(conn: sqlite3.Connection, year: int, grade: str) -> list[dict]:
    """แถว = ข้อสอบ, คอลัมน์ = ห้องในชั้นนี้, ค่า = ร้อยละเฉลี่ยของห้อง"""
    rows = conn.execute(
        """SELECT e.id AS exam_id, e.title, c.id AS classroom_id, AVG(sub.score * 100.0 / sub.max_score) AS avg
           FROM sittings s JOIN exams e ON e.id = s.exam_id JOIN classrooms c ON c.id = s.classroom_id
           JOIN submissions sub ON sub.sitting_id = s.id AND sub.score IS NOT NULL AND sub.max_score > 0
           WHERE c.year = ? AND c.grade = ? GROUP BY s.id ORDER BY MIN(s.created_at)""",
        (year, grade),
    ).fetchall()
    rooms = _grade_rooms(conn, year, grade)
    table: dict[int, dict] = {}
    for r in rows:
        row = table.setdefault(r["exam_id"], {"ข้อสอบ": r["title"], **{c.name: None for c in rooms}})
        row[next(c.name for c in rooms if c.id == r["classroom_id"])] = round(r["avg"], 1)
    return list(table.values())


def grade_concept_table(conn: sqlite3.Connection, year: int, grade: str) -> list[dict]:
    """แถว = หัวข้อ, คอลัมน์ = ห้อง + ทั้งชั้น, ค่า = ร้อยละ — เรียงจากหัวข้อที่ทั้งชั้นอ่อนที่สุด"""
    rows = conn.execute(
        """SELECT qs.concept, c.id AS classroom_id, SUM(qs.points) AS points, SUM(qs.max_points) AS max_points
           FROM question_scores qs JOIN submissions sub ON sub.id = qs.submission_id
           JOIN sittings s ON s.id = sub.sitting_id JOIN classrooms c ON c.id = s.classroom_id
           WHERE c.year = ? AND c.grade = ? GROUP BY qs.concept, c.id""",
        (year, grade),
    ).fetchall()
    rooms = {c.id: c.name for c in _grade_rooms(conn, year, grade)}
    table: dict[str, dict] = {}
    totals: dict[str, list[float]] = {}
    for r in rows:
        table.setdefault(r["concept"], {"หัวข้อ": r["concept"], **{name: None for name in rooms.values()}})
        table[r["concept"]][rooms[r["classroom_id"]]] = _percent(r["points"], r["max_points"])
        total = totals.setdefault(r["concept"], [0.0, 0.0])
        total[0] += r["points"]
        total[1] += r["max_points"]
    result = [{**row, "ทั้งชั้น": _percent(*totals[concept])} for concept, row in table.items()]
    return sorted(result, key=lambda row: (row["ทั้งชั้น"] is None, row["ทั้งชั้น"]))


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("classrooms", "students", "sittings", "submissions")
    }


# ---------------- สำรอง / กู้คืน (ย้ายเครื่อง) ----------------

def backup_bytes(path: Optional[Path] = None) -> bytes:
    """สำเนาฐานข้อมูลทั้งไฟล์ (ทำได้แม้แอปกำลังใช้อยู่) สำหรับดาวน์โหลดเก็บหรือย้ายเครื่อง"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "school.db"
        with connect(path) as conn:
            _copy(conn, copy)
        return copy.read_bytes()


def _check_backup(conn: sqlite3.Connection) -> None:
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as e:
        raise SchoolDbError("ไฟล์ฐานข้อมูลเสียหาย เปิดไม่ได้") from e
    if not ok:
        raise SchoolDbError("ไฟล์ฐานข้อมูลเสียหาย (ตรวจความสมบูรณ์ไม่ผ่าน)")
    if version > len(MIGRATIONS):
        raise SchoolDbError("ไฟล์สำรองนี้มาจากโปรแกรมเวอร์ชันใหม่กว่า — อัปเดตโค้ดด้วย git pull ก่อนกู้คืน")
    if version < 1 or not {"classrooms", "submissions"} <= tables:
        raise SchoolDbError("ไฟล์นี้ไม่ใช่ฐานข้อมูลของแอปนี้")


def restore(data: bytes, path: Optional[Path] = None) -> Optional[Path]:
    """แทนที่ฐานข้อมูลทั้งหมดด้วยไฟล์สำรอง คืนที่อยู่สำเนาของฐานข้อมูลเดิม (เก็บไว้ใน backups/ ก่อนแทนที่)"""
    path = Path(path or config.DB_PATH)
    if not data.startswith(SQLITE_HEADER):
        raise SchoolDbError("ไฟล์นี้ไม่ใช่ไฟล์ฐานข้อมูล (.db) ที่ดาวน์โหลดจากแอป")
    previous = None
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        uploaded = Path(tmp) / "upload.db"
        uploaded.write_bytes(data)
        source = sqlite3.connect(uploaded)
        try:
            _check_backup(source)
            if path.exists():
                previous = _backup_path(path, "before-restore")
                previous.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, previous)
            path.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    with connect(path):  # ไฟล์สำรองจากโค้ดเวอร์ชันเก่า = อัปเกรดโครงสร้างให้ทันที
        pass
    return previous
