#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import json
import os
import re
import socket
import subprocess
import threading
import time
from urllib import parse, request

import redis

# ==============================================================================
# الإعدادات
# ==============================================================================
REDIS_URL = os.environ.get(
    "REDIS_URL", "redis://:CoraNetRedis2026SecurePass@54.86.129.233:6379/0"
)
XRAY_BIN = "/usr/local/bin/xray"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_ACCESS_LOG = "/tmp/xray_access.log"
XRAY_ERROR_LOG = "/tmp/xray_error.log"
REDIS_USERS_KEY = "users:data"

XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"
XRAY_WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1")

BLOCK_DURATION = int(os.environ.get("BLOCK_DURATION", "60"))  # مدة الحظر بالثواني
SYNC_INTERVAL = 40  # دورة فك الحظر والمزامنة مع ريديس

# التوكن الجديد المحدث
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8812248294:AAHD5aPVPSGbgtgqFUE7PDMW67kcllAZKmw"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5813081202")

ACCESS_LINE_RE = re.compile(
    r"(?:tcp:)?(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|\[?[0-9a-fA-F:]+\]?):\d+\s+accepted\s+.*?email:\s*(?P<user_id>\S+)"
)


def log(msg):
    print(f"[MANAGER] {msg}", flush=True)


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = request.Request(url, data=data)
        request.urlopen(req, timeout=5)
    except Exception as e:
        log(f"Telegram error: {e}")


# ==============================================================================
# اتصال Redis وجلب المستخدمين
# ==============================================================================
try:
    r = redis.from_url(REDIS_URL, decode_responses=True, max_connections=5)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis error: {e}")
    r = None


def get_all_users():
    if not r:
        return {}
    try:
        raw = r.hgetall(REDIS_USERS_KEY)
        users = {}
        for uid, data_json in raw.items():
            uid = uid.decode() if isinstance(uid, bytes) else uid
            data_str = (
                data_json.decode()
                if isinstance(data_json, bytes)
                else data_json
            )
            users[str(uid)] = json.loads(data_str)
        return users
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}


# ==============================================================================
# إدارة وتشغيل Xray وفحص الجاهزية
# ==============================================================================
def wait_for_port(port=5000, timeout=10):
    """التحقق المباشر من أن المنفذ مفتوح وجاهز لاستقبال الاتصالات"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def restart_xray(users, blocked_set=None):
    if blocked_set is None:
        blocked_set = set()

    clients = []
    seen_uuids = set()

    for user_id, data in users.items():
        uuid = str(data.get("uuid", "")).strip().lower()
        if (
            uuid
            and len(uuid) == 36
            and uuid not in seen_uuids
            and str(user_id) not in blocked_set
        ):
            seen_uuids.add(uuid)
            clients.append({"id": uuid, "email": str(user_id)})

    # مستخدم افتراضي لمنع انهيار Xray في حال كانت القائمة فارغة
    if not clients:
        clients = [{
            "id": "b831381d-6324-4d53-ad4f-8cda48b30811",
            "email": "fallback_keepalive",
        }]

    config = {
        "log": {
            "access": XRAY_ACCESS_LOG,
            "error": XRAY_ERROR_LOG,
            "loglevel": "warning",
        },
        "api": {"tag": "api", "services": ["HandlerService"]},
        "inbounds": [
            {
                "port": 5000,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "tag": XRAY_INBOUND_TAG,
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": XRAY_WS_PATH},
                    "sockopt": {"acceptProxyProtocol": True},
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
            {"protocol": "blackhole", "tag": "block"},
        ],
    }

    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    # قتل نظيف ودقيق باسم العملية التنفيذية فقط
    subprocess.run(["pkill", "-9", "-x", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])

    # انتظار فتح المنفذ مع تشخيص فوري للأخطاء في حال الفشل
    if wait_for_port(5000, timeout=8):
        log(f"✅ Xray is LIVE on port 5000 with {len(clients)} unique clients.")
    else:
        log("❌ Xray failed to bind port 5000 within timeout!")
        if os.path.exists(XRAY_ERROR_LOG):
            with open(XRAY_ERROR_LOG, "r") as ef:
                err_content = ef.read().strip()
                if err_content:
                    log(f"📋 [XRAY ERROR DETAILS]:\n{err_content}")


def xray_api_remove_user(user_id):
    """طرد فوري للمستخدم لحظة المخالفة دون إعادة تشغيل"""
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{user_id}"'
    subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ==============================================================================
# كشف تعدد الأجهزة الذكي مع فحص الـ 5 ثوانٍ لوضع الطيران
# ==============================================================================
user_tracker = {}
tracker_lock = threading.Lock()

blocked_users = {}  # user_id -> unblock_timestamp
blocked_lock = threading.Lock()


def kick_and_block(user_id, ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return
        blocked_users[user_id] = now + BLOCK_DURATION

    xray_api_remove_user(user_id)

    with tracker_lock:
        user_tracker.pop(user_id, None)

    safe_uid = html.escape(str(user_id))
    safe_ips = html.escape(", ".join(ips))

    log(f"🚨 Multiple devices detected! Kicked: {user_id} | IPs: {ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف: <code>{safe_uid}</code>\n"
        f"🌐 العناوين: <code>{safe_ips}</code>\n"
        f"⛔ تم الحظر المؤقت لمدة {BLOCK_DURATION} ثانية."
    )


def verify_airplane_mode(user_id, old_ip, new_ip, switch_time):
    """انتظار 5 ثوانٍ: امتصاص الحزم المتأخرة والتحقق من بقاء الجهازين معاً"""
    time.sleep(5)

    with tracker_lock:
        if user_id not in user_tracker:
            return

        state = user_tracker[user_id]
        state["verifying"] = False

        old_ip_last_seen = state["ips"].get(old_ip, 0)

        # إذا استمر الآيبي القديم في إرسال بايتات بعد ظهور الآيبي الجديد بأكثر من 1.2 ثانية
        if old_ip_last_seen > (switch_time + 1.2):
            kick_and_block(user_id, [old_ip, new_ip])
        else:
            log(f"✈️ Airplane mode confirmed for {user_id}. Switched to {new_ip}")
            state["current_ip"] = new_ip
            state["ips"].pop(old_ip, None)


def handle_new_connection(user_id, ip):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return

    with tracker_lock:
        if user_id not in user_tracker:
            user_tracker[user_id] = {
                "current_ip": ip,
                "ips": {ip: now},
                "verifying": False,
            }
            return

        state = user_tracker[user_id]
        state["ips"][ip] = now

        # تصفح طبيعي من نفس العنوان
        if ip == state["current_ip"]:
            return

        # ظهور آيبي جديد -> بدء فترة فحص وضع الطيران
        if not state["verifying"]:
            state["verifying"] = True
            old_ip = state["current_ip"]
            threading.Thread(
                target=verify_airplane_mode,
                args=(user_id, old_ip, ip, now),
                daemon=True,
            ).start()


def access_log_reader():
    while not os.path.exists(XRAY_ACCESS_LOG):
        time.sleep(0.3)

    log(f"Monitoring log file: {XRAY_ACCESS_LOG}")
    try:
        with open(XRAY_ACCESS_LOG, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            while True:
                where = f.tell()
                line = f.readline()
                if not line:
                    # حماية من تدوير الملف أو مسحه
                    if os.path.exists(XRAY_ACCESS_LOG) and os.path.getsize(XRAY_ACCESS_LOG) < where:
                        f.seek(0)
                    else:
                        time.sleep(0.1)
                        f.seek(where)
                    continue

                if "accepted" not in line or "email:" not in line:
                    continue

                m = ACCESS_LINE_RE.search(line)
                if not m:
                    continue

                ip = m.group("ip").strip("[]")
                user_id = m.group("user_id")

                if ip in ("127.0.0.1", "::1"):
                    continue

                handle_new_connection(user_id, ip)
    except Exception as exc:
        log(f"access_log_reader error: {exc}")


# ==============================================================================
# التشغيل الرئيسي
# ==============================================================================
def main():
    if os.path.exists(XRAY_ACCESS_LOG):
        try:
            os.remove(XRAY_ACCESS_LOG)
        except Exception:
            pass
    open(XRAY_ACCESS_LOG, "a").close()

    # 1. تشغيل Xray والتأكد التام من فتح المنفذ 5000 أولاً
    users = get_all_users()
    restart_xray(users)

    # 2. تشغيل proxy.py بعد أن أصبحت الخلفية جاهزة (يمنع Connection refused تماماً)
    try:
        import proxy
        threading.Thread(target=proxy.main, daemon=True).start()
        log("✅ Proxy started on port 8080.")
    except Exception as e:
        log(f"❌ Proxy start error: {e}")

    threading.Thread(target=access_log_reader, daemon=True).start()

    last_loaded_clients = {
        str(u_id): d.get("uuid")
        for u_id, d in users.items()
        if d.get("uuid")
    }

    last_sync_time = time.time()
    last_cleanup_time = time.time()

    while True:
        now = time.time()

        # تنظيف دوري للذاكرة كل دقيقتين لمنع تسريب العناوين القديمة
        if now - last_cleanup_time >= 120:
            with tracker_lock:
                for uid in list(user_tracker.keys()):
                    ips_dict = user_tracker[uid]["ips"]
                    for ip_addr, t_stamp in list(ips_dict.items()):
                        if now - t_stamp > 180:
                            del ips_dict[ip_addr]
                    if not ips_dict:
                        del user_tracker[uid]
            last_cleanup_time = now

        # دورة المزامنة وفك الحظر كل 40 ثانية
        if now - last_sync_time >= SYNC_INTERVAL:
            try:
                fresh_users = get_all_users()
                if fresh_users:
                    users = fresh_users

                unblocked_users = []
                with blocked_lock:
                    for uid, unblock_time in list(blocked_users.items()):
                        if now >= unblock_time:
                            unblocked_users.append(uid)
                            del blocked_users[uid]

                    currently_blocked = set(str(k) for k in blocked_users)

                target_clients = {
                    str(u_id): data.get("uuid")
                    for u_id, data in users.items()
                    if data.get("uuid") and str(u_id) not in currently_blocked
                }

                # إعادة تشغيل Xray فقط عند وجود تحديث حقيقي (مستخدم جديد أو فك حظر)
                if target_clients != last_loaded_clients or unblocked_users:
                    log("Sync cycle (40s): Reloading Xray for state updates...")
                    restart_xray(users, blocked_set=currently_blocked)
                    last_loaded_clients = target_clients

                    with tracker_lock:
                        for uid in unblocked_users:
                            user_tracker.pop(uid, None)

                    for uid in unblocked_users:
                        safe_u = html.escape(str(uid))
                        log(f"✅ Unblocked: {uid}")
                        send_telegram(
                            f"✅ <b>انتهى الحظر المؤقت:</b>\n<code>{safe_u}</code>\n"
                            f"🚀 يمكنك معاودة الاتصال الآن بأمان."
                        )

                last_sync_time = now
            except Exception as e:
                log(f"❌ Sync cycle error: {e}")
                last_sync_time = now

        time.sleep(0.5)


if __name__ == "__main__":
    main()
