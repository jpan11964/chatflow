# การตั้งค่า gunicorn สำหรับ Render
# ปิด access log ดิบ (GET/POST ทุกบรรทัด) เหลือเฉพาะ log กิจกรรมที่อ่านง่ายจากแอป
accesslog = None
errorlog = "-"        # error/log ของแอปออก stdout (Render เก็บให้)
loglevel = "info"
