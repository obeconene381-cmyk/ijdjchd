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
# الإعدادات
# =============================================================================

# يجب ضبط REDIS_URL في متغيرات البيئة، بدون كلمة مرور داخل الملف.
REDIS_URL = os.environ.get("REDIS_URL", "")
if not REDIS_URL:
    raise RuntimeError("Missing environment variable: REDIS_URL")

REDIS_USERS_KEY = os.environ.get("REDIS_USERS_KEY", "users:data")

# نفس القيمة في جميع الحاويات التابعة لنفس الخدمة.
STATE_NAMESPACE = os.environ.get(
    "STATE_NAMESPACE", "corazon:shared-ipban:v2"
)

XRAY_BIN = "/usr/local/bin/xray"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_ACCESS_LOG = "/tmp/xray_access.log"
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"
XRAY_WS_PATH = os.environ.get("XRAY_WS_PATH", "/@pycorav1").rstrip("/")

BLOCK_DURATION = 60
IP_TTL_SECONDS = max(
    2, int(os.environ.get("IP_TTL_SECONDS", "12"))
)
SYNC_INTERVAL = max(
    2, int(os.environ.get("SYNC_INTERVAL", "10"))
)
BAN_CHECK_INTERVAL = 1.0

# عدد عينات التشخيص في كل حاوية؛ اجعلها صفرًا لاحقًا.
DIAG_SAMPLES = max(
    0, int(os.environ.get("DIAG_SAMPLES", "10"))
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

INSTANCE = (
    f"{os.environ.get('K_REVISION', 'local')}/"
    f"{socket.gethostname()}/pid={os.getpid()}"
)

os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def log(message):
    print(f"[MANAGER][{INSTANCE}] {message}", flush=True)


# =============================================================================
# Telegram خارج خيط قراءة اللوغ
# =============================================================================

notifications = queue.Queue(maxsize=100)


def notify(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        notifications.put_nowait(text)
    except queue.Full:
        log("Telegram queue full; notification skipped")


def notification_worker():
    while True:
        text = notifications.get()

        try:
            url = (
                f"https://api.telegram.org/bot"
                f"{TELEGRAM_BOT_TOKEN}/sendMessage"
            )
            body = parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }).encode()

            req = request.Request(url, data=body)

            with request.urlopen(req, timeout=5) as response:
                response.read()

        except Exception as exc:
            # تجنب طباعة رابط يحتوي توكن البوت.
            log(f"Telegram failed: {type(exc).__name__}")

        finally:
            notifications.task_done()


# =============================================================================
# Redis
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
    """
    None: فشل القراءة أو بيانات غير صالحة؛ احتفظ بالقائمة السابقة.
    {}: قراءة ناجحة لقائمة فارغة.
    """
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
        log(
            f"Redis user snapshot failed: {type(exc).__name__}; "
            "previous users retained"
        )
        return None


def get_ban_tokens(users):
    """لقطة الحظر المشترك للحسابات المعروفة."""
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
# كشف مشترك وقرار حظر ذري
# =============================================================================

# KEYS:
# 1 العناوين المرصودة
# 2 الحظر المؤقت
# 3 توقيت يمنع استعمال أحداث سابقة للحظر بعد انتهائه
#
# ARGV:
# 1 عنوان المصدر الكامل
# 2 نافذة الرصد
# 3 مدة الحظر
# 4 عمر الحدث عند قراءته
# 5 معرف فريد لحدث الحظر

DETECT_LUA = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local window = tonumber(ARGV[2])
local duration = tonumber(ARGV[3])
local age = tonumber(ARGV[4])
local event_time = now - age

if redis.call('EXISTS', KEYS[2]) == 1 then
    return {}
end

local ignore_before = tonumber(redis.call('GET', KEYS[3]) or '0')
if event_time <= ignore_before then
    return {}
end

redis.call(
    'ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window
)

local previous = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not previous or tonumber(previous) < event_time then
    redis.call('ZADD', KEYS[1], event_time, ARGV[1])
end

redis.call('EXPIRE', KEYS[1], math.ceil(window * 3))

local addresses = redis.call('ZRANGE', KEYS[1], 0, -1)

if #addresses > 1 then
    local created = redis.call(
        'SET', KEYS[2], ARGV[5], 'NX', 'EX', duration
    )

    if created then
        redis.call(
            'SET', KEYS[3], tostring(now + duration),
            'EX', math.ceil(duration + window + 60)
        )
        redis.call('DEL', KEYS[1])
        return addresses
    end
end

return {}
"""

detect_script = r.register_script(DETECT_LUA)


def observe_address(user_id, source_ip, event_ts):
    try:
        age = time.time() - event_ts

        if age < -2 or age > IP_TTL_SECONDS:
            return

        address = ipaddress.ip_address(source_ip)

        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                address = address.ipv4_mapped

        if address.is_loopback or address.is_unspecified:
            return

        base = state_base(user_id)
        token = uuid.uuid4().hex

        addresses = detect_script(
            keys=[
                f"{base}:addresses",
                f"{base}:ban",
                f"{base}:ignore_before",
            ],
            args=[
                str(address),
                IP_TTL_SECONDS,
                BLOCK_DURATION,
                max(0.0, age),
                token,
            ],
        )

        if addresses:
            log(
                f"BAN_CREATED uid={user_id!r} "
                f"token={token} duration={BLOCK_DURATION}s "
                f"addresses={addresses!r}"
            )

            notify(
                "⛔ <b>تم تسجيل حظر مؤقت للحساب</b>\n"
                f"👤 المعرف: <code>{html.escape(user_id)}</code>\n"
                f"🌐 العناوين: "
                f"<code>{html.escape(', '.join(addresses[:8]))}</code>\n"
                f"⏱ المدة: {BLOCK_DURATION} ثانية\n"
                "السبب: ظهور عنوانَي مصدر مختلفين ضمن نافذة الرصد.\n"
                "يجري تطبيق الحظر على الحاويات."
            )

    except Exception as exc:
        log(f"IP detection failed: {type(exc).__name__}")


# =============================================================================
# gRPC لإضافة المستخدم بدون إعادة تشغيل
# =============================================================================

def encode_varint(value):
    result = bytearray()

    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7

    result.append(value)
    return bytes(result)


def bytes_field(number, value):
    return (
        encode_varint((number << 3) | 2)
        + encode_varint(len(value))
        + value
    )


def string_field(number, value):
    return bytes_field(number, str(value).encode("utf-8"))


def typed_message(type_name, payload):
    # Xray TypedMessage وليس google.protobuf.Any.
    return (
        string_field(1, type_name)
        + bytes_field(2, payload)
    )


grpc_channel = grpc.insecure_channel(XRAY_API_SERVER)

grpc_alter_inbound = grpc_channel.unary_unary(
    "/xray.app.proxyman.command.HandlerService/AlterInbound",
    request_serializer=lambda value: value,
    response_deserializer=lambda value: value,
)


def add_user(user_id, user_uuid):
    try:
        account = typed_message(
            "xray.proxy.vless.Account",
            string_field(1, user_uuid),
        )

        user_message = (
            string_field(2, user_id)
            + bytes_field(3, account)
        )

        operation = typed_message(
            "xray.app.proxyman.command.AddUserOperation",
            bytes_field(1, user_message),
        )

        payload = (
            string_field(1, XRAY_INBOUND_TAG)
            + bytes_field(2, operation)
        )

        grpc_alter_inbound(payload, timeout=5)

        log(f"API_ADD_OK uid={user_id!r}")
        return True

    except grpc.RpcError as exc:
        log(
            f"API_ADD_FAILED uid={user_id!r} "
            f"code={exc.code()} details={exc.details()!r}"
        )
        return False


# =============================================================================
# الحذف بالطريقة القديمة المطلوبة: xray api rmu
# =============================================================================

def remove_user(user_id):
    command = [
        XRAY_BIN,
        "api",
        "rmu",
        f"--server={XRAY_API_SERVER}",
        f"-tag={XRAY_INBOUND_TAG}",
        str(user_id),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )

    except subprocess.TimeoutExpired:
        log(f"API_REMOVE_FAILED uid={user_id!r}: timeout")
        return False

    except OSError as exc:
        log(
            f"API_REMOVE_FAILED uid={user_id!r}: "
            f"{type(exc).__name__}"
        )
        return False

    if result.returncode != 0:
        details = (
            result.stderr.strip()
            or result.stdout.strip()
            or "No details"
        )

        log(
            f"API_REMOVE_FAILED uid={user_id!r} "
            f"exit={result.returncode} details={details[:1500]!r}"
        )
        return False

    log(
        f"API_REMOVE_OK uid={user_id!r}; "
        "existing tunnel closure not verified"
    )
    return True


# =============================================================================
# تشغيل Xray مرة واحدة
# =============================================================================

def start_xray(users):
    config = {
        "log": {
            "access": XRAY_ACCESS_LOG,
            "error": "/dev/stderr",
            "loglevel": "warning",
        },
        "api": {
            "tag": "api",
            "services": ["HandlerService"],
        },
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
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
        ],
    }

    os.makedirs(
        os.path.dirname(XRAY_CONFIG_PATH),
        exist_ok=True,
    )

    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(config, file)

    process = subprocess.Popen([
        XRAY_BIN,
        "run",
        "-config",
        XRAY_CONFIG_PATH,
    ])

    try:
        grpc.channel_ready_future(grpc_channel).result(timeout=15)

        deadline = time.monotonic() + 10

        while True:
            if process.poll() is not None:
                raise RuntimeError("Xray exited during startup")

            try:
                with socket.create_connection(
                    ("127.0.0.1", 5000),
                    timeout=0.5,
                ):
                    break

            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Xray port 5000 is not ready")
                time.sleep(0.2)

    except Exception:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise

    log(f"XRAY_READY users={len(users)}")
    return process


# =============================================================================
# قراءة سجل Xray مع توقيت إلزامي
# =============================================================================

ACCESS_RE = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2}\s+"
    r"\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?)\s+"
    r"(?:from\s+)?"
    r"(?:(?:tcp|udp):)?"
    r"(?P<ip>\[[0-9a-fA-F:.]+\]|[0-9a-fA-F:.]+)"
    r":(?P<port>\d+)\s+"
    r"accepted\s+.*?\bemail:\s*(?P<uid>\S+)"
)


def parse_event_time(value):
    base, dot, fraction = value.partition(".")
    base = " ".join(base.split())

    parsed = datetime.strptime(
        base, "%Y/%m/%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    return (
        parsed.timestamp()
        + (float("0." + fraction) if dot else 0)
    )


def access_log_reader():
    samples_left = DIAG_SAMPLES
    last_parse_warning = 0.0
    file = None
    file_identity = None

    try:
        while True:
            try:
                stat = os.stat(XRAY_ACCESS_LOG)
            except FileNotFoundError:
                time.sleep(0.2)
                continue

            identity = (stat.st_dev, stat.st_ino)

            if file is None or identity != file_identity:
                if file is not None:
                    file.close()

                file = open(
                    XRAY_ACCESS_LOG,
                    "r",
                    encoding="utf-8",
                    errors="replace",
                )
                file_identity = identity

            if stat.st_size < file.tell():
                file.seek(0)

            position = file.tell()
            line = file.readline()

            if not line:
                time.sleep(0.1)
                continue

            if not line.endswith("\n"):
                file.seek(position)
                time.sleep(0.1)
                continue

            if samples_left > 0:
                log(f"XRAY_RAW {line.rstrip()[:2000]!r}")
                samples_left -= 1

            if "accepted" not in line or "email:" not in line:
                continue

            match = ACCESS_RE.match(line)

            if not match:
                now = time.monotonic()

                if now - last_parse_warning > 60:
                    log(
                        "ACCESS_PARSE_MISS: line ignored; "
                        "unsupported source/timestamp format"
                    )
                    last_parse_warning = now

                continue

            try:
                event_ts = parse_event_time(match.group("ts"))

                observe_address(
                    match.group("uid"),
                    match.group("ip").strip("[]"),
                    event_ts,
                )

            except (ValueError, OverflowError):
                continue

    finally:
        if file is not None:
            file.close()


# =============================================================================
# تشخيص اختيار الآيبي في proxy.py الحالي
# =============================================================================

def install_proxy_diagnostics(proxy_module):
    original = proxy_module.ProxyHandler._get_client_ip
    lock = threading.Lock()
    remaining = DIAG_SAMPLES

    def diagnosed(handler, headers, client):
        nonlocal remaining

        selected = original(handler, headers, client)

        with lock:
            should_log = remaining > 0
            if should_log:
                remaining -= 1

        if should_log:
            log(
                f"IP_DEBUG "
                f"peer={client.getpeername()[0]!r} "
                f"xff={headers.get('x-forwarded-for', '')[:500]!r} "
                f"selected={selected!r}"
            )

        # تحقق صيغة فقط، وليس إثبات موثوقية X-Forwarded-For.
        return str(ipaddress.ip_address(selected))

    proxy_module.ProxyHandler._get_client_ip = diagnosed


# =============================================================================
# تطبيق قائمة المستخدمين والحظر محليًا
# =============================================================================

def reconcile_users(loaded, desired, banned):
    # الحذف أولًا: المحظور، المحذوف من Redis، أو صاحب UUID المتغير.
    for uid in list(loaded):
        must_remove = (
            uid in banned
            or uid not in desired
            or desired.get(uid) != loaded[uid]
        )

        if must_remove and remove_user(uid):
            del loaded[uid]

    # الإضافة: إعادة فحص الحظر قبل كل إضافة لتقليل سباق التحديث.
    for uid, user_uuid in desired.items():
        if uid in loaded or uid in banned:
            continue

        if r.exists(f"{state_base(uid)}:ban"):
            continue

        if add_user(uid, user_uuid):
            loaded[uid] = user_uuid


def announce_local_restoration(user_id, ban_token):
    """
    إشعار واحد قدر الإمكان لكل حدث.
    يثبت الاستعادة المحلية فقط، وليس اكتمال جميع الحاويات.
    """
    key = f"{state_base(user_id)}:restored:{ban_token}"

    acquired = r.set(key, INSTANCE, nx=True, ex=3600)

    if acquired:
        notify(
            "✅ <b>انتهت مدة الحظر</b>\n"
            f"👤 المعرف: <code>{html.escape(user_id)}</code>\n"
            "تم التحقق من إتاحة الحساب في إحدى الحاويات.\n"
            "بقية الحاويات تتابع المزامنة تلقائيًا."
        )


# =============================================================================
# التشغيل الرئيسي
# =============================================================================

def main():
    log(
        f"MODE=SHARED_IP_BAN "
        f"duration={BLOCK_DURATION}s "
        f"window={IP_TTL_SECONDS}s"
    )

    users = get_users()

    if users is None:
        raise RuntimeError(
            "Cannot start without a valid Redis user snapshot"
        )

    initial_bans = get_ban_tokens(users)

    initial_allowed = {
        uid: user_uuid
        for uid, user_uuid in users.items()
        if uid not in initial_bans
    }

    with open(XRAY_ACCESS_LOG, "w", encoding="utf-8"):
        pass

    threading.Thread(
        target=notification_worker,
        daemon=True,
    ).start()

    xray_process = start_xray(initial_allowed)

    loaded = dict(initial_allowed)
    desired = dict(users)

    # أحداث الحظر التي شاهدتها هذه الحاوية.
    observed_bans = dict(initial_bans)

    try:
        import proxy
        install_proxy_diagnostics(proxy)

        reader_thread = threading.Thread(
            target=access_log_reader,
            daemon=True,
        )

        proxy_thread = threading.Thread(
            target=proxy.main,
            daemon=True,
        )

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
                fresh_users = get_users()

                if fresh_users is not None:
                    desired = fresh_users

                next_user_sync = (
                    time.monotonic() + SYNC_INTERVAL
                )

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
                            # الإضافة لم تنجح بعد.
                            continue

                        # تأكيد أن الحظر لم يتجدد أثناء المزامنة.
                        if r.exists(f"{state_base(uid)}:ban"):
                            continue

                        log(
                            f"LOCAL_UNBLOCK_CONFIRMED "
                            f"uid={uid!r} token={token}"
                        )

                        announce_local_restoration(uid, token)
                        del observed_bans[uid]

                except redis.RedisError as exc:
                    now_error = time.monotonic()

                    if now_error - last_redis_error > 10:
                        log(
                            "Redis ban synchronization failed; "
                            "local state retained: "
                            f"{type(exc).__name__}"
                        )
                        last_redis_error = now_error

                next_ban_check = (
                    time.monotonic() + BAN_CHECK_INTERVAL
                )

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
