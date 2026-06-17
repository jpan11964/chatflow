import json
import logging
import os
import random
import threading
from datetime import timedelta
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pymongo import MongoClient
from pymongo.errors import PyMongoError

BASE_DIR = Path(__file__).resolve().parent
RESPONSES_FILE = BASE_DIR / "responses.json"

# จำนวนข้อความที่สุ่มตอบกลับ (ค่าเริ่มต้น และค่าเฉพาะบาง keyword)
DEFAULT_SAMPLE_SIZE = 5
SAMPLE_OVERRIDES = {
    "กรอกข้อมูล": 3,
}

MAX_NOTE_LEN = 5000
MAX_NOTES_PER_USER = 500
_user_lock = threading.Lock()

# ===== Flask + session ถาวร =====
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "chatflow-default-secret-please-change"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    SESSION_COOKIE_SAMESITE="Lax",
)

# ===== MongoDB =====
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("MONGODB_DB", "chatflow")
COLLECTION_NAME = "userdata"

_mongo_client = None
_userdata = None


def get_collection():
    """คืน collection userdata (เชื่อมต่อแบบ lazy ครั้งแรกที่ใช้)"""
    global _mongo_client, _userdata
    if _userdata is None:
        if not MONGODB_URI:
            raise RuntimeError("ยังไม่ได้ตั้งค่า MONGODB_URI")
        _mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
        _userdata = _mongo_client[DB_NAME][COLLECTION_NAME]
    return _userdata


# ===== Log แบบอ่านง่าย (console เท่านั้น — ไม่เก็บไฟล์) =====
logging.getLogger("werkzeug").setLevel(logging.WARNING)
_activity = logging.getLogger("chatflow")
_activity.setLevel(logging.INFO)
_activity.propagate = False
if not _activity.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%d/%m/%Y %H:%M:%S"))
    _activity.addHandler(_h)


def log_activity(user: str, action: str) -> None:
    ip = request.remote_addr if request else "-"
    _activity.info(f"{user or '(ไม่ทราบชื่อ)'} [{ip}] {action}")


def load_responses() -> dict:
    """โหลดคลังข้อความตอบกลับจากไฟล์ JSON อย่างปลอดภัย"""
    try:
        with open(RESPONSES_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if not isinstance(data, dict):
                _activity.error("responses.json ต้องเป็น object (dict)")
                return {}
            return data
    except FileNotFoundError:
        _activity.error("ไม่พบไฟล์ responses.json")
    except json.JSONDecodeError:
        _activity.error("ไฟล์ responses.json มีข้อผิดพลาดในการอ่าน")
    return {}


def _clean_username(name) -> str:
    return (name or "").strip()[:60]


def current_user():
    return session.get("user")


# ===== โน๊ตใน MongoDB (1 เอกสารต่อผู้ใช้: {_id: username, notes: [...]}) =====
def get_notes(user: str) -> list:
    doc = get_collection().find_one({"_id": user})
    return (doc or {}).get("notes", [])


def set_notes(user: str, notes: list) -> None:
    get_collection().update_one({"_id": user}, {"$set": {"notes": notes}}, upsert=True)


responses = load_responses()


@app.errorhandler(PyMongoError)
def handle_mongo_error(err):
    _activity.error(f"MongoDB error: {err}")
    return jsonify({"error": "database unavailable"}), 503


@app.route("/")
def login_page():
    """หน้าแรก — ถ้าล็อกอินค้างไว้แล้วเข้าแอปเลย ไม่ต้องกรอกชื่อใหม่"""
    if current_user():
        return redirect(url_for("app_page"))
    return render_template("login.html")


@app.route("/app")
def app_page():
    """หน้าแอปหลัก — ต้องล็อกอินก่อน"""
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    return render_template("index.html", user=user)


@app.route("/api/login", methods=["POST"])
def api_login():
    """ตั้ง session ถาวรเมื่อผู้ใช้กรอกชื่อ"""
    body = request.get_json(silent=True) or {}
    user = _clean_username(body.get("user"))
    if not user:
        return jsonify({"error": "missing user"}), 400
    session.permanent = True
    session["user"] = user
    log_activity(user, "เข้าใช้งาน")
    return jsonify({"ok": True})


@app.route("/logout")
def logout():
    user = session.pop("user", None)
    if user:
        log_activity(user, "ออกจากระบบ")
    return redirect(url_for("login_page"))


@app.route("/generate", methods=["POST"])
def generate():
    """สุ่มข้อความตอบกลับตาม keyword ที่ส่งมา"""
    data = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip()

    if keyword not in responses:
        return jsonify({"error": "Invalid keyword"}), 400

    log_activity(current_user(), f"กดปุ่ม '{keyword}'")
    pool = responses[keyword]
    sample_size = SAMPLE_OVERRIDES.get(keyword, DEFAULT_SAMPLE_SIZE)
    picked = random.sample(pool, min(sample_size, len(pool)))

    # แปลง \n เป็น <br> เพื่อแสดงผลใน HTML
    formatted = [resp.replace("\n", "<br>") for resp in picked]
    return jsonify({"responses": formatted})


@app.route("/api/notes", methods=["GET"])
def list_notes():
    """ดึงรายการโน๊ตของผู้ใช้ที่ล็อกอินอยู่"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    log_activity(user, "เปิดดูโน๊ต")
    return jsonify({"notes": get_notes(user)})


@app.route("/api/notes", methods=["POST"])
def add_note():
    """เพิ่มโน๊ตใหม่"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty note"}), 400

    text = text[:MAX_NOTE_LEN]
    with _user_lock:
        notes = get_notes(user)
        if len(notes) >= MAX_NOTES_PER_USER:
            return jsonify({"error": "note limit reached"}), 400
        notes.append(text)
        set_notes(user, notes)
        preview = text[:40] + ("..." if len(text) > 40 else "")
        log_activity(user, f"บันทึกโน๊ต: \"{preview}\"")
        return jsonify({"notes": notes})


@app.route("/api/notes/delete", methods=["POST"])
def delete_note():
    """ลบโน๊ตตาม index"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    index = body.get("index")
    with _user_lock:
        notes = get_notes(user)
        if not isinstance(index, int) or not (0 <= index < len(notes)):
            return jsonify({"error": "invalid index"}), 400
        notes.pop(index)
        set_notes(user, notes)
        log_activity(user, "ลบโน๊ต")
        return jsonify({"notes": notes})


@app.route("/api/notes/reorder", methods=["POST"])
def reorder_notes():
    """จัดลำดับโน๊ตใหม่ (order = permutation ของ index เดิม)"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    order = body.get("order")
    with _user_lock:
        notes = get_notes(user)
        if (not isinstance(order, list)
                or len(order) != len(notes)
                or sorted(order) != list(range(len(notes)))):
            return jsonify({"error": "invalid order"}), 400
        notes = [notes[i] for i in order]
        set_notes(user, notes)
        log_activity(user, "จัดลำดับโน๊ตใหม่")
        return jsonify({"notes": notes})


@app.route("/health")
def health():
    """ตรวจสอบสถานะระบบ + การเชื่อมต่อ MongoDB"""
    db_ok = True
    try:
        get_collection().database.client.admin.command("ping")
    except Exception:
        db_ok = False
    return jsonify({"status": "ok", "keywords": len(responses), "mongo": db_ok})


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "6060"))
    app.run(host="0.0.0.0", port=port, debug=debug)
