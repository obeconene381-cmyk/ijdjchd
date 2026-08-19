import json
import os
import re
import subprocess
import time

# --- الإعدادات ---
TEST_UUID = "b831381d-6324-4d53-ad4f-8cda48b30811"
TEST_EMAIL = "tester@vpn.local"
BAN_DURATION = 15  # مدة الفصل بالثواني

XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_BIN = "/usr/local/bin/xray"
API_SERVER = "127.0.0.1:10085"
INBOUND_TAG = "vless-inbound"
LOG_PATH = "/var/log/xray/access.log"

is_banned = False
unban_time = 0


def log(msg):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ANTI-SHARE] {msg}", flush=True
    )


def init_xray():
    """تشغيل Xray مرة واحدة فقط عند البداية"""
    config = {
        "log": {
            "access": LOG_PATH,
            "error": "/var/log/xray/error.log",
            "loglevel": "warning",
        },
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {
                "0": {"statsUserUplink": True, "statsUserDownlink": True}
            }
        },
        "inbounds": [
            {
                "port": 5000,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "tag": INBOUND_TAG,
                "settings": {
                    "clients": [{"id": TEST_UUID, "email": TEST_EMAIL}],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/@pycorav1"},
                },
            },
            {
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
                "tag": "api-inbound",
            },
        ],
        "routing": {
            "rules": [
                {
                    "inboundTag": ["api-inbound"],
                    "outboundTag": "api",
                    "type": "field",
                }
            ]
        },
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "api"},
        ],
    }

    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    subprocess.run(["pkill", "-9", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])
    log(f"🚀 Xray started successfully with UUID: {TEST_UUID}")


def api_remove_user():
    """طرد المستخدم عبر API فوراً"""
    cmd = [
        XRAY_BIN,
        "api",
        "rmu",
        f"--server={API_SERVER}",
        f"-tag={INBOUND_TAG}",
        TEST_EMAIL,
    ]
    subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    log(
        f"⛔ User {TEST_EMAIL} REMOVED via API. Connection dropped for {BAN_DURATION}s."
    )


def api_add_user():
    """إرجاع العميل للخدمة عبر API بعد انتهاء العقوبة"""
    client_data = json.dumps({"id": TEST_UUID, "email": TEST_EMAIL})
    cmd = [
        XRAY_BIN,
        "api",
        "adu",
        f"--server={API_SERVER}",
        f"-tag={INBOUND_TAG}",
        client_data,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        log(f"✅ User {TEST_EMAIL} restored via API successfully.")
    else:
        log(f"⚠️ Failed to restore user via API: {res.stderr.strip()}")


# نمط Regex يستخرج: التاريخ والوقت (بدون أجزاء الثانية)، عنوان الـ IP، والإيميل
# مثال: 2026/08/19 18:48:49.713692 from 129.45.83.252:0 accepted ... email: tester@vpn.local
LOG_REGEX = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\.\d+\s+from\s+([^:]+):\d+\s+accepted.*email:\s*(\S+)"
)


def detect_multi_ip():
    """فحص السجلات لرصد الـ IPs النشطة لكل حساب"""
    global is_banned, unban_time

    if not os.path.exists(LOG_PATH) or is_banned:
        return

    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()[-200:]
    except Exception:
        return

    now_ts = time.time()
    active_ips = {}  # ip -> last_seen_timestamp

    for line in lines:
        match = LOG_REGEX.search(line.strip())
        if not match:
            continue

        time_str, client_ip, email = match.groups()

        if email != TEST_EMAIL:
            continue

        try:
            # تحويل الوقت مع إهمال أجزاء الثانية
            log_ts = time.mktime(
                time.strptime(time_str, "%Y/%m/%d %H:%M:%S")
            )

            # فحص النشاط في آخر 4 ثوانٍ فقط
            if abs(now_ts - log_ts) <= 4:
                active_ips[client_ip] = max(
                    active_ips.get(client_ip, 0), log_ts
                )
        except Exception:
            continue

    # إذا وجدنا أكثر من IP نشط لنفس الإيميل في نفس اللحظة
    if len(active_ips) >= 2:
        is_banned = True
        unban_time = time.time() + BAN_DURATION

        ips_list = list(active_ips.keys())
        log(f"🚨 Multi-IP DETECTED for {TEST_EMAIL}! IPs: {ips_list}")

        # مسح السجل لتفادي تكرار العقاب
        try:
            with open(LOG_PATH, "w") as f:
                f.write("")
        except Exception:
            pass

        # الفصل الفوري عبر API
        api_remove_user()


# --- بداية التشغيل ---
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
with open(LOG_PATH, "w") as f:
    f.write("")

init_xray()
time.sleep(1)

try:
    while True:
        now = time.time()

        if not is_banned:
            detect_multi_ip()
        else:
            if now >= unban_time:
                api_add_user()
                is_banned = False

        time.sleep(0.5)
except KeyboardInterrupt:
    log("Manager stopped.")
