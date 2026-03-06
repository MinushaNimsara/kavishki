import os
import random
import sqlite3
from datetime import datetime
from typing import List, Dict, Tuple

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(APP_DIR, "models")
MODEL_PATH = os.path.join(MODEL_DIR, "edubear_rf.pkl")


def _ensure_dir():
    os.makedirs(MODEL_DIR, exist_ok=True)


def load_attempt_rows(db_path: str) -> List[Dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT a.user_id, a.subject, a.topic, a.difficulty,
               a.total_q, a.correct_q,
               (a.ended_at - a.started_at) AS seconds,
               a.hints_used,
               u.grade
        FROM attempts a
        JOIN users u ON u.id = a.user_id
        ORDER BY a.id ASC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _encode_subject(s: str) -> int:
    return {"math": 0, "english": 1, "science": 2}.get(s, 0)


def _encode_diff(d: str) -> int:
    return {"easy": 0, "medium": 1, "hard": 2}.get(d, 0)


def build_dataset(rows: List[Dict]) -> Tuple[np.ndarray, np.ndarray]:
    """
    Features:
      grade, subject_id, diff_id, accuracy, seconds, hints
    Label:
      success (1 if accuracy >= 0.70 else 0)
    """
    X = []
    y = []

    for r in rows:
        total = max(1, int(r["total_q"]))
        correct = int(r["correct_q"])
        acc = correct / total

        sec = float(r.get("seconds") or 0.0)
        hints = int(r.get("hints_used") or 0)
        grade = int(r.get("grade") or 1)

        X.append([
            grade,
            _encode_subject(r.get("subject", "math")),
            _encode_diff(r.get("difficulty", "easy")),
            acc,
            sec,
            hints
        ])
        y.append(1 if acc >= 0.70 else 0)

    return np.array(X, dtype=float), np.array(y, dtype=int)


def generate_synthetic_rows(n: int = 500) -> List[Dict]:
    """
    Bootstraps training when real data is small.
    Generates realistic-ish attempts based on grade + difficulty.
    """
    subjects = ["math", "english", "science"]
    diffs = ["easy", "medium"]

    rows = []
    for _ in range(n):
        grade = random.randint(1, 5)
        subject = random.choice(subjects)
        diff = random.choice(diffs)

        # baseline success
        base = 0.75
        if grade <= 2:
            base -= 0.05
        if grade == 5:
            base += 0.05
        if diff == "medium":
            base -= 0.10

        acc = max(0.10, min(0.98, random.gauss(base, 0.12)))
        total_q = random.choice([3, 4, 5])
        correct_q = int(round(acc * total_q))
        correct_q = max(0, min(total_q, correct_q))

        seconds = random.uniform(20, 90) + (20 if diff == "medium" else 0)
        hints = 0 if acc >= 0.7 else random.choice([0, 1, 2])

        rows.append({
            "user_id": 0,
            "subject": subject,
            "topic": "synthetic",
            "difficulty": diff,
            "total_q": total_q,
            "correct_q": correct_q,
            "seconds": seconds,
            "hints_used": hints,
            "grade": grade
        })

    return rows


def train_and_save(db_path: str, min_real_rows: int = 60) -> Dict:
    _ensure_dir()
    real_rows = load_attempt_rows(db_path)

    if len(real_rows) < min_real_rows:
        rows = generate_synthetic_rows(500) + real_rows
        used = f"synthetic + real ({len(real_rows)} real rows)"
    else:
        rows = real_rows
        used = f"real only ({len(real_rows)} rows)"

    X, y = build_dataset(rows)

    model = RandomForestClassifier(
        n_estimators=240,
        max_depth=10,
        random_state=42
    )
    model.fit(X, y)
    joblib.dump(model, MODEL_PATH)

    return {
        "status": "trained",
        "used": used,
        "model_path": MODEL_PATH,
        "trained_at": datetime.utcnow().isoformat()
    }


def load_model():
    if os.path.exists(MODEL_PATH):
        return joblib.load(MODEL_PATH)
    return None


def predict_success_prob(model, grade: int, subject: str, difficulty: str,
                         last_accuracy: float, seconds: float, hints_used: int) -> float:
    x = np.array([[
        int(grade),
        _encode_subject(subject),
        _encode_diff(difficulty),
        float(last_accuracy),
        float(seconds),
        int(hints_used)
    ]], dtype=float)

    p = float(model.predict_proba(x)[0][1])
    return max(0.0, min(1.0, p))


# ---------------- Weakness classification (Weak / Moderate / Strong) ----------------
def classify_weakness(mastery: float, recent_failures: int = 0, avg_accuracy: float = 0.5) -> str:
    """
    Returns: "Weak" | "Moderate" | "Strong"
    Based on mastery, repeated failures, and average accuracy.
    """
    if mastery >= 0.70 and avg_accuracy >= 0.70 and recent_failures == 0:
        return "Strong"
    if mastery < 0.40 or avg_accuracy < 0.45 or recent_failures >= 2:
        return "Weak"
    return "Moderate"


# ---------------- Risk score (struggle / drop-off) ----------------
def compute_risk_score(
    days_since_last_activity: int,
    recent_failures: int,
    avg_accuracy: float,
    low_mastery_count: int
) -> float:
    """
    Returns risk score 0-1. Higher = more at risk.
    """
    risk = 0.0
    if days_since_last_activity > 7:
        risk += 0.3
    elif days_since_last_activity > 3:
        risk += 0.15
    if recent_failures >= 3:
        risk += 0.35
    elif recent_failures >= 1:
        risk += 0.15
    if avg_accuracy < 0.40:
        risk += 0.25
    elif avg_accuracy < 0.55:
        risk += 0.1
    if low_mastery_count >= 3:
        risk += 0.2
    return min(1.0, risk)


# ---------------- Confidence trend ----------------
def estimate_confidence(streak: int, recent_success_rate: float, avg_speed_ok: bool) -> str:
    """
    Returns: "High" | "Medium" | "Low"
    """
    if streak >= 3 and recent_success_rate >= 0.75 and avg_speed_ok:
        return "High"
    if recent_success_rate < 0.50 or streak == 0:
        return "Low"
    return "Medium"


# ---------------- Recommendation: next best lesson ----------------
def recommend_next_lesson(
    topic_masteries: List[Tuple[str, str, float]],
    weak_topics: List[Tuple[str, str]]
) -> Tuple[str, str, str]:
    """
    topic_masteries: [(subject, topic, mastery), ...]
    Returns (subject, topic, reason).
    """
    if weak_topics:
        subj, topic = weak_topics[0]
        return subj, topic, "Needs more practice"
    if not topic_masteries:
        return "Mathematics", "addition", "Start learning"
    lowest = min(topic_masteries, key=lambda x: x[2])
    return lowest[0], lowest[1], "Continue building mastery"