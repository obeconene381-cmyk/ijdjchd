#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import socket
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

BLOCK_DURATION = int(os.environ.get("BLOCK_DURATION", "40"))  # مدة الحظر بالثواني
IP_TTL_SECONDS = int(os.environ.get("IP_TTL_SECONDS", "30"))  # نافذة نشاط الآيبي
SYNC_INTERVAL = 40  # دورة المزامنة وإعادة التشغيل (كل 40 ثانية)

TELEGRAM_BOT_TOKEN = "8812248294:AAHD5aPVPSGbgtgqFUE7PDMW67kcllAZKmw"
TELEGRAM_CHAT_ID = "5813081202"

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
# إدارة Xray
# ==============================================================================
def wait_for_port(port=5000, timeout=10):
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

    subprocess.run(["pkill", "-9", "-x", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])

    if wait_for_port(5000, timeout=8):
        log(f"✅ Xray restarted cleanly with {len(clients)} active clients.")
    else:
        log("❌ Xray failed to bind port 5000!")


def xray_api_remove_user(user_id):
    """طرد فوري للمستخدم لحظة المخالفة دون إعادة تشغيل"""
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{user_id}"'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode == 0


# ==============================================================================
# كشف العناوين المتعددة الأصلي (Anti Account-Sharing)
# ==============================================================================
ips_seen = defaultdict(dict)
ips_lock = threading.Lock()
blocked_users = {}
blocked_lock = threading.Lock()


def kick_and_block(user_id, ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return
        blocked_users[user_id] = now + BLOCK_DURATION

    # طرد فوري للمستخدم عبر API دون إعادة تشغيل Xray
    xray_api_remove_user(user_id)

    with ips_lock:
        ips_seen.pop(user_id, None)

    log(f"🚨 Sharing detected! Kicked: {user_id} | IPs: {ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف (ID): <code>{user_id}</code>\n"
        f"🌐 عدد الأجهزة: {len(ips)}\n"
        f"📋 العناوين: <code>{', '.join(ips)}</code>\n"
        f"⛔ تم قطع الاتصال فوراً وحظر الحساب مؤقتاً."
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


def unblock_worker():
    """مراقبة انتهاء وقت الحظر وإشعار المستخدم فقط"""
    while True:
        now = time.time()
        to_unblock = []
        with blocked_lock:
            for user_id, unblock_time in list(blocked_users.items()):
                if now >= unblock_time:
                    to_unblock.append(user_id)
                    del blocked_users[user_id]

        for user_id in to_unblock:
            log(f"✅ Unblocked: {user_id} (queued for next reload)")
            send_telegram(
                f"✅ <b>انتهى الحظر المؤقت للحساب:</b>\n<code>{user_id}</code>\n"
                f"🔄 ستتم استعادة الاتصال تلقائياً خلال ثوانٍ."
            )
        time.sleep(1)


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

    last_loaded_clients = {
        str(u_id): d.get("uuid")
        for u_id, d in users.items()
        if d.get("uuid")
    }

    last_sync_time = time.time()

    while True:
        now = time.time()

        # دورة المزامنة وإعادة التشغيل (كل 40 ثانية)
        if now - last_sync_time >= SYNC_INTERVAL:
            try:
                users = get_all_users()
                with blocked_lock:
                    currently_blocked = set(str(k) for k in blocked_users)

                target_clients = {
                    str(u_id): data.get("uuid")
                    for u_id, data in users.items()
                    if data.get("uuid") and str(u_id) not in currently_blocked
                }

                if target_clients != last_loaded_clients:
                    log("Sync cycle (40s): Changes detected, reloading Xray once...")
                    restart_xray(users, blocked_set=currently_blocked)
                    last_loaded_clients = target_clients
                else:
                    log("Sync cycle (40s): No client changes.")

                last_sync_time = now
            except Exception as e:
                log(f"❌ Sync cycle error: {e}")
                last_sync_time = now

        time.sleep(0.5)


if __name__ == "__main__":
    main()
