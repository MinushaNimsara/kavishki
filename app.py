from __future__ import annotations

import csv
import os
import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date
from typing import List, Tuple, Dict, Any, Optional

from ml_edubear import (
    train_and_save, load_model, predict_success_prob,
    classify_weakness, compute_risk_score, estimate_confidence, recommend_next_lesson
)
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, jsonify

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FIREBASE_CRED_PATH = os.path.join(APP_DIR, "firebase-credentials.json")
FIREBASE_WEB_CONFIG_PATH = os.path.join(APP_DIR, "firebase-web-config.json")
IS_VERCEL = os.environ.get("VERCEL") == "1"

# Initialize Firebase Admin (backend token verification)
try:
    import firebase_admin
    from firebase_admin import credentials, auth as firebase_auth
    cred = None
    cred_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
    elif os.path.exists(FIREBASE_CRED_PATH):
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
    if cred:
        firebase_admin.initialize_app(cred)
        FIREBASE_ENABLED = True
    else:
        FIREBASE_ENABLED = False
except Exception:
    FIREBASE_ENABLED = False


def get_firebase_web_config():
    """Load Firebase web config for frontend. Returns None if missing/invalid."""
    cfg_json = os.environ.get("FIREBASE_WEB_CONFIG_JSON")
    if cfg_json:
        try:
            cfg = json.loads(cfg_json)
            if cfg.get("apiKey") and cfg.get("apiKey") != "YOUR_API_KEY":
                return cfg
        except Exception:
            pass
    if os.path.exists(FIREBASE_WEB_CONFIG_PATH):
        try:
            with open(FIREBASE_WEB_CONFIG_PATH) as f:
                cfg = json.load(f)
            if cfg.get("apiKey") and cfg.get("apiKey") != "YOUR_API_KEY":
                return cfg
        except Exception:
            pass
    return None

DB_PATH = os.path.join("/tmp" if IS_VERCEL else APP_DIR, "app.db")
DATA_DIR = os.path.join(APP_DIR, "data")

app = Flask(
    __name__,
    static_folder=os.path.join(APP_DIR, "static"),
    template_folder=os.path.join(APP_DIR, "templates"),
    static_url_path="/static"
)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")

SUBJECTS = ["Mathematics", "English", "Science"]

TOPICS = {
    "Math": ["addition", "subtraction", "fractions"],
    "Mathematics": ["addition", "subtraction", "fractions"],
    "English": ["vocabulary", "grammar", "reading"],
    "Science": ["plants", "animals", "matter_forces"],
}

UNLOCK_MASTERY = 0.65
DIFFICULTIES = ["easy", "medium", "hard"]

# ---------------- DB ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


def _map_level_to_stage(level: str) -> str:
    if level == "Easy":
        return "easy"
    if level == "Simple":
        return "medium"
    return "hard"


STAGE_SUBJECTS = ["Math", "English", "Science", "General Knowledge"]


def _import_stage_questions(db) -> None:
    """Import questions from CSV. Clear admin_questions first."""
    db.execute("DELETE FROM admin_questions")
    db.execute("DELETE FROM mastery")
    db.execute("DELETE FROM attempts")
    db.execute("DROP TABLE IF EXISTS stage_progress")
    db.execute("DROP TABLE IF EXISTS stage_questions")
    db.execute("""
        CREATE TABLE stage_questions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          grade INTEGER NOT NULL,
          subject TEXT NOT NULL,
          stage TEXT NOT NULL,
          q_text TEXT NOT NULL,
          option_a TEXT NOT NULL,
          option_b TEXT NOT NULL,
          option_c TEXT NOT NULL,
          option_d TEXT NOT NULL,
          answer TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE stage_progress(
          user_id INTEGER NOT NULL,
          subject TEXT NOT NULL,
          stage TEXT NOT NULL,
          completed INTEGER NOT NULL DEFAULT 0,
          completed_at TEXT,
          PRIMARY KEY (user_id, subject, stage)
        )
    """)
    csv_path = os.path.join(DATA_DIR, "questions.csv")
    if not os.path.exists(csv_path):
        return
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            grade = int(row.get("grade", 1))
            subject = row.get("subject", "Math")
            stage = _map_level_to_stage(row.get("level", "Easy"))
            q_text = row.get("question", "")
            opt_a = row.get("optionA", "")
            opt_b = row.get("optionB", "")
            opt_c = row.get("optionC", "")
            opt_d = row.get("optionD", "")
            ans_letter = (row.get("answer", "A") or "A").upper()
            ans_map = {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d}
            answer = ans_map.get(ans_letter, opt_a)
            db.execute(
                "INSERT INTO stage_questions(grade, subject, stage, q_text, option_a, option_b, option_c, option_d, answer) VALUES(?,?,?,?,?,?,?,?,?)",
                (grade, subject, stage, q_text, opt_a, opt_b, opt_c, opt_d, answer)
            )


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      nickname TEXT NOT NULL,
      grade INTEGER NOT NULL,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      xp INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'student',
      parent_id INTEGER REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS parent_child(
      parent_id INTEGER NOT NULL REFERENCES users(id),
      child_id INTEGER NOT NULL REFERENCES users(id),
      PRIMARY KEY (parent_id, child_id)
    );

    CREATE TABLE IF NOT EXISTS badges(
      user_id INTEGER NOT NULL REFERENCES users(id),
      badge_id TEXT NOT NULL,
      earned_at TEXT NOT NULL,
      PRIMARY KEY (user_id, badge_id)
    );

    CREATE TABLE IF NOT EXISTS mastery(
      user_id INTEGER NOT NULL,
      key TEXT NOT NULL,
      mastery REAL NOT NULL,
      PRIMARY KEY (user_id, key)
    );

    CREATE TABLE IF NOT EXISTS attempts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      subject TEXT NOT NULL,
      topic TEXT NOT NULL,
      difficulty TEXT NOT NULL,
      total_q INTEGER NOT NULL,
      correct_q INTEGER NOT NULL,
      started_at REAL NOT NULL,
      ended_at REAL NOT NULL,
      hints_used INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS streaks(
      user_id INTEGER PRIMARY KEY,
      last_practice_date TEXT,
      streak_count INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS model_predictions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL,
      predicted_difficulty TEXT,
      risk_score REAL,
      recommended_subject TEXT,
      recommended_topic TEXT,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admin_questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      subject TEXT NOT NULL,
      topic TEXT NOT NULL,
      difficulty TEXT NOT NULL,
      grade_min INTEGER DEFAULT 1,
      grade_max INTEGER DEFAULT 5,
      q_text TEXT NOT NULL,
      options_json TEXT NOT NULL,
      answer TEXT NOT NULL,
      hint TEXT,
      disabled INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stage_questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      grade INTEGER NOT NULL,
      subject TEXT NOT NULL,
      stage TEXT NOT NULL,
      q_text TEXT NOT NULL,
      option_a TEXT NOT NULL,
      option_b TEXT NOT NULL,
      option_c TEXT NOT NULL,
      option_d TEXT NOT NULL,
      answer TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS stage_progress(
      user_id INTEGER NOT NULL,
      subject TEXT NOT NULL,
      stage TEXT NOT NULL,
      completed INTEGER NOT NULL DEFAULT 0,
      completed_at TEXT,
      PRIMARY KEY (user_id, subject, stage)
    );
    """)
    # Migrate: add role, parent_id, firebase_uid to users if missing
    for sql in ["ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'",
                "ALTER TABLE users ADD COLUMN parent_id INTEGER REFERENCES users(id)",
                "ALTER TABLE users ADD COLUMN firebase_uid TEXT",
                "ALTER TABLE users ADD COLUMN age INTEGER"]:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Load stage questions from CSV if empty or schema outdated (no subject column)
    try:
        db.execute("SELECT subject FROM stage_questions LIMIT 1")
        has_subject = True
    except sqlite3.OperationalError:
        has_subject = False
    count = db.execute("SELECT COUNT(*) as c FROM stage_questions").fetchone()["c"]
    if count == 0 or not has_subject:
        _import_stage_questions(db)

    db.commit()


@app.before_request
def _ensure_db():
    init_db()


# ---------------- Auth helpers ----------------
def require_login():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("needs_grade_pick") and request.endpoint not in ("ask_grade", "ask_grade_post"):
        return redirect(url_for("ask_grade"))
    return None


# ---------------- Grade helpers ----------------
def get_user_grade(user_id: int) -> int:
    db = get_db()
    row = db.execute("SELECT grade FROM users WHERE id=?", (user_id,)).fetchone()
    return int(row["grade"]) if row and row["grade"] else 1


STAGES = ["easy", "medium", "hard"]


def get_stage_completed(user_id: int, subject: str, stage: str) -> bool:
    db = get_db()
    row = db.execute("SELECT completed FROM stage_progress WHERE user_id=? AND subject=? AND stage=?", (user_id, subject, stage)).fetchone()
    return bool(row and row["completed"])


def set_stage_completed(user_id: int, subject: str, stage: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO stage_progress(user_id, subject, stage, completed, completed_at) VALUES(?,?,?,1,?) "
        "ON CONFLICT(user_id, subject, stage) DO UPDATE SET completed=1, completed_at=excluded.completed_at",
        (user_id, subject, stage, datetime.utcnow().isoformat())
    )
    db.commit()


def get_stage_best_score(user_id: int, subject: str, stage: str) -> Tuple[int, int, float]:
    """Returns (correct, total, percentage) for best attempt. (0, 0, 0) if no attempts."""
    db = get_db()
    rows = db.execute(
        "SELECT correct_q, total_q FROM attempts WHERE user_id=? AND subject=? AND topic=? AND total_q>0 ORDER BY (1.0*correct_q/total_q) DESC LIMIT 1",
        (user_id, subject, stage)
    ).fetchall()
    if not rows:
        return 0, 0, 0.0
    r = rows[0]
    c, t = int(r["correct_q"]), int(r["total_q"])
    pct = round(100 * c / t, 0) if t else 0
    return c, t, float(pct)


def is_stage_unlocked(user_id: int, subject: str, stage: str) -> bool:
    if stage == "easy":
        return True
    idx = STAGES.index(stage) if stage in STAGES else 0
    prev_stage = STAGES[idx - 1]
    return get_stage_completed(user_id, subject, prev_stage)


def get_stage_questions(grade: int, subject: str, stage: str, n: int = 10) -> List[Dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, q_text, option_a, option_b, option_c, option_d, answer FROM stage_questions WHERE grade=? AND subject=? AND stage=? ORDER BY RANDOM() LIMIT ?",
        (grade, subject, stage, n)
    ).fetchall()
    out = []
    for r in rows:
        opts = [r["option_a"], r["option_b"], r["option_c"], r["option_d"]]
        random.shuffle(opts)
        out.append({
            "q": r["q_text"],
            "options": opts,
            "answer": r["answer"],
            "hint": "Think carefully."
        })
    return out


def cap_difficulty_by_grade(grade: int, diff: str) -> str:
    """
    Grade 1-3: only easy
    Grade 4-5: easy or medium (no hard)
    """
    if grade <= 3:
        return "easy"
    if diff == "hard":
        return "medium"
    return diff


# ---------------- Mastery helpers ----------------
def mastery_key(subject: str, topic: str) -> str:
    return f"{subject}:{topic}"


def get_mastery(user_id: int, subject: str, topic: str) -> float:
    db = get_db()
    row = db.execute(
        "SELECT mastery FROM mastery WHERE user_id=? AND key=?",
        (user_id, mastery_key(subject, topic))
    ).fetchone()
    return float(row["mastery"]) if row else 0.30


def set_mastery(user_id: int, subject: str, topic: str, value: float) -> None:
    value = max(0.0, min(1.0, float(value)))
    db = get_db()
    db.execute(
        "INSERT INTO mastery(user_id, key, mastery) VALUES(?,?,?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET mastery=excluded.mastery",
        (user_id, mastery_key(subject, topic), value)
    )
    db.commit()


def update_mastery(old_m: float, performance: float, difficulty: str) -> float:
    alpha = {"easy": 0.08, "medium": 0.10, "hard": 0.12}[difficulty]
    return old_m + alpha * (performance - old_m)


def growth_level(m: float) -> str:
    if m < 0.40:
        return "🌱 Growing"
    elif m < 0.70:
        return "🌟 Getting Strong"
    return "🦁 Super Strong"


# ---------------- Streak + XP ----------------
def add_xp(user_id: int, correct: int, total: int) -> None:
    xp_gain = correct * 10 + (total - correct) * 5
    db = get_db()
    db.execute("UPDATE users SET xp = xp + ? WHERE id=?", (xp_gain, user_id))
    db.commit()


def update_streak(user_id: int) -> None:
    today = date.today().isoformat()
    db = get_db()
    row = db.execute("SELECT last_practice_date, streak_count FROM streaks WHERE user_id=?", (user_id,)).fetchone()

    if not row:
        db.execute("INSERT INTO streaks(user_id, last_practice_date, streak_count) VALUES(?,?,?)", (user_id, today, 1))
        db.commit()
        return

    last = row["last_practice_date"]
    count = int(row["streak_count"])

    if last == today:
        return

    try:
        last_dt = date.fromisoformat(last)
        today_dt = date.fromisoformat(today)
        count = count + 1 if (today_dt - last_dt).days == 1 else 1
    except Exception:
        count = 1

    db.execute("UPDATE streaks SET last_practice_date=?, streak_count=? WHERE user_id=?", (today, count, user_id))
    db.commit()


def get_streak(user_id: int) -> int:
    db = get_db()
    row = db.execute("SELECT streak_count FROM streaks WHERE user_id=?", (user_id,)).fetchone()
    return int(row["streak_count"]) if row else 0


# ---------------- Badges ----------------
BADGE_DEFS = {
    "first_quiz": ("🎯", "First Quiz", "Completed your first quiz!"),
    "streak_3": ("🔥", "3-Day Streak", "Practiced 3 days in a row!"),
    "streak_7": ("⭐", "7-Day Streak", "One week of practice!"),
    "mastery_75": ("🦁", "Super Strong", "Reached 75% mastery in a topic!"),
    "xp_100": ("🌟", "Rising Star", "Earned 100 XP!"),
}


def award_badge(user_id: int, badge_id: str) -> bool:
    """Award badge if not already earned. Returns True if newly awarded."""
    db = get_db()
    row = db.execute("SELECT 1 FROM badges WHERE user_id=? AND badge_id=?", (user_id, badge_id)).fetchone()
    if row:
        return False
    db.execute("INSERT INTO badges(user_id, badge_id, earned_at) VALUES(?,?,?)",
               (user_id, badge_id, datetime.utcnow().isoformat()))
    db.commit()
    return True


def check_and_award_badges(user_id: int, context: Dict[str, Any]) -> None:
    """Check conditions and award new badges. context has xp, streak, new_mastery, first_attempt, etc."""
    streak = get_streak(user_id)
    db = get_db()
    user = db.execute("SELECT xp FROM users WHERE id=?", (user_id,)).fetchone()
    xp = int(user["xp"]) if user else 0
    if context.get("first_attempt"):
        award_badge(user_id, "first_quiz")
    if streak >= 3:
        award_badge(user_id, "streak_3")
    if streak >= 7:
        award_badge(user_id, "streak_7")
    if context.get("new_mastery") and context["new_mastery"] >= 0.75:
        award_badge(user_id, "mastery_75")
    if xp >= 100:
        award_badge(user_id, "xp_100")


def get_user_badges(user_id: int) -> List[Dict]:
    db = get_db()
    rows = db.execute("SELECT badge_id, earned_at FROM badges WHERE user_id=?", (user_id,)).fetchall()
    result = []
    for r in rows:
        bid = r["badge_id"]
        defn = BADGE_DEFS.get(bid, ("?", "Badge", ""))
        result.append({"id": bid, "emoji": defn[0], "name": defn[1], "desc": defn[2]})
    return result


# ---------------- Cross-subject recommendation ----------------
def recommend_subject(user_id: int):
    subject_scores = {}
    for s in SUBJECTS:
        total = 0.0
        count = 0
        for t in TOPICS[s]:
            total += get_mastery(user_id, s, t)
            count += 1
        subject_scores[s] = total / count if count else 0.0

    weakest = min(subject_scores, key=subject_scores.get)
    strongest = max(subject_scores, key=subject_scores.get)
    return weakest, strongest, subject_scores


# ---------------- Weakness, Risk, Confidence, ML Recommendation ----------------
def get_topic_weakness_labels(user_id: int) -> Dict[str, str]:
    """Returns {subject:topic -> 'Weak'|'Moderate'|'Strong'}."""
    db = get_db()
    result = {}
    for s in SUBJECTS:
        for t in TOPICS[s]:
            m = get_mastery(user_id, s, t)
            rows = db.execute(
                "SELECT correct_q, total_q FROM attempts WHERE user_id=? AND subject=? AND topic=? ORDER BY id DESC LIMIT 5",
                (user_id, s, t)
            ).fetchall()
            recent_failures = sum(1 for r in rows if r["total_q"] and int(r["correct_q"]) / int(r["total_q"]) < 0.5)
            avg_acc = (sum(int(r["correct_q"]) / max(1, int(r["total_q"])) for r in rows) / len(rows)) if rows else 0.5
            result[f"{s}:{t}"] = classify_weakness(m, recent_failures, avg_acc)
    return result


def get_student_risk_score(user_id: int) -> Tuple[float, str]:
    """Returns (risk_score 0-1, level 'low'|'medium'|'high')."""
    db = get_db()
    today = date.today()
    last = db.execute(
        "SELECT created_at FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    days_since = 999
    if last and last["created_at"]:
        try:
            last_dt = datetime.fromisoformat(last["created_at"][:10]).date()
            days_since = (today - last_dt).days
        except Exception:
            pass
    recent = db.execute(
        "SELECT correct_q, total_q FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    failures = sum(1 for r in recent if r["total_q"] and int(r["correct_q"]) / int(r["total_q"]) < 0.5)
    avg_acc = (sum(int(r["correct_q"]) / max(1, int(r["total_q"])) for r in recent) / len(recent)) if recent else 0.5
    low_mastery = sum(1 for _ in db.execute(
        "SELECT 1 FROM mastery WHERE user_id=? AND mastery < 0.45",
        (user_id,)
    ).fetchall())
    risk = compute_risk_score(days_since, failures, avg_acc, low_mastery)
    level = "high" if risk >= 0.5 else ("medium" if risk >= 0.25 else "low")
    return risk, level


def get_confidence_status(user_id: int) -> str:
    """Returns 'High'|'Medium'|'Low'."""
    streak = get_streak(user_id)
    db = get_db()
    rows = db.execute(
        "SELECT correct_q, total_q, (ended_at - started_at) as sec FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    if not rows:
        return "Medium"
    success_rate = sum(int(r["correct_q"]) / max(1, int(r["total_q"])) for r in rows) / len(rows)
    avg_sec = sum(float(r["sec"] or 60) for r in rows) / len(rows)
    avg_speed_ok = 15 <= avg_sec <= 120
    return estimate_confidence(streak, success_rate, avg_speed_ok)


def get_ml_recommended_lesson(user_id: int) -> Tuple[str, str, str]:
    """Returns (subject, topic, reason) for next best lesson."""
    topic_masteries = []
    weak_topics = []
    for s in SUBJECTS:
        for t in TOPICS[s]:
            m = get_mastery(user_id, s, t)
            topic_masteries.append((s, t, m))
            if m < 0.45:
                weak_topics.append((s, t))
    weak_topics.sort(key=lambda x: get_mastery(user_id, x[0], x[1]))
    return recommend_next_lesson(topic_masteries, weak_topics)


# ---------------- Adaptive difficulty (rule-based + optional ML) ----------------
def get_last_attempt_stats(user_id: int, subject: str) -> Tuple[float, float, int]:
    """Returns (accuracy, seconds, hints_used) from most recent attempt, or defaults."""
    db = get_db()
    row = db.execute(
        "SELECT correct_q, total_q, (ended_at - started_at) as sec, hints_used FROM attempts "
        "WHERE user_id=? AND subject=? ORDER BY id DESC LIMIT 1",
        (user_id, subject)
    ).fetchone()
    if row and row["total_q"]:
        acc = int(row["correct_q"]) / max(1, int(row["total_q"]))
        sec = float(row["sec"] or 30)
        hints = int(row["hints_used"] or 0)
        return acc, sec, hints
    return 0.5, 30.0, 0


def choose_difficulty(m: float, user_id: int = 0, subject: str = "", grade: int = 1) -> str:
    """Rule-based difficulty; if ML model exists and predicts low success, step down."""
    rule_diff = "easy" if m < 0.45 else ("medium" if m < 0.70 else "hard")
    model = load_model()
    if model and user_id and subject:
        acc, sec, hints = get_last_attempt_stats(user_id, subject)
        p = predict_success_prob(model, grade, _subject_canonical(subject), rule_diff, acc, sec, hints)
        if p < 0.45 and rule_diff != "easy":
            idx = DIFFICULTIES.index(rule_diff)
            rule_diff = DIFFICULTIES[max(0, idx - 1)]
    return rule_diff


def choose_next_practice(accuracy: float, current_diff: str) -> Tuple[str, int, str]:
    if accuracy < 0.50:
        next_diff = DIFFICULTIES[max(0, DIFFICULTIES.index(current_diff) - 1)]
        return next_diff, 3, "💛 No worries. Let’s try a smaller practice."
    if accuracy >= 0.80:
        next_diff = DIFFICULTIES[min(len(DIFFICULTIES) - 1, DIFFICULTIES.index(current_diff) + 1)]
        return next_diff, 5, "🌟 Great job! Let’s try a little challenge."
    return current_diff, 4, "🌿 Good work. Let’s keep going step by step."


# ---------------- JSON Question Banks (grade, topic, difficulty tags) ----------------
_SCIENCE_JSON: List[Dict] = []
_ENGLISH_JSON: Dict = {}

def _load_question_banks():
    global _SCIENCE_JSON, _ENGLISH_JSON
    if _SCIENCE_JSON:
        return
    try:
        with open(os.path.join(DATA_DIR, "science_questions.json"), encoding="utf-8") as f:
            _SCIENCE_JSON = json.load(f)
    except Exception:
        pass
    try:
        with open(os.path.join(DATA_DIR, "english_bank.json"), encoding="utf-8") as f:
            _ENGLISH_JSON = json.load(f)
    except Exception:
        pass


def _q_from_dict(d: Dict) -> Q:
    """Build Q from JSON question dict (science or english format)."""
    q_text = d.get("q") or f"Choose the meaning of: {d.get('word', '')}"
    opts = d.get("options", [])
    ans = d.get("answer", "")
    hint = d.get("hint", "Think carefully.")
    if "text" in d:
        q_text = f"Read: \"{d['text']}\"\n\n{d.get('q', '')}"
    return Q(q_text, opts, ans, hint)


def _questions_from_admin_db(subject: str, topic: str, difficulty: str) -> Optional[Q]:
    """Load from admin_questions table. subject uses canonical form."""
    db = get_db()
    for subj in [subject, "Mathematics" if subject.lower() in ("math", "mathematics") else subject]:
        rows = db.execute(
            "SELECT q_text, options_json, answer, hint FROM admin_questions WHERE subject=? AND topic=? AND difficulty=? AND disabled=0",
            (subj, topic, difficulty)
        ).fetchall()
        if rows:
            r = random.choice(rows)
            try:
                opts = json.loads(r["options_json"] or "[]")
            except Exception:
                opts = [r["answer"]]
            return Q(r["q_text"], opts, r["answer"], r["hint"] or "Think carefully.")
    return None


def _science_from_json(topic: str, difficulty: str) -> Optional[Q]:
    _load_question_banks()
    pool = [x for x in _SCIENCE_JSON if x.get("topic") == topic and x.get("difficulty") == difficulty]
    if pool:
        d = random.choice(pool)
        return _q_from_dict(d)
    return None


def _english_vocab_from_json(difficulty: str) -> Optional[Q]:
    _load_question_banks()
    vb = _ENGLISH_JSON.get("vocabulary", {}).get(difficulty, [])
    if vb:
        d = random.choice(vb)
        return Q(f"Choose the meaning of: {d.get('word', '')}", d.get("options", []), d.get("answer", ""), d.get("hint", ""))
    return None


def _english_grammar_from_json(difficulty: str) -> Optional[Q]:
    _load_question_banks()
    gr = _ENGLISH_JSON.get("grammar", {}).get(difficulty, [])
    if gr:
        d = random.choice(gr)
        return _q_from_dict(d)
    return None


def _english_reading_from_json() -> Optional[Q]:
    _load_question_banks()
    passages = _ENGLISH_JSON.get("reading", {}).get("passages", [])
    if passages:
        d = random.choice(passages)
        return _q_from_dict(d)
    return None


# # QUESTION ENGINE (Grade-aware + lots of questions offline)

@dataclass
class Q:
    q: str
    options: List[str]
    answer: str
    hint: str


def _shuffle4(correct: str, wrongs: List[str]) -> List[str]:
    opts = [correct] + wrongs[:]
    random.shuffle(opts)
    return opts


# ---------------- MATH (already generator, we keep & improve) ----------------
def gen_math_question(topic: str, difficulty: str, grade: int) -> Q:
    # extra grade shaping: grade 1 uses smaller ranges even inside easy
    if topic == "addition":
        if grade <= 1:
            lo, hi = 0, 5
        elif grade == 2:
            lo, hi = 0, 9
        elif grade == 3:
            lo, hi = (0, 15) if difficulty == "easy" else (5, 20)
        else:
            ranges = {"easy": (0, 30), "medium": (10, 99), "hard": (100, 999)}
            lo, hi = ranges[difficulty]
        a, b = random.randint(lo, hi), random.randint(lo, hi)
        ans = a + b
        opts = _shuffle4(str(ans), [str(ans + 1), str(max(0, ans - 1)), str(ans + 2)])
        return Q(f"What is {a} + {b} ?", opts, str(ans), "Add slowly, one step at a time.")

    if topic == "subtraction":
        if grade <= 1:
            lo, hi = 0, 5
        elif grade == 2:
            lo, hi = 0, 9
        elif grade == 3:
            lo, hi = (0, 15) if difficulty == "easy" else (5, 20)
        else:
            ranges = {"easy": (0, 30), "medium": (10, 99), "hard": (100, 999)}
            lo, hi = ranges[difficulty]
        a, b = random.randint(lo, hi), random.randint(lo, hi)
        if b > a:
            a, b = b, a
        ans = a - b
        opts = _shuffle4(str(ans), [str(ans + 1), str(max(0, ans - 1)), str(ans + 2)])
        return Q(f"What is {a} - {b} ?", opts, str(ans), "Count backwards calmly.")

    # FRACTIONS: grade 1-2 should not get fractions
    if grade <= 2:
        # fallback to easy addition if someone reaches fractions early
        return gen_math_question("addition", "easy", grade)

    pools = {
        "easy": [(1, 2, 1, 4), (1, 3, 1, 6), (3, 4, 2, 4), (2, 3, 1, 3)],
        "medium": [(2, 5, 3, 5), (3, 8, 1, 8), (5, 6, 2, 3), (3, 10, 7, 10)],
        "hard": [(7, 12, 5, 8), (9, 10, 4, 5), (11, 16, 3, 4), (5, 12, 7, 18)],
    }
    n1, d1, n2, d2 = random.choice(pools[difficulty])
    v1, v2 = n1 / d1, n2 / d2
    bigger = f"{n1}/{d1}" if v1 > v2 else f"{n2}/{d2}"
    if abs(v1 - v2) < 1e-9:
        bigger = "They are equal"
    opts = _shuffle4(bigger, [f"{n1}/{d1}", f"{n2}/{d2}", "They are equal"])
    return Q("Which is bigger?", opts, bigger, "Think of halves and quarters.")


# ---------------- ENGLISH (grade-aware generators) ----------------

# # Vocabulary seeds per grade (small list => infinite combos via options)
VOCAB_SEEDS = {
    1: [("cat", "a small animal"), ("sun", "the star in the sky"), ("run", "move fast"), ("big", "not small"),
        ("red", "a color"), ("book", "you read it"), ("milk", "a drink"), ("tree", "a tall plant")],
    2: [("happy", "feeling good"), ("quiet", "not loud"), ("quick", "fast"), ("family", "mother and father"),
        ("school", "a place to learn"), ("summer", "hot season"), ("circle", "a round shape")],
    3: [("careful", "do it slowly and safely"), ("between", "in the middle"), ("because", "a reason"),
        ("weather", "sunny or rainy"), ("discover", "find something new"), ("practice", "do again to improve")],
    4: [("predict", "guess what may happen"), ("improve", "make better"), ("compare", "see what is same/different"),
        ("energy", "power to do work"), ("habitat", "home of an animal"), ("protect", "keep safe")],
    5: [("responsible", "you can be trusted"), ("experiment", "a test to learn"), ("conclusion", "final result"),
        ("character", "person in a story"), ("solution", "an answer to a problem"), ("increase", "become more")],
}

# # Grammar templates per grade
GRAMMAR_TEMPLATES = {
    1: [
        ("Choose the correct word: I ___ a ball.", ["have", "has", "had"], "have", "Say it aloud: I have..."),
        ("Choose the correct word: She ___ happy.", ["is", "are", "am"], "is", "She is..."),
    ],
    2: [
        ("Choose the correct word: They ___ playing.", ["are", "is", "am"], "are", "They are..."),
        ("Choose the correct word: He ___ to school.", ["goes", "go", "gone"], "goes", "He goes..."),
    ],
    3: [
        ("Pick the correct sentence:", ["I goed home.", "I went home.", "I go home yesterday."], "I went home.", "Past tense: went."),
        ("Choose the correct word: We ___ a new book.", ["read", "reads", "reading"], "read", "We read..."),
    ],
    4: [
        ("Choose the correct word: If it rains, we ___ inside.", ["will stay", "stayed", "stay"], "will stay", "Future: will..."),
        ("Choose the correct word: She is ___ than me.", ["taller", "tallest", "tall"], "taller", "Comparative: taller."),
    ],
    5: [
        ("Choose the correct word: The cake was ___ by us.", ["eaten", "ate", "eating"], "eaten", "Passive voice uses past participle."),
        ("Pick the correct sentence:", ["I have went.", "I have gone.", "I has gone."], "I have gone.", "Present perfect: have/has + past participle."),
    ],
}

# # Reading passages per grade (small, safe, offline)
READING_PASSAGES = {
    1: [
        {"text": "Mia has a red ball. She plays with it.", "q": "What color is the ball?", "options": ["Red", "Blue", "Green"], "answer": "Red", "hint": "Look for the color word."},
    ],
    2: [
        {"text": "Ben goes to school. He likes to read books.", "q": "What does Ben like?", "options": ["To read books", "To swim", "To sleep"], "answer": "To read books", "hint": "Find what Ben likes."},
    ],
    3: [
        {"text": "It rained today, so the ground is wet. Sara used an umbrella.", "q": "Why did Sara use an umbrella?", "options": ["Because it rained", "Because it was hot", "Because it was windy"], "answer": "Because it rained", "hint": "Rain makes umbrellas useful."},
    ],
    4: [
        {"text": "The plant grew faster because it got sunlight and water every day.", "q": "What helped the plant grow faster?", "options": ["Sunlight and water", "Noise", "Cold wind"], "answer": "Sunlight and water", "hint": "Plants need light and water."},
    ],
    5: [
        {"text": "Nila tested two paper boats. The one with a wider base stayed afloat longer.", "q": "Which boat stayed afloat longer?", "options": ["The wider base boat", "The smaller base boat", "Both the same"], "answer": "The wider base boat", "hint": "Wider base = more stable."},
    ],
}


def english_vocabulary_q(grade: int, difficulty: str) -> Q:
    q_admin = _questions_from_admin_db("English", "vocabulary", difficulty)
    if q_admin:
        return q_admin
    q_json = _english_vocab_from_json(difficulty)
    if q_json:
        return q_json
    # Fallback to in-code seeds
    seeds = VOCAB_SEEDS.get(grade, VOCAB_SEEDS[1])
    word, meaning = random.choice(seeds)

    if difficulty == "easy":
        wrongs = random.sample([m for (_w, m) in seeds if m != meaning] + ["a fruit", "a toy"], k=3)
    elif difficulty == "medium":
        wrongs = random.sample([m for (_w, m) in seeds if m != meaning] + ["a place", "a feeling", "a color"], k=3)
    else:
        # hard (will be capped for grade 4-5 only)
        wrongs = random.sample([m for (_w, m) in seeds if m != meaning] + ["a reason", "a rule", "an idea"], k=3)

    opts = _shuffle4(meaning, wrongs)
    return Q(f"Choose the meaning of: {word}", opts, meaning, "Think about what the word means.")


def english_grammar_q(grade: int, difficulty: str) -> Q:
    q_admin = _questions_from_admin_db("English", "grammar", difficulty)
    if q_admin:
        return q_admin
    q_json = _english_grammar_from_json(difficulty)
    if q_json:
        return q_json
    templates = GRAMMAR_TEMPLATES.get(grade, GRAMMAR_TEMPLATES[2])
    q, opts, ans, hint = random.choice(templates)

    # for medium/hard, we can sometimes switch to trickier template from higher grade
    if difficulty in ("medium", "hard") and grade < 5 and random.random() < 0.25:
        templates2 = GRAMMAR_TEMPLATES.get(min(5, grade + 1), templates)
        q, opts, ans, hint = random.choice(templates2)

    return Q(q, opts, ans, hint)


def english_reading_q(grade: int, difficulty: str) -> Q:
    q_admin = _questions_from_admin_db("English", "reading", difficulty)
    if q_admin:
        return q_admin
    q_json = _english_reading_from_json()
    if q_json:
        return q_json
    pool = READING_PASSAGES.get(grade, READING_PASSAGES[1])
    p = random.choice(pool)
    return Q(f"Read: “{p['text']}”\n\n{p['q']}", p["options"], p["answer"], p["hint"])


# ---------------- SCIENCE (grade-aware generators) ----------------

SCIENCE_FACTS = {
    "plants": {
        1: [
            ("Plants need ___ to grow.", ["water", "candy", "toys"], "water", "Plants drink water."),
            ("Leaves are usually ___.", ["green", "blue", "black"], "green", "Most leaves are green."),
        ],
        2: [
            ("Roots help the plant to ___.", ["take water", "fly", "talk"], "take water", "Roots drink water."),
            ("Plants make food using ___.", ["sunlight", "shoes", "music"], "sunlight", "Sunlight helps plants."),
        ],
        3: [
            ("Which part makes seeds?", ["flower", "root", "stem"], "flower", "Flowers make seeds."),
            ("Stems help the plant to ___.", ["stand up", "swim", "sleep"], "stand up", "Stems hold plants."),
        ],
        4: [
            ("Photosynthesis uses sunlight and ___.", ["water", "sand", "plastic"], "water", "Water + light helps."),
            ("Plants release ___.", ["oxygen", "smoke", "oil"], "oxygen", "We breathe oxygen."),
        ],
        5: [
            ("A cactus is adapted to ___.", ["dry places", "icy oceans", "space"], "dry places", "Cactus saves water."),
            ("Which helps reduce water loss?", ["waxy leaves", "thin paper", "open holes"], "waxy leaves", "Waxy layers help."),
        ],
    },

    "animals": {
        1: [
            ("A fish lives in ___.", ["water", "sand", "trees"], "water", "Fish swim."),
            ("Birds have ___.", ["wings", "wheels", "roots"], "wings", "Birds fly with wings."),
        ],
        2: [
            ("A baby cat is called a ___.", ["kitten", "puppy", "calf"], "kitten", "Cats have kittens."),
            ("Which animal gives milk?", ["cow", "lizard", "snake"], "cow", "Cows are mammals."),
        ],
        3: [
            ("Animals that eat plants are ___.", ["herbivores", "carnivores", "rocks"], "herbivores", "Herb = plant."),
            ("Which has a habitat in water?", ["frog", "camel", "lion"], "frog", "Frogs like water."),
        ],
        4: [
            ("A food chain shows ___.", ["who eats who", "how to sleep", "how to paint"], "who eats who", "Energy moves by eating."),
            ("Which is a predator?", ["lion", "grass", "mushroom"], "lion", "Predators hunt."),
        ],
        5: [
            ("Camouflage helps animals ___.", ["hide", "sing", "glow"], "hide", "Camouflage = blend in."),
            ("Migration means animals ___.", ["move seasonally", "stop eating", "change color"], "move seasonally", "Some birds migrate."),
        ],
    },

    "matter_forces": {
        1: [
            ("Ice is a ___.", ["solid", "liquid", "gas"], "solid", "Ice is hard."),
            ("A push is a kind of ___.", ["force", "food", "color"], "force", "Push or pull."),
        ],
        2: [
            ("Water is a ___.", ["liquid", "solid", "gas"], "liquid", "Water flows."),
            ("Wind is made of ___.", ["air", "rocks", "paper"], "air", "Wind is moving air."),
        ],
        3: [
            ("Heating water can make ___.", ["steam", "sand", "wood"], "steam", "Steam is gas."),
            ("Gravity pulls things ___.", ["down", "up", "sideways"], "down", "Gravity goes down."),
        ],
        4: [
            ("Friction happens when two surfaces ___.", ["rub", "sleep", "shine"], "rub", "Rubbing makes friction."),
            ("A magnet attracts ___.", ["iron", "glass", "paper"], "iron", "Magnets like iron."),
        ],
        5: [
            ("Force can change an object’s ___.", ["motion", "name", "age"], "motion", "Motion = movement."),
            ("Which is an example of friction?", ["rubbing hands", "reading", "sleeping"], "rubbing hands", "Rubbing creates heat."),
        ],
    },
}


def science_q(topic: str, grade: int, difficulty: str) -> Q:
    q_admin = _questions_from_admin_db("Science", topic, difficulty)
    if q_admin:
        return q_admin
    q_json = _science_from_json(topic, difficulty)
    if q_json:
        return q_json
    # Fallback to in-code pool
    pool = SCIENCE_FACTS.get(topic, {}).get(grade)
    if not pool:
        pool = SCIENCE_FACTS.get(topic, {}).get(1, [])
    q, opts, ans, hint = random.choice(pool)

    # For medium/hard (grades 4-5), occasionally pull from next grade to increase challenge
    if difficulty in ("medium", "hard") and grade < 5 and random.random() < 0.25:
        pool2 = SCIENCE_FACTS.get(topic, {}).get(min(5, grade + 1), pool)
        q, opts, ans, hint = random.choice(pool2)

    return Q(q, opts, ans, hint)


# ---------------- Main question router ----------------
def _subject_canonical(s: str) -> str:
    """Normalize subject name for routing (Mathematics/Math -> math)."""
    low = s.lower() if s else ""
    if low in ("math", "mathematics"):
        return "math"
    if low == "english":
        return "english"
    if low == "science":
        return "science"
    return low


def get_question(subject: str, topic: str, difficulty: str, grade: int) -> Q:
    s = _subject_canonical(subject)
    if s == "math":
        return gen_math_question(topic, difficulty, grade)
    if s == "english":
        if topic == "vocabulary":
            return english_vocabulary_q(grade, difficulty)
        if topic == "grammar":
            return english_grammar_q(grade, difficulty)
        return english_reading_q(grade, difficulty)
    # science
    return science_q(topic, grade, difficulty)


# ---------------- Routes ----------------
@app.get("/")
def index():
    return redirect(url_for("stages") if session.get("user_id") else url_for("login"))


@app.get("/signup")
def signup():
    cfg = get_firebase_web_config()
    if not cfg:
        flash("Firebase is not configured. Add firebase-web-config.json with your project credentials.")
    return render_template("signup.html", firebase_config=cfg)


@app.get("/choose-grade")
def choose_grade():
    guard = require_login()
    if guard:
        return guard
    if not session.get("subject"):
        return redirect(url_for("choose_subject"))
    if not session.get("needs_grade_pick"):
        return redirect(url_for("stages"))
    return render_template("choose_grade.html")


@app.post("/choose-grade")
def choose_grade_post():
    guard = require_login()
    if guard:
        return guard

    grade = int(request.form.get("grade", "1"))
    grade = max(1, min(5, grade))

    db = get_db()
    db.execute("UPDATE users SET grade=? WHERE id=?", (grade, session["user_id"]))
    db.commit()

    session.pop("needs_grade_pick", None)
    return redirect(url_for("stages"))


@app.get("/choose-subject")
def choose_subject():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "student":
        return redirect(url_for("stages"))
    return render_template("choose_subject.html", subjects=STAGE_SUBJECTS)


@app.post("/choose-subject")
def choose_subject_post():
    guard = require_login()
    if guard:
        return guard
    subject = request.form.get("subject", "").strip()
    if subject not in STAGE_SUBJECTS:
        subject = STAGE_SUBJECTS[0]
    session["subject"] = subject
    flash("Let's start learning " + subject + " 🌿")
    if session.get("needs_grade_pick"):
        return redirect(url_for("choose_grade"))
    return redirect(url_for("stages"))


@app.get("/login")
def login():
    cfg = get_firebase_web_config()
    if not cfg:
        flash("Firebase is not configured. Add firebase-web-config.json with your project credentials.")
    return render_template("login.html", firebase_config=cfg)


@app.post("/auth/firebase")
def auth_firebase():
    """Verify Firebase ID token and create session. Accepts JSON {token, nickname?, role?}."""
    def err(msg, code=500):
        return jsonify({"error": msg}), code

    if not FIREBASE_ENABLED:
        return err("Firebase not configured", 503)

    data = request.get_json(silent=True) or {}
    id_token = data.get("token") or request.form.get("token")
    if not id_token:
        return jsonify({"error": "Missing token"}), 400

    try:
        decoded = firebase_auth.verify_id_token(id_token)
        firebase_uid = decoded["uid"]
        email = (decoded.get("email") or "").strip().lower()
    except Exception:
        return jsonify({"error": "Invalid token"}), 401

    db = get_db()
    row = db.execute("SELECT id, grade, role FROM users WHERE firebase_uid=?", (firebase_uid,)).fetchone()

    if row:
        user_id = int(row["id"])
    else:
        nickname = (data.get("nickname") or "").strip() or (email.split("@")[0] if email else "Learner")
        role = data.get("role", "student")
        if role not in ("student", "teacher", "parent"):
            role = "student"
        try:
            db.execute(
                "INSERT INTO users(nickname, grade, email, password_hash, xp, created_at, role, firebase_uid) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (nickname, 1, email or f"{firebase_uid}@firebase.user", "", 0, datetime.utcnow().isoformat(), role, firebase_uid)
            )
            db.commit()
        except sqlite3.IntegrityError:
            row = db.execute("SELECT id, grade, role FROM users WHERE email=?", (email or f"{firebase_uid}@firebase.user",)).fetchone()
            if row:
                db.execute("UPDATE users SET firebase_uid=? WHERE id=?", (firebase_uid, row["id"]))
                db.commit()
                user_id = int(row["id"])
            else:
                return jsonify({"error": "Could not create user"}), 500
        else:
            user_id = db.execute("SELECT id FROM users WHERE firebase_uid=?", (firebase_uid,)).fetchone()["id"]

    row = db.execute("SELECT grade, role FROM users WHERE id=?", (user_id,)).fetchone()
    session["user_id"] = int(user_id)
    session["role"] = (row["role"] if row["role"] else "student")

    if session["role"] == "teacher":
        return jsonify({"redirect": url_for("teacher_dashboard")})
    if session["role"] == "parent":
        return jsonify({"redirect": url_for("parent_dashboard")})
    if not row["grade"]:
        session["needs_grade_pick"] = True
        return jsonify({"redirect": url_for("ask_grade")})
    # Students: show grade screen first, then subject
    if session["role"] == "student":
        return jsonify({"redirect": url_for("ask_grade")})
    return jsonify({"redirect": url_for("stages")})


@app.get("/ask-grade")
def ask_grade():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "student":
        return redirect(url_for("stages"))
    if not session.get("needs_grade_pick"):
        return redirect(url_for("choose_subject") if not session.get("subject") else url_for("stages"))
    return render_template("ask_grade.html")


@app.post("/ask-grade")
def ask_grade_post():
    guard = require_login()
    if guard:
        return guard
    grade_str = (request.form.get("grade") or "").strip()
    if grade_str not in ("1", "2", "3", "4", "5"):
        flash("Please select a grade.")
        return redirect(url_for("ask_grade"))
    grade = int(grade_str)
    db = get_db()
    db.execute("UPDATE users SET grade=? WHERE id=?", (grade, session["user_id"]))
    db.commit()
    session.pop("needs_grade_pick", None)
    flash("Thanks! Let's get started.")
    return redirect(url_for("choose_subject"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/stages")
def stages():
    guard = require_login()
    if guard:
        return guard

    if session.get("role") == "teacher":
        return redirect(url_for("teacher_dashboard"))
    if session.get("role") == "parent":
        return redirect(url_for("parent_dashboard"))

    if not session.get("subject"):
        return redirect(url_for("choose_subject"))
    if session.get("needs_grade_pick"):
        return redirect(url_for("ask_grade"))

    user_id = session["user_id"]
    grade = get_user_grade(user_id)
    db = get_db()
    user = db.execute("SELECT nickname, grade, xp FROM users WHERE id=?", (user_id,)).fetchone()
    streak = get_streak(user_id)

    subject = session.get("subject", "Math")
    stage_items = []
    for s in STAGES:
        unlocked = is_stage_unlocked(user_id, subject, s)
        completed = get_stage_completed(user_id, subject, s)
        correct, total, pct = get_stage_best_score(user_id, subject, s)
        stage_items.append({
            "stage": s,
            "label": s.capitalize(),
            "unlocked": unlocked,
            "completed": completed,
            "correct": correct,
            "total": total,
            "pct": pct,
        })

    return render_template(
        "stages.html",
        user=user,
        streak=streak,
        subject=subject,
        stage_items=stage_items,
    )


@app.get("/stage/<stage_name>")
def stage_quiz(stage_name: str):
    guard = require_login()
    if guard:
        return guard
    if not session.get("subject"):
        return redirect(url_for("choose_subject"))
    if stage_name not in STAGES:
        return redirect(url_for("stages"))
    if session.get("role") != "student":
        return redirect(url_for("stages"))

    subject = session["subject"]
    user_id = session["user_id"]
    if not is_stage_unlocked(user_id, subject, stage_name):
        flash("Complete the previous stage first to unlock this one.")
        return redirect(url_for("stages"))

    grade = get_user_grade(user_id)
    n_questions = 5 if stage_name == "easy" else 10
    questions = get_stage_questions(grade, subject, stage_name, n=n_questions)
    if not questions:
        flash("No questions available for this stage yet.")
        return redirect(url_for("stages"))

    session["active_stage_quiz"] = {
        "subject": subject,
        "stage": stage_name,
        "grade": grade,
        "questions": questions,
        "started_at": datetime.utcnow().timestamp(),
    }
    return render_template("stage_quiz.html", stage=stage_name, subject=subject, questions=questions)


@app.post("/stage/submit")
def stage_submit():
    guard = require_login()
    if guard:
        return guard

    quiz = session.get("active_stage_quiz")
    if not quiz:
        return redirect(url_for("stages"))

    user_id = session["user_id"]
    stage = quiz["stage"]
    questions = quiz["questions"]
    correct = sum(1 for i, q in enumerate(questions) if request.form.get(f"q{i}", "") == q["answer"])
    total = len(questions)
    perfect = (correct == total)

    subject = quiz.get("subject", session.get("subject", "Math"))
    db = get_db()
    db.execute(
        "INSERT INTO attempts(user_id, subject, topic, difficulty, total_q, correct_q, started_at, ended_at, hints_used, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,0,?)",
        (user_id, subject, stage, stage, total, correct,
         quiz.get("started_at", datetime.utcnow().timestamp()),
         datetime.utcnow().timestamp(), datetime.utcnow().isoformat())
    )
    db.commit()
    add_xp(user_id, correct, total)
    update_streak(user_id)

    session.pop("active_stage_quiz", None)

    if perfect:
        set_stage_completed(user_id, subject, stage)
        if stage == "hard":
            return redirect(url_for("congrats"))
        flash("Perfect! You unlocked the next stage.")
        return redirect(url_for("stages"))

    return render_template(
        "stage_result.html",
        stage=stage,
        subject=subject,
        correct=correct,
        total=total,
        perfect=False,
    )


@app.get("/congrats")
def congrats():
    guard = require_login()
    if guard:
        return guard
    user_id = session["user_id"]
    subject = session.get("subject", "Math")
    if not get_stage_completed(user_id, subject, "hard"):
        return redirect(url_for("stages"))
    user = get_db().execute("SELECT nickname FROM users WHERE id=?", (user_id,)).fetchone()
    return render_template("congrats.html", user=user, subject=subject)


@app.get("/subjects")
def subjects_redirect():
    return redirect(url_for("stages"))


@app.get("/path/<subject>")
def path(subject: str):
    guard = require_login()
    if guard:
        return guard
    if subject not in SUBJECTS:
        return redirect(url_for("stages"))

    user_id = session["user_id"]
    items = []
    unlocked_all_prev = True

    for topic in TOPICS[subject]:
        m = get_mastery(user_id, subject, topic)
        unlocked = unlocked_all_prev
        if m < UNLOCK_MASTERY:
            unlocked_all_prev = False

        stars = 0
        if m >= 0.45: stars = 1
        if m >= 0.60: stars = 2
        if m >= 0.75: stars = 3

        items.append({
            "topic": topic,
            "mastery": m,
            "unlocked": unlocked,
            "stars": stars,
            "growth": growth_level(m),
        })

    return render_template("path.html", subject=subject, items=items)


@app.get("/lesson/<subject>/<topic>")
def lesson(subject: str, topic: str):
    guard = require_login()
    if guard:
        return guard
    if subject not in SUBJECTS or topic not in TOPICS.get(subject, []):
        return redirect(url_for("stages"))

    lesson_text = {
        "math:addition": "Addition means putting numbers together. Count slowly and calmly.",
        "math:subtraction": "Subtraction means taking away. Start and count backwards.",
        "math:fractions": "Fractions are parts of a whole. Like half a pizza!",
        "english:vocabulary": "Vocabulary helps you learn words with examples.",
        "english:grammar": "Grammar helps sentences make sense.",
        "english:reading": "Read slowly and find clue words in the story.",
        "science:plants": "Plants need water and sunlight. Roots drink water.",
        "science:animals": "Animals have special body parts to live and move.",
        "science:matter_forces": "Matter can be solid, liquid, or gas. Forces push or pull.",
    }.get(mastery_key(subject, topic), "Let’s learn something new today!")

    return render_template("lesson.html", subject=subject, topic=topic, lesson_text=lesson_text)


@app.get("/practice/<subject>/<topic>")
def practice(subject: str, topic: str):
    guard = require_login()
    if guard:
        return guard
    if subject not in SUBJECTS or topic not in TOPICS.get(subject, []):
        return redirect(url_for("stages"))

    user_id = session["user_id"]
    grade = get_user_grade(user_id)

    m = get_mastery(user_id, subject, topic)
    revision_mode = request.args.get("revision") == "1"
    if revision_mode:
        diff = "easy"
    else:
        diff = choose_difficulty(m, user_id, subject, grade)
    diff = cap_difficulty_by_grade(grade, diff)

    total_q = int(request.args.get("n", "4"))

    qs = []
    for _ in range(total_q):
        q = get_question(subject, topic, diff, grade)
        qs.append({"q": q.q, "options": q.options, "answer": q.answer, "hint": q.hint})

    session["active_quiz"] = {
        "subject": subject,
        "topic": topic,
        "difficulty": diff,
        "grade": grade,
        "questions": qs,
        "started_at": datetime.utcnow().timestamp()
    }

    return render_template("quiz.html", subject=subject, topic=topic, difficulty=diff, questions=qs)


@app.post("/submit")
def submit():
    guard = require_login()
    if guard:
        return guard

    quiz = session.get("active_quiz")
    if not quiz:
        return redirect(url_for("stages"))

    user_id = session["user_id"]
    subject = quiz["subject"]
    topic = quiz["topic"]
    difficulty = quiz["difficulty"]
    questions = quiz["questions"]
    started_at = float(quiz.get("started_at", datetime.utcnow().timestamp()))
    ended_at = datetime.utcnow().timestamp()

    correct = 0
    hints_used = 0
    for i, q in enumerate(questions):
        chosen = request.form.get(f"q{i}", "")
        if request.form.get(f"hint{i}", "") == "on":
            hints_used += 1
        if chosen == q["answer"]:
            correct += 1

    total = len(questions)
    accuracy = correct / max(1, total)

    db = get_db()
    db.execute(
        "INSERT INTO attempts(user_id, subject, topic, difficulty, total_q, correct_q, started_at, ended_at, hints_used, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, subject, topic, difficulty, total, correct, started_at, ended_at, hints_used, datetime.utcnow().isoformat())
    )
    db.commit()

    add_xp(user_id, correct, total)
    update_streak(user_id)

    first_attempt = (db.execute("SELECT COUNT(*) as c FROM attempts WHERE user_id=?", (user_id,)).fetchone()["c"] == 1)

    old_m = get_mastery(user_id, subject, topic)
    new_m = update_mastery(old_m, accuracy, difficulty)
    set_mastery(user_id, subject, topic, new_m)

    check_and_award_badges(user_id, {"first_attempt": first_attempt, "new_mastery": new_m})

    next_diff, next_n, kid_reason = choose_next_practice(accuracy, difficulty)

    grade = get_user_grade(user_id)
    next_diff = cap_difficulty_by_grade(grade, next_diff)

    if accuracy < 0.50:
        ai_explain = "We made it easier so you can feel comfortable."
    elif accuracy >= 0.80:
        ai_explain = "We made it a little harder because you did very well."
    else:
        ai_explain = "We kept the same level because you are improving steadily."

    weakest, strongest, subject_scores = recommend_subject(user_id)
    subject_note = f"Tip: practice {weakest} today because it needs the most help."

    unlocked = (new_m >= UNLOCK_MASTERY)
    next_topic = topic
    if unlocked:
        topics = TOPICS[subject]
        idx = topics.index(topic)
        if idx < len(topics) - 1:
            next_topic = topics[idx + 1]
    next_action = ("next_topic", next_topic) if (unlocked and next_topic != topic) else ("retry", topic)

    session.pop("active_quiz", None)

    level_text = growth_level(new_m)
    feeling_text = "🙂 Doing well" if accuracy >= 0.60 else "😊 Let’s practice gently"

    return render_template(
        "result.html",
        subject=subject,
        topic=topic,
        difficulty=difficulty,
        correct=correct,
        total=total,
        accuracy=accuracy,
        old_m=old_m,
        new_m=new_m,
        next_diff=next_diff,
        next_n=next_n,
        kid_reason=kid_reason,
        ai_explain=ai_explain,
        subject_note=subject_note,
        level_text=level_text,
        feeling_text=feeling_text,
        next_action=next_action,
        revision_suggested=(new_m < 0.45)
    )


@app.get("/dashboard")
def dashboard():
    guard = require_login()
    if guard:
        return guard

    user_id = session["user_id"]
    db = get_db()
    user = db.execute("SELECT nickname, grade, xp FROM users WHERE id=?", (user_id,)).fetchone()
    streak = get_streak(user_id)

    mastery_rows = db.execute("SELECT key, mastery FROM mastery WHERE user_id=?", (user_id,)).fetchall()
    mastery_map = {r["key"]: float(r["mastery"]) for r in mastery_rows}

    topics_view = []
    for s in SUBJECTS:
        for t in TOPICS[s]:
            m = mastery_map.get(mastery_key(s, t), 0.30)
            topics_view.append({
                "subject": s,
                "topic": t,
                "mastery": m,
                "growth": growth_level(m)
            })

    attempts = db.execute(
        "SELECT subject, topic, difficulty, total_q, correct_q, created_at "
        "FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()

    weakest, strongest, subject_scores = recommend_subject(user_id)
    badges = get_user_badges(user_id)
    weakness_labels = get_topic_weakness_labels(user_id)
    risk_score, risk_level = get_student_risk_score(user_id)
    confidence = get_confidence_status(user_id)
    rec_subj, rec_topic, rec_reason = get_ml_recommended_lesson(user_id)

    return render_template(
        "dashboard.html",
        user=user,
        streak=streak,
        topics=topics_view,
        attempts=attempts,
        weakest=weakest,
        strongest=strongest,
        subject_scores=subject_scores,
        badges=badges,
        weakness_labels=weakness_labels,
        risk_score=risk_score,
        risk_level=risk_level,
        confidence=confidence,
        ml_rec_subject=rec_subj,
        ml_rec_topic=rec_topic,
        ml_rec_reason=rec_reason
    )

@app.get("/teacher")
def teacher_dashboard():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("stages"))

    db = get_db()
    students_raw = db.execute(
        "SELECT u.id, u.nickname, u.grade, u.xp, u.email, "
        "(SELECT COUNT(*) FROM attempts a WHERE a.user_id=u.id) as attempt_count "
        "FROM users u WHERE u.role='student' ORDER BY u.xp DESC"
    ).fetchall()
    students = []
    risk_alerts = []
    for s in students_raw:
        risk, level = get_student_risk_score(s["id"])
        students.append(dict(s) | {"risk_score": risk, "risk_level": level})
        if level == "high":
            risk_alerts.append({"id": s["id"], "nickname": s["nickname"], "risk": risk})
    recent_attempts = db.execute(
        "SELECT a.*, u.nickname FROM attempts a JOIN users u ON u.id=a.user_id "
        "WHERE u.role='student' ORDER BY a.id DESC LIMIT 20"
    ).fetchall()
    common_mistakes = db.execute("""
        SELECT subject, topic, difficulty,
               COUNT(*) as fail_count,
               AVG(1.0 * correct_q / NULLIF(total_q, 0)) as avg_acc
        FROM attempts
        WHERE 1.0 * correct_q / NULLIF(total_q, 1) < 0.5
        GROUP BY subject, topic, difficulty
        ORDER BY fail_count DESC
        LIMIT 10
    """).fetchall()
    _load_question_banks()
    science_count = len(_SCIENCE_JSON)
    english_keys = list(_ENGLISH_JSON.keys()) if _ENGLISH_JSON else []
    return render_template(
        "teacher_dashboard.html",
        students=students,
        recent_attempts=recent_attempts,
        science_questions=science_count,
        english_topics=english_keys,
        risk_alerts=risk_alerts,
        common_mistakes=common_mistakes,
    )


@app.get("/parent")
def parent_dashboard():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "parent":
        return redirect(url_for("stages"))

    db = get_db()
    parent_id = session["user_id"]
    children = db.execute(
        "SELECT u.id, u.nickname, u.grade, u.xp FROM users u "
        "JOIN parent_child pc ON pc.child_id=u.id WHERE pc.parent_id=?",
        (parent_id,)
    ).fetchall()
    children_data = []
    for c in children:
        cid = c["id"]
        mastery = db.execute("SELECT key, mastery FROM mastery WHERE user_id=?", (cid,)).fetchall()
        attempts = db.execute(
            "SELECT subject, topic, correct_q, total_q, created_at FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (cid,)
        ).fetchall()
        risk, risk_level = get_student_risk_score(cid)
        confidence = get_confidence_status(cid)
        children_data.append({"child": c, "mastery": mastery, "attempts": attempts, "risk": risk, "risk_level": risk_level, "confidence": confidence})
    return render_template("parent_dashboard.html", children_data=children_data)


@app.post("/parent/link-child")
def parent_link_child():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "parent":
        return redirect(url_for("parent_dashboard"))

    email = request.form.get("child_email", "").strip().lower()
    if not email:
        flash("Please enter the child's email.")
        return redirect(url_for("parent_dashboard"))

    db = get_db()
    child = db.execute("SELECT id FROM users WHERE email=? AND role='student'", (email,)).fetchone()
    if not child:
        flash("No student account found with that email.")
        return redirect(url_for("parent_dashboard"))

    parent_id = session["user_id"]
    child_id = child["id"]
    try:
        db.execute("INSERT INTO parent_child(parent_id, child_id) VALUES(?,?)", (parent_id, child_id))
        db.commit()
        flash("Child linked successfully!")
    except sqlite3.IntegrityError:
        flash("Child is already linked.")
    return redirect(url_for("parent_dashboard"))


@app.get("/analytics/student/<int:user_id>")
def analytics_student(user_id: int):
    """JSON for Chart.js: scores over time, time spent, completion trends."""
    guard = require_login()
    if guard:
        return guard
    if session.get("role") not in ("teacher", "parent") and session.get("user_id") != user_id:
        return {"error": "Forbidden"}, 403
    db = get_db()
    rows = db.execute(
        "SELECT correct_q, total_q, (ended_at - started_at) as sec, created_at FROM attempts WHERE user_id=? ORDER BY id",
        (user_id,)
    ).fetchall()
    labels = []
    scores = []
    times = []
    for r in rows:
        labels.append((r["created_at"] or "")[:10])
        scores.append(round(100 * int(r["correct_q"]) / max(1, int(r["total_q"])), 0))
        times.append(round(float(r["sec"] or 0), 0))
    return {"labels": labels, "scores": scores, "times": times}


@app.get("/admin/questions")
def admin_questions():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("stages"))
    db = get_db()
    questions = db.execute(
        "SELECT id, subject, topic, difficulty, grade_min, grade_max, q_text, options_json, answer, hint, disabled FROM admin_questions ORDER BY id"
    ).fetchall()
    return render_template("admin_questions.html", questions=questions)


@app.get("/admin/question/add")
def admin_question_add():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("stages"))
    return render_template("admin_question_form.html", question=None)


@app.post("/admin/question/add")
def admin_question_add_post():
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("admin_questions"))
    subj = request.form.get("subject", "").strip()
    topic = request.form.get("topic", "").strip()
    diff = request.form.get("difficulty", "easy")
    q_text = request.form.get("q_text", "").strip()
    opts = request.form.get("options", "[]")
    answer = request.form.get("answer", "").strip()
    hint = request.form.get("hint", "").strip()
    if not subj or not topic or not q_text or not answer:
        flash("Missing required fields.")
        return redirect(url_for("admin_question_add"))
    db = get_db()
    db.execute(
        "INSERT INTO admin_questions(subject, topic, difficulty, grade_min, grade_max, q_text, options_json, answer, hint, disabled, created_at) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
        (subj, topic, diff, 1, 5, q_text, opts, answer, hint, datetime.utcnow().isoformat())
    )
    db.commit()
    flash("Question added.")
    return redirect(url_for("admin_questions"))


@app.post("/admin/question/<int:qid>/disable")
def admin_question_disable(qid: int):
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("stages"))
    db = get_db()
    db.execute("UPDATE admin_questions SET disabled=1 WHERE id=?", (qid,))
    db.commit()
    flash("Question disabled.")
    return redirect(url_for("admin_questions"))


@app.post("/admin/question/<int:qid>/enable")
def admin_question_enable(qid: int):
    guard = require_login()
    if guard:
        return guard
    if session.get("role") != "teacher":
        return redirect(url_for("stages"))
    db = get_db()
    db.execute("UPDATE admin_questions SET disabled=0 WHERE id=?", (qid,))
    db.commit()
    flash("Question enabled.")
    return redirect(url_for("admin_questions"))


@app.get("/ml-train")
def ml_train():
    guard = require_login()
    if guard:
        return guard

    info = train_and_save(DB_PATH)
    flash(f"ML model trained ✅ ({info['used']})")
    if session.get("role") == "teacher":
        return redirect(url_for("teacher_dashboard"))
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    app.run(debug=True)