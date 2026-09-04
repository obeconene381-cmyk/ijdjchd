#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from urllib import parse, request

import redis

# ==============================================================================
# الإعدادات
# ==============================================================================
REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://:CoraNetRedis2026SecurePass@54.86.129.233:6379/0",
)
XRAY_BIN = "/usr/local/bin/xray"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_ACCESS_LOG = "/var/log/xray/access.log"
XRAY_ERROR_LOG = "/var/log/xray/error.log"
REDIS_USERS_KEY = "users:data"
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"

BLOCK_DURATION = int(os.environ.get("BLOCK_DURATION", "60"))  # مدة الحظر بالثواني
IP_TTL_SECONDS = int(os.environ.get("IP_TTL_SECONDS", "120"))  # صلاحية وجود الـ IP

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8812248294:AAHbQnTWwkkneggwN8G8yTg_1HyYoy95S5I"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5813081202")

# تعبير نمطي صحيح يطابق سطر Xray الحقيقي بدقة
ACCESS_LINE_RE = re.compile(
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|\[?[0-9a-fA-F:]+\]?):\d+\s+accepted\s+.*?email:\s*(?P<email>\S+)"
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
# اتصال Redis و Lua Script
# ==============================================================================
try:
    r = redis.from_url(REDIS_URL, decode_responses=True, max_connections=5)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis error: {e}")
    r = None

DEDUCT_BYTES_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if raw then
    local data = cjson.decode(raw)
    local old_quota = tonumber(data["quota_bytes"]) or 0
    local used = tonumber(ARGV[2]) or 0
    local new_quota = old_quota - used
    if new_quota < 0 then new_quota = 0 end
    data["quota_bytes"] = new_quota
    redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(data))
    return new_quota
end
return -1
"""
deduct_script = r.register_script(DEDUCT_BYTES_LUA) if r else None


def atomic_deduct_user_bytes(email, bytes_used):
    if not deduct_script:
        return -1
    try:
        return int(
            deduct_script(
                keys=[REDIS_USERS_KEY], args=[str(email), str(bytes_used)]
            )
        )
    except Exception as e:
        log(f"❌ Error in atomic deduct for {email}: {e}")
        return -1


def get_all_users():
    if not r:
        return {}
    try:
        raw = r.hgetall(REDIS_USERS_KEY)
        users = {}
        for email, data_str in raw.items():
            users[email] = json.loads(data_str)
        return users
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}


# ==============================================================================
# إعداد وبناء Xray (استرجاع إعداداتك الأصلية مع تفعيل PROXY protocol)
# ==============================================================================
def restart_xray(users, blocked_set=None):
    if blocked_set is None:
        blocked_set = set()

    clients = [
        {"id": data["uuid"], "email": email}
        for email, data in users.items()
        if data.get("quota_bytes", 0) > 0 and email not in blocked_set
    ]

    config = {
        "log": {
            "access": XRAY_ACCESS_LOG,
            "error": XRAY_ERROR_LOG,
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
                "tag": XRAY_INBOUND_TAG,
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/@pycorav1"},
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

    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])
    log("Xray restarted.")


def xray_api_remove_user(email):
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{email}"'
    res = subprocess.run(
        cmd,
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


def xray_api_add_user(email, uuid):
    user_config = {
        "inbounds": [
            {
                "tag": XRAY_INBOUND_TAG,
                "protocol": "vless",
                "settings": {
                    "decryption": "none",
                    "clients": [{"id": uuid, "email": email}],
                },
            }
        ]
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(user_config, f)
        tmp = f.name
    try:
        cmd = [XRAY_BIN, "api", "adu", f"--server={XRAY_API_SERVER}", tmp]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ==============================================================================
# كشف العناوين المتعددة (Anti Account-Sharing)
# ==============================================================================
ips_seen = defaultdict(dict)  # email -> {ip: timestamp}
ips_lock = threading.Lock()
blocked_users = {}  # email -> unblock_timestamp
blocked_lock = threading.Lock()


def kick_and_block(email, ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(email, 0):
            return
        blocked_users[email] = now + BLOCK_DURATION

    # قطع الاتصال مباشرة بنفس الأمر المجرّب لديك
    xray_api_remove_user(email)

    with ips_lock:
        ips_seen.pop(email, None)

    log(f"🚨 Kicked and blocked {email} for {BLOCK_DURATION}s. IPs: {ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المستخدم: <code>{email}</code>\n"
        f"🌐 عدد الأجهزة: {len(ips)}\n"
        f"📋 العناوين: <code>{', '.join(ips)}</code>\n"
        f"⛔ تم قطع الاتصال والحظر لمدة {BLOCK_DURATION} ثانية."
    )


def handle_new_connection(email, ip):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(email, 0):
            return

    with ips_lock:
        table = ips_seen[email]
        # حذف العناوين التي تجاوزت مدة الـ TTL
        for old_ip in list(table.keys()):
            if now - table[old_ip] > IP_TTL_SECONDS:
                del table[old_ip]

        table[ip] = now
        active_ips = list(table.keys())

    # إذا تم تسجيل أكثر من آيبي واحد مختلف خلال نافذة الـ TTL
    if len(active_ips) > 1:
        kick_and_block(email, active_ips)


def access_log_reader():
    """قراءة مباشرة وسريعة للسجل بدون تأخير buffer"""
    while not os.path.exists(XRAY_ACCESS_LOG):
        time.sleep(0.5)

    log(f"Monitoring {XRAY_ACCESS_LOG} for multi-IP connections...")
    with open(XRAY_ACCESS_LOG, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue

            if "accepted" not in line or "email:" not in line:
                continue

            m = ACCESS_LINE_RE.search(line)
            if not m:
                continue

            ip = m.group("ip").strip("[]")
            email = m.group("email")

            if ip in ("127.0.0.1", "::1"):
                continue

            handle_new_connection(email, ip)


def unblock_worker():
    """إرجاع المستخدم بعد انقضاء مدة الحظر المؤقت"""
    while True:
        now = time.time()
        to_unblock = []
        with blocked_lock:
            for email, unblock_time in list(blocked_users.items()):
                if now >= unblock_time:
                    to_unblock.append(email)
                    del blocked_users[email]

        if to_unblock:
            current_users = get_all_users()
            for email in to_unblock:
                user = current_users.get(email)
                if user and user.get("quota_bytes", 0) > 0:
                    uuid = user.get("uuid")
                    if uuid and xray_api_add_user(email, uuid):
                        log(f"✅ Unblocked: {email}")
                        send_telegram(
                            f"✅ انتهى الحظر المؤقت للمستخدم <code>{email}</code> وتمت إعادته للخدمة."
                        )
        time.sleep(2)


# ==============================================================================
# جلب الترافيك
# ==============================================================================
def get_user_traffic():
    cmd = [
        XRAY_BIN,
        "api",
        "statsquery",
        f"--server={XRAY_API_SERVER}",
        "-pattern",
        "user",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)
        traffic = {}
        for item in data.get("stat", []):
            name = item["name"]
            val = int(item["value"])
            if "user>>>" in name and ">>>traffic>>>" in name:
                email = name.split(">>>")[1]
                traffic[email] = traffic.get(email, 0) + val
        return traffic
    except Exception:
        return None


# ==============================================================================
# التشغيل الرئيسي
# ==============================================================================
def main():
    os.makedirs("/var/log/xray", exist_ok=True)
    open(XRAY_ACCESS_LOG, "a").close()

    # تشغيل proxy.py في خلفية نفس الحاوية
    try:
        import proxy

        threading.Thread(target=proxy.main, daemon=True).start()
        log("✅ TCP Proxy started on port 8080.")
    except Exception as e:
        log(f"Proxy start error: {e}")

    users = get_all_users()
    restart_xray(users)
    time.sleep(1)

    # تشغيل خيوط مراقبة السجل وفك الحظر
    threading.Thread(target=access_log_reader, daemon=True).start()
    threading.Thread(target=unblock_worker, daemon=True).start()

    last_stats = get_user_traffic() or {}
    last_active = {
        e for e, d in users.items() if d.get("quota_bytes", 0) > 0
    }
    last_sync_time = time.time()
    last_quota_check = time.time()

    while True:
        now = time.time()

        # خصم الكوتا كل 3 ثوانٍ
        if now - last_quota_check >= 3:
            current_stats = get_user_traffic()
            if current_stats is not None:
                for email in list(users.keys()):
                    cur = current_stats.get(email, 0)
                    prev = last_stats.get(email, 0)
                    used = cur - prev
                    if used < 0:
                        used = cur

                    if used > 0:
                        new_quota = atomic_deduct_user_bytes(email, used)
                        if new_quota >= 0:
                            if email in users:
                                users[email]["quota_bytes"] = new_quota
                            if new_quota <= 0:
                                xray_api_remove_user(email)
                                with ips_lock:
                                    ips_seen.pop(email, None)
                                log(f"🚫 Quota finished: {email}")
                last_stats = current_stats
            last_quota_check = now

        # مزامنة المستخدمين الجدد كل 20 ثانية مع احترام قائمة المحظورين
        if now - last_sync_time >= 20:
            try:
                users = get_all_users()
                current_active = {
                    e for e, d in users.items() if d.get("quota_bytes", 0) > 0
                }
                with blocked_lock:
                    currently_blocked = set(blocked_users.keys())

                if current_active - last_active:
                    log("New users detected, updating Xray...")
                    restart_xray(users, blocked_set=currently_blocked)
                    time.sleep(1)
                    last_stats = get_user_traffic() or last_stats

                last_active = current_active
                last_sync_time = now
            except Exception as e:
                log(f"❌ Sync error: {e}")

        time.sleep(0.5)


if __name__ == "__main__":
    main()
