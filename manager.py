import json
import os
import subprocess
import time

# --- الإعدادات الثابتة للتجربة ---
TEST_UUID = "b831381d-6324-4d53-ad4f-8cda48b30811"  # ضع الـ UUID الخاص بك هنا
TEST_EMAIL = "tester@vpn.local"
BAN_DURATION = 10  # مدة الفصل/الحظر بالثواني عند اكتشاف المشاركة

XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_BIN = "/usr/local/bin/xray"
API_SERVER = "127.0.0.1:10085"
INBOUND_TAG = "vless-inbound"
LOG_PATH = "/var/log/xray/access.log"

# حالة المستخدم في الذاكرة
user_state = {
    "email": TEST_EMAIL,
    "uuid": TEST_UUID,
    "is_active": True,
    "banned_until": 0,
}


def log(msg):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [MANAGER] {msg}", flush=True
    )


def restart_xray():
    """كتابة الإعدادات وإعادة تشغيل Xray بالـ UUID المختار"""
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

    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])
    user_state["is_active"] = True
    log(f"🚀 Xray started with UUID: {TEST_UUID} ({TEST_EMAIL})")


def kick_user():
    """حذف العميل من Xray فوراً لقطع اتصاله"""
    cmd = f'{XRAY_BIN} api rmu --server={API_SERVER} -tag="{INBOUND_TAG}" "{TEST_EMAIL}"'
    subprocess.run(
        cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    user_state["is_active"] = False
    user_state["banned_until"] = time.time() + BAN_DURATION
    log(
        f"⛔ User {TEST_EMAIL} KICKED! Connection blocked for {BAN_DURATION}s."
    )


def re_add_user():
    """إعادة إضافة المستخدم بعد انتهاء مدة الحظر المؤقت"""
    client_json = json.dumps({"id": TEST_UUID, "email": TEST_EMAIL})
    # إضافة المستخدم مجدداً عبر API بدون الحاجة لإعادة تشغيل السيرفر
    cmd = [
        XRAY_BIN,
        "api",
        "adu",
        f"--server={API_SERVER}",
        f"-tag={INBOUND_TAG}",
        client_json,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)

    if res.returncode == 0:
        user_state["is_active"] = True
        log(f"✅ Ban expired. User {TEST_EMAIL} re-added and active again.")
    else:
        # إذا فشل الـ API adu يتم عمل restart سريع
        restart_xray()


def detect_sharing_and_punish():
    """فحص سجل الاتصال والتحقق من نشاط أكثر من IP خلال ثانيتين"""
    if not os.path.exists(LOG_PATH) or not user_state["is_active"]:
        return

    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()[-300:]
    except Exception:
        return

    now_ts = time.time()
    ip_tracker = {}  # ip -> last_timestamp

    for line in lines:
        if (
            "from " not in line
            or f"email: {TEST_EMAIL}" not in line
            and "email:" not in line
        ):
            continue

        parts = line.split()
        try:
            date_part = parts[0]
            time_part = parts[1]
            log_ts = time.mktime(
                time.strptime(f"{date_part} {time_part}", "%Y/%m/%d %H:%M:%S")
            )

            # تجاهل السجلات القديمة أكثر من 5 ثوانٍ
            if now_ts - log_ts > 5:
                continue

            from_idx = parts.index("from")
            client_ip = parts[from_idx + 1].split(":")[0]

            ip_tracker[client_ip] = max(ip_tracker.get(client_ip, 0), log_ts)
        except Exception:
            continue

    # التحقق إذا كان هناك 2 IP أو أكثر
    if len(ip_tracker) >= 2:
        sorted_ips = sorted(ip_tracker.items(), key=lambda x: x[1], reverse=True)
        ip_new, ts_new = sorted_ips[0]
        ip_old, ts_old = sorted_ips[1]

        # شرط الكشف: كلا الـ IPين أرسلا طلبات في آخر ثانيتين
        if (now_ts - ts_new <= 2) and (now_ts - ts_old <= 2):
            log(
                f"🚨 Multi-IP detected! IP1: {ip_old} ({int(now_ts - ts_old)}s ago) | IP2: {ip_new} ({int(now_ts - ts_new)}s ago)"
            )
            kick_user()

            # تفريغ الـ access log حتى لا يتكرر العقاب فوراً
            with open(LOG_PATH, "w") as f:
                f.write("")


# --- التشغيل الأساسي ---
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
open(LOG_PATH, "a").close()

restart_xray()

try:
    while True:
        now = time.time()

        # 1. كشف المشاركة في حال كان الحساب غير محظور
        if user_state["is_active"]:
            detect_sharing_and_punish()
        else:
            # 2. فك الحظر إذا انتهى الوقت المحدد
            if now >= user_state["banned_until"]:
                re_add_user()

        time.sleep(0.5)
except KeyboardInterrupt:
    log("Manager stopped.")
