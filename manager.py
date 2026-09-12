#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib import parse, request

import grpc
import redis

# =============================================================================
# 1. الإعدادات الأساسية
# =============================================================================

REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    raise RuntimeError("Missing environment variable: REDIS_URL")

REDIS_USERS_KEY = os.environ.get("REDIS_USERS_KEY", "users:data")
STATE_NAMESPACE = os.environ.get("STATE_NAMESPACE", "corazon:netban:v3")

XRAY_BIN = "/usr/local/bin/xray"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_ACCESS_LOG = "/tmp/xray_access.log"
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"
XRAY_WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1").rstrip("/")

# فترات الحظر والمخالفات
BLOCK_DURATION = max(10, int(os.environ.get("BLOCK_DURATION", "60")))        # مدة الحظر العادي
LONG_BLOCK_DURATION = int(os.environ.get("LONG_BLOCK_DURATION", "10800"))    # حظر 3 ساعات (10800 ثانية)
MAX_STRIKES = int(os.environ.get("MAX_STRIKES", "5"))                        # الحد الأقصى للمخالفات

IP_TTL_SECONDS = max(2, int(os.environ.get("IP_TTL_SECONDS", "12")))
SYNC_INTERVAL = max(2, int(os.environ.get("SYNC_INTERVAL", "10")))
BAN_CHECK_INTERVAL = 1.0

# ضبط صريح للأقنعة:
# IPv4: تثبيت أول 3 أرقام (A.B.C) وتجاهل الأخير فقط
# IPv6: تثبيت أول خانتين (/32) لتفادي تقلبات خوادم Meta ومزودي الخدمة
V4_PREFIX = 24
V6_PREFIX = 32

IGNORED_NETWORKS = os.environ.get("IGNORED_NETWORKS", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

INSTANCE = f"{os.environ.get('K_REVISION', 'local')}/{socket.gethostname()}/pid={os.getpid()}"

os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def log(message):
    print(f"[MANAGER][{INSTANCE}] {message}", flush=True)


def parse_networks(value):
    networks = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log(f"IGNORED_NETWORKS entry invalid: {item!r}")
    return networks


IGNORED_PARSED = parse_networks(IGNORED_NETWORKS)
ZERO_V6 = ipaddress.ip_network("::/8")


# =============================================================================
# 2. نظام إشعارات Telegram غير المتزامن مع منع التكرار (Debounce)
# =============================================================================

notifications = queue.Queue(maxsize=200)
recent_alerts = {}
alerts_lock = threading.Lock()


def notify(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        notifications.put_nowait(text)
    except queue.Full:
        log("Telegram queue full; notification skipped")


def notify_debounced(key, text, cooldown=30):
    """منع تكرار نفس الإشعار لنفس المصدر خلال فترة محددة لمنع إغراق البوت"""
    now = time.time()
    with alerts_lock:
        last = recent_alerts.get(key, 0)
        if now - last < cooldown:
            return
        recent_alerts[key] = now
    notify(text)


def notification_worker():
    while True:
        text = notifications.get()
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            body = parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }).encode()
            req = request.Request(url, data=body)
            with request.urlopen(req, timeout=5) as response:
                response.read()
        except Exception as exc:
            log(f"Telegram failed: {type(exc).__name__}")
        finally:
            notifications.task_done()


# =============================================================================
# 3. إدارة المستخدمين والحالات في Redis
# =============================================================================

r = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    max_connections=20,
    socket_connect_timeout=3,
    socket_timeout=3,
    health_check_interval=30,
)


def state_base(user_id):
    safe_id = parse.quote(str(user_id), safe="")
    return f"{STATE_NAMESPACE}:{{{safe_id}}}"


def get_users():
    try:
        records = r.hgetall(REDIS_USERS_KEY)
        users = {}
        seen_uuids = set()

        for user_id, payload in records.items():
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ValueError("Invalid user object")

            user_id = str(user_id)
            if not user_id or any(c.isspace() for c in user_id):
                raise ValueError("Invalid user ID")

            user_uuid = str(uuid.UUID(str(data["uuid"])))

            if user_uuid in seen_uuids:
                raise ValueError("Duplicate UUID")

            seen_uuids.add(user_uuid)
            users[user_id] = user_uuid

        return users
    except Exception as exc:
        log(f"Redis user snapshot failed: {type(exc).__name__}")
        return None


def get_ban_tokens(users):
    if not users:
        return {}

    user_ids = list(users)
    with r.pipeline(transaction=False) as pipe:
        for uid in user_ids:
            pipe.get(f"{state_base(uid)}:ban")
        values = pipe.execute()

    return {
        uid: token
        for uid, token in zip(user_ids, values)
        if token is not None
    }


# =============================================================================
# 4. تطبيع واقتطاع الشبكات الصارم
# =============================================================================

def normalize_source(raw_ip):
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return None, "not-parseable"

    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address.is_loopback or address.is_unspecified:
            return None, None
        elif address in ZERO_V6:
            return None, "suspicious-zero-prefix"

    if address.is_loopback or address.is_unspecified:
        return None, None
    if address.is_multicast or address.is_link_local:
        return None, None
    if address.is_private or address.is_reserved:
        return None, "non-public"

    return address, None


def network_key(address):
    """
    اقتطاع دقيق وصارم:
    - IPv4: تثبيت أول 3 أرقام (A.B.C) وتجاهل الأخير تماماً (A.B.C.0/24)
    - IPv6: تثبيت أول خانتين فقط (/32)
    """
    try:
        if address.version == 4:
            parts = str(address).split(".")
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        else:
            return str(ipaddress.ip_network(f"{address}/{V6_PREFIX}", strict=False))
    except Exception:
        return str(address)


def is_ignored(address):
    for network in IGNORED_PARSED:
        if network.version == address.version and address in network:
            return True
    return False


# =============================================================================
# 5. منطق الحظر الذري مع احتساب المخالفات في Redis
# =============================================================================

DETECT_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local window = tonumber(ARGV[2])
local base_duration = tonumber(ARGV[3])
local age = tonumber(ARGV[4])
local token = ARGV[5]
local long_duration = tonumber(ARGV[6])
local max_strikes = tonumber(ARGV[7])
local event_time = now - age

-- إذا كان محظوراً حالياً لا داعي لتكرار الحظر
if redis.call('EXISTS', KEYS[2]) == 1 then
    return {}
end

-- تنظيف السجلات الأقدم من نافذة الرصد
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)

local previous = redis.call('ZSCORE', KEYS[1], ARGV[1])
if (not previous) or tonumber(previous) < event_time then
    redis.call('ZADD', KEYS[1], event_time, ARGV[1])
end

redis.call('EXPIRE', KEYS[1], math.ceil(window * 3))

local networks = redis.call('ZRANGE', KEYS[1], 0, -1)

-- إذا ظهرت شبكتان مختلفتان في نفس النافذة
if #networks > 1 then
    local strikes = redis.call('INCR', KEYS[3])
    redis.call('EXPIRE', KEYS[3], 86400) -- حفظ العداد لـ 24 ساعة

    local ban_time = base_duration
    local is_long_ban = 0

    -- عند الوصول لـ 5 مخالفات: حظر 3 ساعات وتصفير العداد
    if strikes >= max_strikes then
        ban_time = long_duration
        is_long_ban = 1
        redis.call('SET', KEYS[3], 0)
    end

    local created = redis.call('SET', KEYS[2], token, 'NX', 'EX', ban_time)
    if created then
        redis.call('DEL', KEYS[1])
        local result = { tostring(ban_time), tostring(strikes), tostring(is_long_ban) }
        for i, net in ipairs(networks) do
            table.insert(result, net)
        end
        return result
    end
end

return {}
"""

detect_script = r.register_script(DETECT_LUA)


def observe_network(user_id, address, age):
    try:
        base = state_base(user_id)
        token = uuid.uuid4().hex

        res = detect_script(
            keys=[
                f"{base}:networks",
                f"{base}:ban",
                f"{base}:strikes",
            ],
            args=[
                network_key(address),
                IP_TTL_SECONDS,
                BLOCK_DURATION,
                max(0.0, age),
                token,
                LONG_BLOCK_DURATION,
                MAX_STRIKES,
            ],
        )

        if res:
            ban_time = int(res[0])
            strikes = int(res[1])
            is_long_ban = (res[2] == "1")
            detected_networks = res[3:]

            log(
                f"🚨 BAN_TRIGGERED uid={user_id!r} duration={ban_time}s "
                f"strikes={strikes} long_ban={is_long_ban} networks={detected_networks!r}"
            )

            if is_long_ban:
                notify(
                    "🚨 <b>عقوبة مشددة: تم حظر الحساب لمدة 3 ساعات!</b>\n"
                    f"👤 المعرف: <code>{html.escape(user_id)}</code>\n"
                    f"⚠️ السبب: الوصول إلى الحد الأقصى للمخالفات (<b>{MAX_STRIKES} مرات</b>).\n"
                    f"🌐 الشبكات المكتشفة: <code>{html.escape(', '.join(detected_networks[:8]))}</code>\n"
                    "⏱ المدة: <b>3 ساعات (180 دقيقة)</b>\n"
                    "⛔ تم فصل الاتصال وإيقاف الخدمة."
                )
            else:
                notify(
                    "⛔ <b>تم تسجيل حظر مؤقت للحساب</b>\n"
                    f"👤 المعرف: <code>{html.escape(user_id)}</code>\n"
                    f"🌐 الشبكات: <code>{html.escape(', '.join(detected_networks[:8]))}</code>\n"
                    f"⏱ المدة: {ban_time} ثانية\n"
                    f"⚠️ تنبيه المخالفات: [ <b>{strikes}</b> / {MAX_STRIKES} ]\n"
                    f"<i>ملاحظة: عند تكرار المخالفة 5 مرات سيتم حظر الحساب تلقائياً لمدة 3 ساعات كاملة.</i>"
                )

    except Exception as exc:
        log(f"IP detection failed: {type(exc).__name__}")


# =============================================================================
# 6. إدارة مستخدمي Xray المباشرة عبر gRPC (بدون أخطاء الـ CLI)
# =============================================================================

def encode_varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def bytes_field(number, value):
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def string_field(number, value):
    return bytes_field(number, str(value).encode("utf-8"))


def typed_message(type_name, payload):
    return string_field(1, type_name) + bytes_field(2, payload)


grpc_channel = grpc.insecure_channel(XRAY_API_SERVER)
grpc_alter_inbound = grpc_channel.unary_unary(
    "/xray.app.proxyman.command.HandlerService/AlterInbound",
    request_serializer=lambda value: value,
    response_deserializer=lambda value: value,
)


def add_user(user_id, user_uuid):
    """إضافة المستخدم فوراً إلى Xray عبر gRPC"""
    try:
        account = typed_message(
            "xray.proxy.vless.Account",
            string_field(1, str(user_uuid).strip().lower()),
        )
        user_message = string_field(2, str(user_id)) + bytes_field(3, account)
        operation = typed_message(
            "xray.app.proxyman.command.AddUserOperation",
            bytes_field(1, user_message),
        )
        payload = string_field(1, XRAY_INBOUND_TAG) + bytes_field(2, operation)

        grpc_alter_inbound(payload, timeout=5)
        log(f"API_ADD_OK uid={user_id!r}")
        return True
    except Exception as exc:
        log(f"API_ADD_FAILED uid={user_id!r}: {exc}")
        return False


def remove_user(user_id):
    """حذف المستخدم فوراً من Xray"""
    command = [
        XRAY_BIN,
        "api",
        "rmu",
        f"--server={XRAY_API_SERVER}",
        f"-tag={XRAY_INBOUND_TAG}",
        str(user_id),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            log(f"API_REMOVE_OK uid={user_id!r}")
            return True
        return False
    except Exception as exc:
        log(f"API_REMOVE_FAILED uid={user_id!r}: {exc}")
        return False


# =============================================================================
# 7. تشغيل خادم Xray
# =============================================================================

def start_xray(users):
    config = {
        "log": {
            "access": XRAY_ACCESS_LOG,
            "error": "/dev/stderr",
            "loglevel": "warning",
        },
        "api": {"tag": "api", "services": ["HandlerService"]},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 5000,
                "protocol": "vless",
                "tag": XRAY_INBOUND_TAG,
                "settings": {
                    "clients": [
                        {"id": user_uuid, "email": user_id}
                        for user_id, user_uuid in users.items()
                    ],
                    "decryption": "none",
                },
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
                "tag": "api-inbound",
                "settings": {"address": "127.0.0.1"},
            },
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api-inbound"],
                    "outboundTag": "api",
                },
            ],
        },
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }

    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(config, file)

    process = subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH])

    deadline = time.monotonic() + 10
    while True:
        if process.poll() is not None:
            raise RuntimeError("Xray exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", 5000), timeout=0.5):
                break
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Xray port 5000 is not ready")
            time.sleep(0.2)

    log(f"XRAY_READY users={len(users)}")
    return process


# =============================================================================
# 8. قراءة سجل الوصول وتتبع الاتصال بعد القبول (Xray)
# =============================================================================

ACCESS_RE = re.compile(
    r"(?P<ts>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?).*?"
    r"(?:(?:tcp|udp):)?\[?(?P<ip>(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]+)\]?:\d+\s+"
    r"accepted.*?\bemail:\s*(?P<uid>\S+)"
)


def parse_event_time(value):
    base, dot, fraction = value.partition(".")
    base = " ".join(base.split())
    parsed = datetime.strptime(base, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return parsed.timestamp() + (float("0." + fraction) if dot else 0)


def access_log_reader():
    handle = None
    identity = None

    while True:
        try:
            stat = os.stat(XRAY_ACCESS_LOG)
        except FileNotFoundError:
            time.sleep(0.2)
            continue

        current = (stat.st_dev, stat.st_ino)

        if handle is None or current != identity:
            if handle is not None:
                handle.close()
            handle = open(XRAY_ACCESS_LOG, "rb")
            identity = current

        if stat.st_size < handle.tell():
            handle.seek(0)

        position = handle.tell()
        raw = handle.readline()

        if not raw:
            time.sleep(0.05)
            continue

        if not raw.endswith(b"\n"):
            handle.seek(position)
            time.sleep(0.05)
            continue

        line = raw.decode("utf-8", "replace")

        if "accepted" not in line or "email:" not in line:
            continue

        match = ACCESS_RE.search(line)
        if not match:
            continue

        try:
            event_ts = parse_event_time(match.group("ts"))
        except (ValueError, OverflowError):
            event_ts = time.time()

        uid = match.group("uid")
        raw_ip = match.group("ip").strip("[]")

        address, reason = normalize_source(raw_ip)
        if address is None or is_ignored(address):
            continue

        net = network_key(address)

        # 1. طباعة تفصيلية في الـ Log
        log(f"🟢 [XRAY_ACCEPTED] uid={uid} | ip={raw_ip} | calculated_net={net}")

        # 2. إشعار تيليجرام (بعد الاتصال) مع منع التكرار لنفس المستخدم والشبكة لمدة دقيقة
        tg_key = f"xray_conn:{uid}:{net}"
        notify_debounced(
            tg_key,
            f"🟢 <b>اتصال مؤكد (بعد الاتصال - Xray)</b>\n"
            f"👤 المعرف: <code>{html.escape(uid)}</code>\n"
            f"🌐 الآيبي الفعلي: <code>{html.escape(raw_ip)}</code>\n"
            f"🏷️ الشبكة المحسوبة: <code>{html.escape(net)}</code>\n"
            f"⏰ التوقيت: <code>{datetime.now().strftime('%H:%M:%S')}</code>",
            cooldown=60
        )

        age = time.time() - event_ts
        if age < -10 or age > (IP_TTL_SECONDS * 3):
            continue

        observe_network(uid, address, age)


# =============================================================================
# 9. تتبع طلب الاتصال قبل المصافحة (Proxy)
# =============================================================================

def install_proxy_diagnostics(proxy_module):
    original_get_ip = proxy_module.ProxyHandler._get_client_ip

    def diagnosed(handler, headers, client):
        selected_ip = original_get_ip(handler, headers, client)

        try:
            peer = client.getpeername()[0]
        except Exception:
            peer = "unknown"

        xff = headers.get("x-forwarded-for", "NONE")
        host = headers.get("host", "NONE")

        # 1. طباعة كاملة في الـ Log
        log(f"📡 [PROXY_INCOMING] peer={peer} | selected={selected_ip} | xff={xff[:120]} | host={host}")

        # 2. إشعار تيليجرام (قبل الاتصال) مع منع التكرار لنفس الآيبي لمدة 30 ثانية
        tg_key = f"proxy_in:{selected_ip}"
        notify_debounced(
            tg_key,
            f"📡 <b>طلب اتصال وارد (قبل الاتصال - Proxy)</b>\n"
            f"🌐 الآيبي المستخرج: <code>{html.escape(selected_ip)}</code>\n"
            f"🔗 المقبس المباشر (Peer): <code>{html.escape(peer)}</code>\n"
            f"📋 ترويسة X-Forwarded-For:\n<code>{html.escape(xff[:150])}</code>\n"
            f"🌍 الهوست: <code>{html.escape(host)}</code>",
            cooldown=30
        )

        return selected_ip

    proxy_module.ProxyHandler._get_client_ip = diagnosed


# =============================================================================
# 10. مزامنة الحالة وتطبيق الحظر
# =============================================================================

def reconcile_users(loaded, desired, banned):
    for uid in list(loaded):
        must_remove = (
            uid in banned
            or uid not in desired
            or desired.get(uid) != loaded[uid]
        )
        if must_remove and remove_user(uid):
            del loaded[uid]

    for uid, user_uuid in desired.items():
        if uid in loaded or uid in banned:
            continue
        if r.exists(f"{state_base(uid)}:ban"):
            continue
        if add_user(uid, user_uuid):
            loaded[uid] = user_uuid


def announce_local_restoration(user_id, ban_token):
    key = f"{state_base(user_id)}:restored:{ban_token}"
    if r.set(key, INSTANCE, nx=True, ex=3600):
        notify(
            "✅ <b>انتهت مدة الحظر</b>\n"
            f"👤 المعرف: <code>{html.escape(user_id)}</code>\n"
            "🔄 تم فك الحظر واستعادة الاتصال بنجاح."
        )


# =============================================================================
# 11. التشغيل الرئيسي
# =============================================================================

def main():
    log(
        f"MODE=NET_BAN duration={BLOCK_DURATION}s long_duration={LONG_BLOCK_DURATION}s "
        f"window={IP_TTL_SECONDS}s v4=/{V4_PREFIX} (ignores last octet) v6=/{V6_PREFIX}"
    )

    users = get_users()
    if users is None:
        raise RuntimeError("Cannot start without a valid Redis user snapshot")

    initial_bans = get_ban_tokens(users)
    initial_allowed = {
        uid: user_uuid
        for uid, user_uuid in users.items()
        if uid not in initial_bans
    }

    with open(XRAY_ACCESS_LOG, "w", encoding="utf-8"):
        pass

    threading.Thread(target=notification_worker, daemon=True).start()

    xray_process = start_xray(initial_allowed)

    loaded = dict(initial_allowed)
    desired = dict(users)
    observed_bans = dict(initial_bans)

    try:
        import proxy
        install_proxy_diagnostics(proxy)

        reader_thread = threading.Thread(target=access_log_reader, daemon=True)
        proxy_thread = threading.Thread(target=proxy.main, daemon=True)
        reader_thread.start()
        proxy_thread.start()

        next_user_sync = time.monotonic() + SYNC_INTERVAL
        next_ban_check = 0.0
        last_redis_error = 0.0

        while True:
            if xray_process.poll() is not None:
                raise RuntimeError("Xray stopped unexpectedly")
            if not proxy_thread.is_alive():
                raise RuntimeError("Proxy thread stopped unexpectedly")
            if not reader_thread.is_alive():
                raise RuntimeError("Access reader stopped unexpectedly")

            now = time.monotonic()

            if now >= next_user_sync:
                fresh = get_users()
                if fresh is not None:
                    desired = fresh
                next_user_sync = time.monotonic() + SYNC_INTERVAL

            if now >= next_ban_check:
                try:
                    bans = get_ban_tokens(desired)
                    observed_bans.update(bans)

                    reconcile_users(loaded, desired, bans)

                    for uid, token in list(observed_bans.items()):
                        if uid not in desired:
                            del observed_bans[uid]
                            continue
                        if uid in bans:
                            continue
                        if loaded.get(uid) != desired[uid]:
                            continue
                        if r.exists(f"{state_base(uid)}:ban"):
                            continue

                        log(f"LOCAL_UNBLOCK_CONFIRMED uid={uid!r} token={token}")
                        announce_local_restoration(uid, token)
                        del observed_bans[uid]

                except redis.RedisError as exc:
                    now_error = time.monotonic()
                    if now_error - last_redis_error > 10:
                        log(f"Redis ban sync failed: {type(exc).__name__}")
                        last_redis_error = now_error

                next_ban_check = time.monotonic() + BAN_CHECK_INTERVAL

            time.sleep(0.2)

    finally:
        grpc_channel.close()
        if xray_process.poll() is None:
            xray_process.terminate()
            try:
                xray_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                xray_process.kill()
                xray_process.wait()


if __name__ == "__main__":
    main()
