import hashlib
import json
import logging
import os
import random
import threading
from datetime import timedelta
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask_session import Session
from pymongo import MongoClient
from pymongo.errors import PyMongoError

BASE_DIR = Path(__file__).resolve().parent
RESPONSES_FILE = BASE_DIR / "responses.json"


def _compute_version() -> str:
    """เวอร์ชันของแอป — เปลี่ยนทุกครั้งที่ deploy (ใช้บังคับ client รีเฟรช)"""
    commit = os.environ.get("RENDER_GIT_COMMIT")
    if commit:
        return commit[:12]
    # สำรอง (รันในเครื่อง): แฮชจากเวลาแก้ไขไฟล์หลัก
    parts = []
    for name in ("app.py", "templates/index.html", "static/styles.css"):
        try:
            parts.append(str((BASE_DIR / name).stat().st_mtime_ns))
        except OSError:
            pass
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


APP_VERSION = _compute_version()

# จำนวนข้อความที่สุ่มตอบกลับ (ค่าเริ่มต้น และค่าเฉพาะบาง keyword)
DEFAULT_SAMPLE_SIZE = 5
SAMPLE_OVERRIDES = {
    "กรอกข้อมูล": 3,
}

MAX_NOTE_LEN = 5000
MAX_NOTES_PER_USER = 500
MAX_RESPONSE_LEN = 5000
_user_lock = threading.Lock()

# ===== MongoDB =====
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("MONGODB_DB", "chatflow")
COLLECTION_NAME = "userdata"
RESPONSES_COLLECTION = "responses"

# MongoClient เชื่อมแบบ lazy (ไม่ติดต่อจริงจนกว่าจะใช้) — สร้างได้แม้ DB ยังไม่พร้อม
mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000) if MONGODB_URI else None


def get_collection():
    """คืน collection userdata"""
    if mongo_client is None:
        raise RuntimeError("ยังไม่ได้ตั้งค่า MONGODB_URI")
    return mongo_client[DB_NAME][COLLECTION_NAME]


# ===== Flask + session ถาวร (เก็บฝั่ง server ใน MongoDB) =====
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "chatflow-default-secret-please-change"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_PERMANENT=True,
    TEMPLATES_AUTO_RELOAD=True,   # แก้ HTML แล้ว refresh เห็นเลย ไม่ต้องรีสตาร์ท
)

if mongo_client is not None:
    # เก็บ session ลง collection 'sessions' ใน MongoDB
    app.config.update(
        SESSION_TYPE="mongodb",
        SESSION_MONGODB=mongo_client,
        SESSION_MONGODB_DB=DB_NAME,
        SESSION_MONGODB_COLLECT="sessions",
    )
    Session(app)

# อยู่หลัง reverse proxy ของ Render — ใช้ X-Forwarded-For เพื่อให้ได้ IP ของผู้ใช้จริง
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ===== Log แบบอ่านง่าย (console เท่านั้น — ไม่เก็บไฟล์) =====
# ปิด access log ดิบทั้งของ Flask dev (werkzeug) และ gunicorn (บน Render)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("gunicorn.access").setLevel(logging.WARNING)
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


# ===== ข้อความปักหมุด (ต่อผู้ใช้ ต่อปุ่ม) — เก็บใน userdata: {pins: {keyword: [text,...]}} =====
def get_pins(user: str, keyword: str = None):
    doc = get_collection().find_one({"_id": user}) or {}
    pins = doc.get("pins", {})
    if not isinstance(pins, dict):
        pins = {}
    if keyword is None:
        return pins
    return pins.get(keyword, [])


# ===== คลังคำสุ่มใน MongoDB (1 เอกสารต่อปุ่ม: {_id: keyword, responses: [...]}) =====
def get_responses_collection():
    if mongo_client is None:
        raise RuntimeError("ยังไม่ได้ตั้งค่า MONGODB_URI")
    return mongo_client[DB_NAME][RESPONSES_COLLECTION]


def get_keyword_responses(keyword: str) -> list:
    doc = get_responses_collection().find_one({"_id": keyword})
    return (doc or {}).get("responses", [])


def keyword_exists(keyword: str) -> bool:
    return get_responses_collection().find_one({"_id": keyword}, {"_id": 1}) is not None


def seed_responses_if_empty() -> None:
    """ครั้งแรก: ย้ายข้อมูลจาก responses.json เข้า MongoDB ถ้า collection ยังว่าง"""
    try:
        col = get_responses_collection()
        if col.estimated_document_count() > 0:
            return
        data = load_responses()
        docs = [{"_id": k, "responses": v} for k, v in data.items() if isinstance(v, list)]
        if docs:
            col.insert_many(docs)
            _activity.info(f"seed responses: {len(docs)} keywords เข้าฐานข้อมูล")
    except Exception as e:  # ไม่ให้ startup ล้มถ้า DB ยังไม่พร้อม
        _activity.error(f"seed responses failed: {e}")


if mongo_client is not None:
    seed_responses_if_empty()


@app.errorhandler(PyMongoError)
def handle_mongo_error(err):
    _activity.error(f"MongoDB error: {err}")
    return jsonify({"error": "database unavailable"}), 503


@app.route("/")
def login_page():
    """หน้าแรก — ถ้าล็อกอินค้างไว้แล้วเข้าแอปเลย ไม่ต้องกรอกชื่อใหม่"""
    if current_user():
        return redirect(url_for("app_page"))
    return render_template("login.html", version=APP_VERSION)


@app.route("/app")
def app_page():
    """หน้าแอปหลัก — ต้องล็อกอินก่อน"""
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    return render_template("index.html", user=user, version=APP_VERSION)


@app.route("/version")
def version():
    """ให้ client ตรวจว่ามีเวอร์ชันใหม่หรือยัง (ไว้บังคับรีเฟรชหลัง deploy)"""
    return jsonify({"version": APP_VERSION})


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

    pool = get_keyword_responses(keyword)
    if not pool:
        return jsonify({"error": "Invalid keyword"}), 400

    log_activity(current_user(), f"กดปุ่ม '{keyword}'")
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


@app.route("/api/responses/keywords", methods=["GET"])
def list_keywords():
    """รายชื่อปุ่ม (keyword) ที่มีในคลัง สำหรับ dropdown เพิ่มคำ"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    keywords = sorted(d["_id"] for d in get_responses_collection().find({}, {"_id": 1}))
    return jsonify({"keywords": keywords})


@app.route("/api/responses", methods=["POST"])
def add_response():
    """เพิ่มคำใหม่เข้าคลังของปุ่มที่ระบุ"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    keyword = (body.get("keyword") or "").strip()
    text = (body.get("text") or "").strip()
    if not keyword or not text:
        return jsonify({"error": "missing keyword or text"}), 400
    if not keyword_exists(keyword):
        return jsonify({"error": "unknown keyword"}), 400

    text = text[:MAX_RESPONSE_LEN]
    get_responses_collection().update_one({"_id": keyword}, {"$push": {"responses": text}})
    preview = text[:40] + ("..." if len(text) > 40 else "")
    log_activity(user, f"เพิ่มคำที่ปุ่ม '{keyword}': \"{preview}\"")
    return jsonify({"ok": True})


@app.route("/api/pins", methods=["GET"])
def list_pins():
    """ดึงรายการข้อความปักหมุดของผู้ใช้สำหรับปุ่มที่ระบุ"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    keyword = (request.args.get("keyword") or "").strip()
    return jsonify({"pins": get_pins(user, keyword) if keyword else []})


@app.route("/api/pins/toggle", methods=["POST"])
def toggle_pin():
    """ปักหมุด/เลิกปักหมุดข้อความของปุ่มหนึ่ง (สลับสถานะ)"""
    user = current_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    keyword = (body.get("keyword") or "").strip()
    text = (body.get("text") or "")[:MAX_RESPONSE_LEN]
    if not keyword or not text.strip():
        return jsonify({"error": "missing keyword or text"}), 400

    with _user_lock:
        doc = get_collection().find_one({"_id": user}) or {}
        pins = doc.get("pins", {})
        if not isinstance(pins, dict):
            pins = {}
        current = pins.get(keyword, [])
        if text in current:
            current = [t for t in current if t != text]
            pinned = False
        else:
            current = [text] + current
            pinned = True
        pins[keyword] = current
        get_collection().update_one({"_id": user}, {"$set": {"pins": pins}}, upsert=True)

    action = "ปักหมุด" if pinned else "เลิกปักหมุด"
    preview = text.replace("\n", " ")[:40]
    log_activity(user, f"{action}ข้อความที่ปุ่ม '{keyword}': \"{preview}\"")
    return jsonify({"pinned": pinned, "pins": current})


@app.route("/health")
def health():
    """ตรวจสอบสถานะระบบ + การเชื่อมต่อ MongoDB"""
    db_ok = True
    keywords = 0
    try:
        get_collection().database.client.admin.command("ping")
        keywords = get_responses_collection().estimated_document_count()
    except Exception:
        db_ok = False
    return jsonify({"status": "ok", "keywords": keywords, "mongo": db_ok})


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", "6060"))
    app.run(host="0.0.0.0", port=port, debug=debug)
