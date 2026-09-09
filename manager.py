#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
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
IP_TTL_SECONDS = int(os.environ.get("IP_TTL_SECONDS", "12"))  # نافذة نشاط الشبكة

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8468334139:AAHTCT7WkqvkXiipJaLOTWUD-zfNRjUTur4"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5813081202")

# نمط استخراج التوقيت الفعلي والآيبي ومعرف المستخدم من سجلات Xray
ACCESS_LINE_RE = re.compile(
    r"(?:(?P<log_time>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s+)?(?:tcp:)?(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|\[?[0-9a-fA-F:]+\]?):\d+\s+accepted\s+.*?email:\s*(?P<user_id>\S+)"
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


def parse_log_time(time_str):
    """تحويل توقيت سطر السجل إلى Unix Timestamp"""
    if not time_str:
        return time.time()
    try:
        return time.mktime(time.strptime(time_str, "%Y/%m/%d %H:%M:%S"))
    except Exception:
        return time.time()


def get_network_prefix(ip_str):
    """
    اقتطاع موحد لجميع الشبكات:
    - IPv4: أول رقمين فقط (A.B)
    - IPv6: أول خانتين فقط (X:Y)
    """
    try:
        clean_ip = ip_str.strip("[]")

        # معالجة IPv4
        if "." in clean_ip:
            parts = clean_ip.split(".")
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}"

        # معالجة IPv6
        if ":" in clean_ip:
            parts = [p for p in clean_ip.split(":") if p]
            if len(parts) >= 2:
                return f"{parts[0]}:{parts[1]}"

        return clean_ip
    except Exception:
        return ip_str


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
                data_json.decode() if isinstance(data_json, bytes) else data_json
            )
            users[str(uid)] = json.loads(data_str)
        return users
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}


# ==============================================================================
# إدارة Xray عبر gRPC وسرعة الإقلاع
# ==============================================================================
def restart_xray(users, blocked_set=None):
    """إعادة تشغيل Xray الأولية السريعة دون فحص منافذ معطل"""
    if blocked_set is None:
        blocked_set = set()

    clients = []
    seen_uuids = set()

    for user_id, data in users.items():
        uuid_str = str(data.get("uuid", "")).strip().lower()
        if (
            uuid_str
            and len(uuid_str) == 36
            and uuid_str not in seen_uuids
            and str(user_id) not in blocked_set
        ):
            seen_uuids.add(uuid_str)
            clients.append({"id": uuid_str, "email": str(user_id)})

    if not clients:
        clients = [{"id": "b831381d-6324-4d53-ad4f-8cda48b30811", "email": "fallback_keepalive"}]

    config = {
        "log": {"access": XRAY_ACCESS_LOG, "error": XRAY_ERROR_LOG, "loglevel": "warning"},
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
        "routing": {"rules": [{"inboundTag": ["api-inbound"], "outboundTag": "api", "type": "field"}]},
        "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}],
    }

    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    subprocess.run(["pkill", "-9", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])
    log(f"⚡ Xray cleanly loaded with {len(clients)} clients.")


def xray_api_remove_user(user_id):
    """طرد المستخدم فوراً من الذاكرة الحية عبر gRPC"""
    cmd = [
        XRAY_BIN, "api", "rmu",
        f"--server={XRAY_API_SERVER}",
        f"-tag={XRAY_INBOUND_TAG}",
        str(user_id)
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def xray_api_add_user(user_id, uuid_str):
    """إعادة إضافة المستخدم للذاكرة الحية فوراً عبر gRPC دون إعادة تشغيل السيرفر"""
    client_spec = json.dumps({
        "email": str(user_id),
        "id": str(uuid_str).strip().lower(),
        "level": 0
    })
    cmd = [
        XRAY_BIN, "api", "adu",
        f"--server={XRAY_API_SERVER}",
        f"-tag={XRAY_INBOUND_TAG}",
        client_spec
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


# ==============================================================================
# كشف العناوين المتعددة ومراقبة النشاط أثناء الحظر
# ==============================================================================
ips_seen = defaultdict(dict)
ips_lock = threading.Lock()

# user_id -> {"unblock_at": float, "attempts_during_ban": int, "last_attempt_ip": str}
blocked_users = {}
blocked_lock = threading.Lock()


def kick_and_block(user_id, active_prefixes, trigger_time):
    with blocked_lock:
        if trigger_time < blocked_users.get(user_id, {}).get("unblock_at", 0):
            return
        blocked_users[user_id] = {
            "unblock_at": trigger_time + BLOCK_DURATION,
            "attempts_during_ban": 0,
            "last_attempt_ip": None
        }

    # إزالة المعرف فوراً من Xray
    xray_api_remove_user(user_id)

    with ips_lock:
        ips_seen.pop(user_id, None)

    log(f"🚨 Sharing detected! Kicked: {user_id} | Prefixes: {active_prefixes}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف (ID): <code>{user_id}</code>\n"
        f"🌐 عدد الأجهزة: {len(active_prefixes)}\n"
        f"📋 الشبكات: <code>{', '.join(active_prefixes)}</code>\n"
        f"⛔ تم قطع الاتصال وحظر الحساب مؤقتاً لمدة {BLOCK_DURATION} ثانية."
    )


def handle_new_connection(user_id, ip, log_ts):
    now = time.time()

    # 1. إهمال الأسطر القديمة في السجل (أكثر من 5 ثوانٍ)
    if (now - log_ts) > 5.0:
        return

    # 2. فحص محاولات الاتصال أثناء سريان الحظر
    with blocked_lock:
        if user_id in blocked_users:
            b_info = blocked_users[user_id]
            if log_ts < b_info["unblock_at"]:
                b_info["attempts_during_ban"] += 1
                b_info["last_attempt_ip"] = ip
                log(f"⚠️ [BAN ACTIVITY] User {user_id} requested connection DURING ban (Attempt #{b_info['attempts_during_ban']}) from IP: {ip}")
                return

    prefix = get_network_prefix(ip)

    # 3. تتبع وتحديث الشبكات النشطة خلال نافذة الوقت (IP_TTL_SECONDS)
    with ips_lock:
        table = ips_seen[user_id]

        for old_prefix in list(table.keys()):
            if log_ts - table[old_prefix] > IP_TTL_SECONDS:
                del table[old_prefix]

        table[prefix] = log_ts
        active_prefixes = list(table.keys())

    # إذا ظهرت شبكتان مختلفتان تماماً في نفس النافذة يتم الحظر
    if len(active_prefixes) > 1:
        kick_and_block(user_id, active_prefixes, log_ts)


def access_log_reader():
    """قراءة مستمرة للأمام فقط بدون تصفير أو تراجع للمؤشر"""
    while not os.path.exists(XRAY_ACCESS_LOG):
        time.sleep(0.3)

    log(f"Monitoring log file: {XRAY_ACCESS_LOG}")
    try:
        with open(XRAY_ACCESS_LOG, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.05)
                    continue

                if "accepted" not in line or "email:" not in line:
                    continue

                m = ACCESS_LINE_RE.search(line)
                if not m:
                    continue

                ip = m.group("ip").strip("[]")
                user_id = m.group("user_id")
                log_time_raw = m.group("log_time")

                if ip in ("127.0.0.1", "::1"):
                    continue

                log_ts = parse_log_time(log_time_raw)
                handle_new_connection(user_id, ip, log_ts)
    except Exception as exc:
        log(f"access_log_reader error: {exc}")
        time.sleep(0.5)


def unblock_worker():
    """فك الحظر لحظياً عبر gRPC وإشعار المستخدم بحالة نشاطه"""
    while True:
        now = time.time()
        to_unblock = []

        with blocked_lock:
            for user_id, info in list(blocked_users.items()):
                if now >= info["unblock_at"]:
                    to_unblock.append((user_id, info["attempts_during_ban"]))
                    del blocked_users[user_id]

        if to_unblock:
            users = get_all_users()
            for user_id, attempts in to_unblock:
                uuid_str = users.get(str(user_id), {}).get("uuid")
                if uuid_str:
                    # إعادة تفعيل الحساب ديناميكياً بدون ريستارت
                    xray_api_add_user(user_id, uuid_str)

                with ips_lock:
                    ips_seen.pop(user_id, None)

                if attempts > 0:
                    status_note = f"⚠️ تم تسجيل <b>{attempts}</b> محاولة اتصال أثناء فترة الحظر."
                else:
                    status_note = "🟢 لم تُسجل أي محاولات اتصال أثناء الحظر."

                log(f"✅ Unblocked via gRPC: {user_id} | Attempts: {attempts}")
                send_telegram(
                    f"✅ <b>انتهى الحظر المؤقت للحساب:</b>\n<code>{user_id}</code>\n"
                    f"{status_note}\n\n"
                    f"🔄 تمت استعادة الخدمة الآن."
                )

        time.sleep(1)


# ==============================================================================
# التشغيل الرئيسي
# ==============================================================================
def main():
    if not os.path.exists(XRAY_ACCESS_LOG):
        open(XRAY_ACCESS_LOG, "a").close()

    users = get_all_users()
    restart_xray(users)

    try:
        import proxy
        threading.Thread(target=proxy.main, daemon=True).start()
        log("✅ Proxy started on port 8080.")
    except Exception as e:
        log(f"Proxy start error: {e}")

    threading.Thread(target=access_log_reader, daemon=True).start()
    threading.Thread(target=unblock_worker, daemon=True).start()

    last_user_count = len(users)

    while True:
        time.sleep(60)
        fresh_users = get_all_users()
        if len(fresh_users) > last_user_count:
            last_user_count = len(fresh_users)
            with blocked_lock:
                currently_blocked = set(str(k) for k in blocked_users)
            restart_xray(fresh_users, blocked_set=currently_blocked)


if __name__ == "__main__":
    main()
