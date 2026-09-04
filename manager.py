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
    "REDIS_URL", "redis://:CoraNetRedis2026SecurePass@54.86.129.233:6379/0"
)
XRAY_BIN = "/usr/local/bin/xray"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_ACCESS_LOG = "/var/log/xray/access.log"
XRAY_ERROR_LOG = "/var/log/xray/error.log"
REDIS_USERS_KEY = "users:data"

XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"
XRAY_WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1")

BLOCK_DURATION = int(os.environ.get("BLOCK_DURATION", "60"))
IP_TTL_SECONDS = int(os.environ.get("IP_TTL_SECONDS", "90"))

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8812248294:AAHbQnTWwkkneggwN8G8yTg_1HyYoy95S5I"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5813081202")

# نمط يطابق سجل الدخول في Xray مع استخراج IP ومعرف الحساب بدقة
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
# اتصال Redis
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


def atomic_deduct_user_bytes(user_id, bytes_used):
    if not deduct_script:
        return -1
    try:
        return int(
            deduct_script(
                keys=[REDIS_USERS_KEY], args=[str(user_id), str(bytes_used)]
            )
        )
    except Exception as e:
        log(f"❌ Deduct error for {user_id}: {e}")
        return -1


def get_all_users():
    if not r:
        return {}
    try:
        raw = r.hgetall(REDIS_USERS_KEY)
        return {k: json.loads(v) for k, v in raw.items()}
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}


# ==============================================================================
# إدارة وتشغيل Xray
# ==============================================================================
def restart_xray(users, blocked_set=None):
    if blocked_set is None:
        blocked_set = set()

    clients = [
        {"id": data["uuid"], "email": str(user_id)}
        for user_id, data in users.items()
        if data.get("quota_bytes", 0) > 0
        and data.get("uuid")
        and str(user_id) not in blocked_set
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

    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])
    log("Xray restarted.")


def xray_api_remove_user(user_id):
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{user_id}"'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        log(f"API rmu output: {res.stderr.strip() or res.stdout.strip()}")
    return res.returncode == 0


def xray_api_add_user(user_id, uuid):
    payload = {
        "inbounds": [
            {
                "tag": XRAY_INBOUND_TAG,
                "protocol": "vless",
                "settings": {
                    "decryption": "none",
                    "clients": [{"id": uuid, "email": str(user_id)}],
                },
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        tmp = f.name
    try:
        cmd = [XRAY_BIN, "api", "adu", f"--server={XRAY_API_SERVER}", tmp]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except Exception as e:
        log(f"API adu exception: {e}")
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ==============================================================================
# كشف العناوين المتعددة (Anti Account-Sharing)
# ==============================================================================
ips_seen = defaultdict(dict)  # user_id -> {ip: timestamp}
ips_lock = threading.Lock()
blocked_users = {}  # user_id -> unblock_timestamp
blocked_lock = threading.Lock()


def kick_and_block(user_id, ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return
        blocked_users[user_id] = now + BLOCK_DURATION

    # قطع الاتصال فوراً
    xray_api_remove_user(user_id)

    with ips_lock:
        ips_seen.pop(user_id, None)

    log(f"🚨 Sharing detected! Kicked: {user_id} | IPs: {ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف (ID): <code>{user_id}</code>\n"
        f"🌐 عدد الأجهزة: {len(ips)}\n"
        f"📋 العناوين: <code>{', '.join(ips)}</code>\n"
        f"⛔ تم قطع الاتصال والحظر لمدة {BLOCK_DURATION} ثانية."
    )


def handle_new_connection(user_id, ip):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return

    with ips_lock:
        table = ips_seen[user_id]
        for old_ip in list(table.keys()):
            if now - table[old_ip] > IP_TTL_SECONDS:
                del table[old_ip]

        table[ip] = now
        active_ips = list(table.keys())

    if len(active_ips) > 1:
        kick_and_block(user_id, active_ips)


def access_log_reader():
    """قراءة مباشرة وسريعة للسجل بدون تأخير buffer"""
    while not os.path.exists(XRAY_ACCESS_LOG):
        time.sleep(0.5)

    log(f"Monitoring log: {XRAY_ACCESS_LOG}")
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
            user_id = m.group("user_id")

            if ip in ("127.0.0.1", "::1"):
                continue

            handle_new_connection(user_id, ip)


def unblock_worker():
    """فك الحظر وإرجاع المستخدم فور انتهاء المدة"""
    while True:
        now = time.time()
        to_unblock = []
        with blocked_lock:
            for user_id, unblock_time in list(blocked_users.items()):
                if now >= unblock_time:
                    to_unblock.append(user_id)
                    del blocked_users[user_id]

        if to_unblock:
            current_users = get_all_users()
            for user_id in to_unblock:
                udata = current_users.get(user_id)
                if udata and udata.get("quota_bytes", 0) > 0:
                    uuid = udata.get("uuid")
                    if uuid:
                        # محاولة الإضافة عبر API أولاً
                        success = xray_api_add_user(user_id, uuid)
                        if not success:
                            # إعادة تشغيل سريعة كحل احتياطي لضمان عودة المستخدم
                            with blocked_lock:
                                current_b = set(str(k) for k in blocked_users)
                            restart_xray(current_users, blocked_set=current_b)
                        log(f"✅ Unblocked: {user_id}")
                        send_telegram(
                            f"✅ انتهى الحظر المؤقت وتمت إعادة تفعيل الحساب:\n<code>{user_id}</code>"
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
                u_id = name.split(">>>")[1]
                traffic[u_id] = traffic.get(u_id, 0) + val
        return traffic
    except Exception:
        return None


# ==============================================================================
# التشغيل الرئيسي
# ==============================================================================
def main():
    os.makedirs("/var/log/xray", exist_ok=True)
    open(XRAY_ACCESS_LOG, "a").close()

    try:
        import proxy

        threading.Thread(target=proxy.main, daemon=True).start()
        log("✅ Proxy started on port 8080.")
    except Exception as e:
        log(f"Proxy start error: {e}")

    users = get_all_users()
    restart_xray(users)
    time.sleep(1)

    threading.Thread(target=access_log_reader, daemon=True).start()
    threading.Thread(target=unblock_worker, daemon=True).start()

    last_stats = get_user_traffic() or {}
    last_active = {
        str(k) for k, d in users.items() if d.get("quota_bytes", 0) > 0
    }
    last_sync_time = time.time()
    last_quota_check = time.time()

    while True:
        now = time.time()

        # خصم الكوتا كل 3 ثوانٍ
        if now - last_quota_check >= 3:
            current_stats = get_user_traffic()
            if current_stats is not None:
                for user_id, udata in list(users.items()):
                    uid_str = str(user_id)
                    cur = current_stats.get(uid_str, 0)
                    prev = last_stats.get(uid_str, 0)
                    used = cur - prev
                    if used < 0:
                        used = cur

                    if used > 0:
                        new_quota = atomic_deduct_user_bytes(user_id, used)
                        if new_quota >= 0:
                            udata["quota_bytes"] = new_quota
                            if new_quota <= 0:
                                xray_api_remove_user(uid_str)
                                with ips_lock:
                                    ips_seen.pop(uid_str, None)
                                log(f"🚫 Quota finished: {uid_str}")
                                send_telegram(
                                    f"🚫 نفدت كوتا الحساب: <code>{uid_str}</code>"
                                )
                last_stats = current_stats
            last_quota_check = now

        # مزامنة المستخدمين الجدد من Redis كل 20 ثانية
        if now - last_sync_time >= 20:
            try:
                users = get_all_users()
                current_active = {
                    str(k)
                    for k, d in users.items()
                    if d.get("quota_bytes", 0) > 0
                }

                with blocked_lock:
                    currently_blocked = set(str(k) for k in blocked_users)

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
