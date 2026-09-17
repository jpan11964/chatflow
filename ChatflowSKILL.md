---
name: chatflow
description: >
  ChatFlow — เว็บแอป Flask สำหรับทีมบริการลูกค้า (ภาษาไทย): กดปุ่มหมวดหมู่เพื่อ
  สุ่มข้อความตอบกลับสำเร็จรูปจาก responses.json แล้วคลิกเพื่อคัดลอก พร้อมระบบ "โน๊ต"
  ส่วนตัวของผู้ใช้แต่ละคน (บันทึก/ลบ/ลากจัดลำดับ) เก็บข้อมูลและ session บน MongoDB
  รองรับล็อกอินค้างข้ามการปิดเครื่อง และ deploy บน Render ด้วย gunicorn. ใช้ skill นี้
  เมื่อต้องแก้ไข ดูแล หรือ deploy โปรเจ็ค ChatFlow.
---

# ChatFlow

เว็บแอปช่วยแอดมิน/ทีม CS ตอบแชทลูกค้าเร็วขึ้น: เลือกหมวด → สุ่มข้อความตอบกลับ → คลิกคัดลอก
พร้อมกระดานโน๊ตส่วนตัวของแต่ละผู้ใช้

## สแตก
- **Backend:** Flask (Python) — `app.py`
- **Frontend:** HTML/CSS/JS ล้วน (ไม่มี framework) — `templates/`, `static/styles.css`
- **ฐานข้อมูล:** MongoDB Atlas (database `chatflow`)
- **Session:** Flask-Session เก็บฝั่ง server ใน MongoDB (collection `sessions`)
- **Deploy:** Render (gunicorn) / รันในเครื่องด้วย waitress หรือ flask dev server

## โครงสร้างไฟล์สำคัญ
| ไฟล์ | หน้าที่ |
|------|---------|
| `app.py` | เซิร์ฟเวอร์ Flask ทั้งหมด (routes + API + MongoDB + logging) |
| `templates/login.html` | หน้าแรก — กรอกชื่อผู้ใช้ |
| `templates/index.html` | หน้าแอปหลัก — ปุ่มหมวด + พื้นที่แสดงผล + โหมดโน๊ต (JS อยู่ท้ายไฟล์) |
| `static/styles.css` | ธีมทั้งหมด (CSS variables + dark mode + responsive + drag styles) |
| `responses.json` | คลังข้อความตอบกลับ `{ "keyword": ["ข้อความ", ...] }` (read-only ตอนรัน) |
| `requirements.txt` | dependencies |
| `Procfile` | คำสั่งรันบน Render: `web: gunicorn app:app -c gunicorn.conf.py` |
| `gunicorn.conf.py` | ปิด access log ดิบของ gunicorn |
| `.env` | ความลับ (ห้าม commit — อยู่ใน .gitignore) |

## Environment variables (ต้องตั้งบน Render + `.env` ในเครื่อง)
- `MONGODB_URI` — connection string ของ MongoDB Atlas (มี `/chatflow` เป็น default db)
- `MONGODB_DB` — ชื่อ database (ค่าเริ่มต้น `chatflow`)
- `SECRET_KEY` — สตริงสุ่มยาว **ต้องคงที่** (ถ้าเปลี่ยน session ทุกคนหลุด)
- `FLASK_DEBUG` — `1` เพื่อเปิด debug (เฉพาะ dev), `PORT` — พอร์ต (ค่าเริ่มต้น 6060)

## โมเดลข้อมูล (MongoDB)
- **`userdata`** — 1 เอกสาร/ผู้ใช้: `{ "_id": "<ชื่อผู้ใช้>", "notes": ["...", ...] }`
- **`sessions`** — จัดการโดย Flask-Session อัตโนมัติ (อย่าแก้มือ)

## Routes / API
| Method | Path | หน้าที่ | Auth |
|--------|------|---------|------|
| GET | `/` | หน้า login (ถ้ามี session อยู่แล้ว → redirect `/app`) | - |
| GET | `/app` | หน้าแอปหลัก | ต้องมี session |
| POST | `/api/login` | `{user}` → ตั้ง session ถาวร | - |
| GET | `/logout` | ล้าง session | - |
| POST | `/generate` | `{keyword}` → สุ่ม 5 ข้อความ (3 ถ้า keyword = `กรอกข้อมูล`) | - |
| GET | `/api/notes` | รายการโน๊ตของผู้ใช้ | 401 ถ้าไม่มี session |
| POST | `/api/notes` | `{text}` → เพิ่มโน๊ต | 401 |
| POST | `/api/notes/delete` | `{index}` → ลบโน๊ต | 401 |
| POST | `/api/notes/reorder` | `{order}` (permutation) → จัดลำดับใหม่ | 401 |
| GET | `/health` | สถานะระบบ + สถานะ MongoDB | - |

## แนวคิด/พฤติกรรมสำคัญ
- **Auth:** ชื่อผู้ใช้เก็บใน session cookie (อายุ 365 วัน) + เอกสารใน `sessions` — เปิดเครื่องใหม่เข้าได้เลย
  API อ่านชื่อผู้ใช้จาก `session["user"]` เท่านั้น (ไม่เชื่อค่าจาก client). ฝั่ง client อ่านชื่อจาก
  `document.body.dataset.user` ที่ server ฝังมา (ผ่าน Jinja `{{ user }}`)
- **คัดลอก:** ใช้ `navigator.clipboard` ถ้าเป็น HTTPS/localhost ไม่งั้น fallback เป็น `execCommand('copy')`
  (จำเป็นเพราะใช้งานผ่าน HTTP บน LAN บ่อย)
- **ปุ่มโน๊ต:** สีส้มอำพัน (`--note`) ต่างจากปุ่มหมวด (น้ำเงิน/เขียว/แดง) เป็นปุ่มขวาสุด
- **โน๊ต:** แสดงสูงสุด 4 บรรทัด (`-webkit-line-clamp`), **กดค้างเพื่อลากจัดลำดับ** (การ์ดลอยตามเมาส์ +
  placeholder เส้นประ, บันทึกลำดับไป server), แตะเร็ว = คัดลอก
- **ข้อความผู้ใช้กรอกเอง** ต้อง escape ป้องกัน XSS (มี `escapeHtml` ใน index.html)
- **Log:** อ่านง่ายเป็นภาษาไทย `[วันเวลา] ชื่อผู้ใช้ [IP] การกระทำ` ออก console เท่านั้น (ไม่เก็บไฟล์)
  ปิด access log ดิบของทั้ง werkzeug และ gunicorn แล้ว; ใช้ `ProxyFix` เพื่อได้ IP จริงหลัง proxy ของ Render

## รันในเครื่อง (Windows)
```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# สร้างไฟล์ .env ใส่ MONGODB_URI / MONGODB_DB / SECRET_KEY
.venv\Scripts\python app.py            # dev server ที่ http://localhost:6060
# หรือ production-like:
.venv\Scripts\waitress-serve --port=6060 app:app
```

## Deploy บน Render
1. ตั้ง Environment Variables: `MONGODB_URI`, `SECRET_KEY` (+ `MONGODB_DB` ถ้าต้องการ)
2. Start Command: `gunicorn app:app -c gunicorn.conf.py`
3. MongoDB Atlas → Network Access เปิด `0.0.0.0/0` (หรือ IP ของ Render)
4. Auto-Deploy = On Commit → push ขึ้น GitHub แล้ว Render deploy อัตโนมัติ

## ข้อควรระวัง
- **ห้าม commit `.env`** (มี credential) — ถูก gitignore แล้ว
- แก้ปุ่มหมวด: แก้ทั้ง `data-keyword` ใน `index.html` และ key ใน `responses.json` ให้ตรงกัน
- Render filesystem ชั่วคราว → ห้ามเก็บข้อมูลถาวรลงไฟล์ ใช้ MongoDB เท่านั้น
- `_user_lock` เป็น lock ระดับ process เดียว — ถ้ารันหลาย worker/instance จะไม่กันชนกันข้าม process
  (ปริมาณการใช้งานต่ำจึงยอมรับได้ แต่พึงระวังถ้าสเกล)
