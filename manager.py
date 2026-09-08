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

import grpc
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

BLOCK_DURATION = 40  # مدة الحظر المؤقت بالثواني

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
# اتصال Redis
# ==============================================================================
try:
    r = redis.from_url(REDIS_URL, decode_responses=True, max_connections=5)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis connection error: {e}")
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
# تشفير وسائط Protobuf لإضافة المستخدم عبر gRPC (بدون ملفات مسبقة)
# ==============================================================================
def _encode_varint(value):
    bits = value & 0x7F
    value >>= 7
    ret = bytearray()
    while value:
        ret.append(0x80 | bits)
        bits = value & 0x7F
        value >>= 7
    ret.append(bits)
    return bytes(ret)


def _encode_tag(field_number, wire_type):
    return _encode_varint((field_number << 3) | wire_type)


def _encode_string(field_number, s):
    b = s.encode("utf-8")
    return _encode_tag(field_number, 2) + _encode_varint(len(b)) + b


def _encode_bytes(field_number, b):
    return _encode_tag(field_number, 2) + _encode_varint(len(b)) + b


def _encode_uint32(field_number, val):
    return _encode_tag(field_number, 0) + _encode_varint(val)


def _encode_any(type_name, message_bytes):
    type_url = f"type.googleapis.com/{type_name}"
    return _encode_string(1, type_url) + _encode_bytes(2, message_bytes)


# إنشاء قناة اتصال gRPC دائمة وسريعة
grpc_channel = grpc.insecure_channel(XRAY_API_SERVER)
grpc_alter_inbound = grpc_channel.unary_unary(
    "/xray.app.proxyman.command.HandlerService/AlterInbound",
    request_serializer=lambda x: x,
    response_deserializer=lambda x: x,
)


def xray_api_add_user(user_uuid, user_id):
    """إضافة المستخدم فورياً في الذاكرة دون إعادة تشغيل Xray"""
    try:
        # 1. بناء رسالة VLESS Account
        acc_bytes = (
            _encode_string(1, str(user_uuid))
            + _encode_string(2, "")
            + _encode_string(3, "none")
        )
        any_account = _encode_any("xray.proxy.vless.Account", acc_bytes)

        # 2. بناء رسالة User
        user_bytes = (
            _encode_uint32(1, 0)
            + _encode_string(2, str(user_id))
            + _encode_bytes(3, any_account)
        )

        # 3. بناء عملية AddUserOperation
        op_bytes = _encode_bytes(1, user_bytes)
        any_op = _encode_any(
            "xray.app.proxyman.command.AddUserOperation", op_bytes
        )

        # 4. بناء طلب AlterInboundRequest
        req_bytes = _encode_string(1, XRAY_INBOUND_TAG) + _encode_bytes(2, any_op)

        grpc_alter_inbound(req_bytes, timeout=3)
        log(f"⚡ Hot-Added user via gRPC: {user_id} ({user_uuid})")
        return True
    except Exception as e:
        log(f"❌ Failed to Hot-Add user {user_id}: {e}")
        return False


def xray_api_remove_user(user_id):
    """طرد المستخدم فوراً عبر الـ API"""
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{user_id}"'
    subprocess.run(
        cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ==============================================================================
# إقلاع Xray لأول مرة فقط
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


def start_xray_once(users):
    clients = []
    seen_uuids = set()

    for user_id, data in users.items():
        uuid_str = str(data.get("uuid", "")).strip().lower()
        if uuid_str and len(uuid_str) == 36 and uuid_str not in seen_uuids:
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
    time.sleep(0.5)

    subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])

    if wait_for_port(5000, timeout=8):
        log(f"✅ Xray started once with {len(clients)} clients. No reboots needed!")
    else:
        log("❌ Xray failed to bind port 5000!")


# ==============================================================================
# كشف تعدد الأجهزة والطرد
# ==============================================================================
user_ips = {}
tracker_lock = threading.Lock()

blocked_users = {}  # user_id -> unblock_timestamp
blocked_lock = threading.Lock()


def kick_and_block(user_id, ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return
        blocked_users[user_id] = now + BLOCK_DURATION

    # طرد فوري للمخالف من الذاكرة
    xray_api_remove_user(user_id)

    with tracker_lock:
        user_ips.pop(user_id, None)

    safe_uid = html.escape(str(user_id))
    safe_ips = html.escape(", ".join(ips))

    log(f"🚨 Multi-device detected! Kicked: {user_id} | IPs: {ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف: <code>{safe_uid}</code>\n"
        f"🌐 العناوين: <code>{safe_ips}</code>\n"
        f"⛔ تم الطرد المؤقت لمدة {BLOCK_DURATION} ثانية."
    )


def handle_new_connection(user_id, ip):
    now = time.time()

    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return

    with tracker_lock:
        if user_id not in user_ips:
            user_ips[user_id] = {"ip": ip, "last_seen": now}
            return

        current_ip = user_ips[user_id]["ip"]

        if ip == current_ip:
            user_ips[user_id]["last_seen"] = now
            return

        # رصد آيبي ثانٍ -> طرد مباشر
        kick_and_block(user_id, [current_ip, ip])


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
                    if os.path.exists(XRAY_ACCESS_LOG) and os.path.getsize(XRAY_ACCESS_LOG) < where:
                        f.seek(0)
                    else:
                        time.sleep(0.05)
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

    # 1. إقلاع Xray لأول مرة فقط
    users = get_all_users()
    start_xray_once(users)

    # 2. تشغيل proxy.py
    try:
        import proxy
        threading.Thread(target=proxy.main, daemon=True).start()
        log("✅ Proxy started on port 8080.")
    except Exception as e:
        log(f"❌ Proxy start error: {e}")

    threading.Thread(target=access_log_reader, daemon=True).start()

    known_user_ids = set(users.keys())
    last_cleanup_time = time.time()

    while True:
        now = time.time()

        # تنظيف الآيبيهات الخاملة كل 3 دقائق
        if now - last_cleanup_time >= 180:
            with tracker_lock:
                for uid in list(user_ips.keys()):
                    if now - user_ips[uid]["last_seen"] > 300:
                        del user_ips[uid]
            last_cleanup_time = now

        # فحص انتهاء مدة الحظر المؤقت
        unblocked_users = []
        with blocked_lock:
            for uid, unblock_time in list(blocked_users.items()):
                if now >= unblock_time:
                    unblocked_users.append(uid)
                    del blocked_users[uid]

        # فحص المستخدمين الجدد المسجلين في Redis
        fresh_users = get_all_users()
        new_uids = set(fresh_users.keys()) - known_user_ids

        # 1. إضافة المستخدمين الجدد لحظياً عبر API بدون أي ريستارت
        if new_uids:
            for uid in new_uids:
                u_uuid = fresh_users[uid].get("uuid")
                if u_uuid:
                    xray_api_add_user(u_uuid, uid)
            known_user_ids = set(fresh_users.keys())

        # 2. إعادة المستخدمين المفكوك حظرهم عبر API بدون أي ريستارت
        if unblocked_users:
            for uid in unblocked_users:
                u_uuid = fresh_users.get(uid, {}).get("uuid")
                if u_uuid:
                    xray_api_add_user(u_uuid, uid)

                with tracker_lock:
                    user_ips.pop(uid, None)

                safe_u = html.escape(str(uid))
                log(f"✅ Unblocked & Hot-Added: {uid}")
                send_telegram(
                    f"✅ <b>انتهى الحظر المؤقت للحساب:</b>\n<code>{safe_u}</code>\n"
                    f"🚀 تم فتح اتصالك فوراً، يمكنك معاودة الاتصال الآن."
                )

        time.sleep(1)


if __name__ == "__main__":
    main()
