#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xray User Manager + Anti Account-Sharing
IP واحد فقط لكل UUID

مهم:
- adu/rmu يحتاجان إصدار Xray حديثاً يدعم هذه الأوامر.
- acceptProxyProtocol يجب أن يكون True فقط إذا كان Xray يستقبل
  PROXY protocol فعلياً من L4 proxy/stream proxy.
"""

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict

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

IP_CHECK_INTERVAL = 2.0
BLOCK_DURATION = 30
IP_TTL_SECONDS = 90

# اجعله True فقط مع TCP/L4 proxy يرسل PROXY protocol.
# مع Nginx HTTP/WebSocket أو Cloud Run اتركه False.
ACCEPT_PROXY_PROTOCOL = os.environ.get(
    "XRAY_ACCEPT_PROXY_PROTOCOL", "false"
).lower() in {"1", "true", "yes", "on"}


def log(message):
    print(f"[MANAGER] {message}", flush=True)


# ==============================================================================
# Redis
# ==============================================================================
try:
    redis_client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        max_connections=5,
    )
    redis_client.ping()
    log("Connected to Redis.")
except Exception as exc:
    log(f"Redis error: {exc}")
    redis_client = None


DEDUCT_BYTES_LUA = r"""
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if raw then
    local data = cjson.decode(raw)
    local old_quota = tonumber(data['quota_bytes']) or 0
    local used = tonumber(ARGV[2]) or 0
    local new_quota = old_quota - used
    if new_quota < 0 then new_quota = 0 end
    data['quota_bytes'] = new_quota
    redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(data))
    return new_quota
end
return -1
"""

try:
    deduct_script = (
        redis_client.register_script(DEDUCT_BYTES_LUA)
        if redis_client else None
    )
except Exception as exc:
    log(f"Redis script error: {exc}")
    deduct_script = None


def atomic_deduct_user_bytes(email, bytes_used):
    if not deduct_script:
        return -1
    try:
        return int(
            deduct_script(
                keys=[REDIS_USERS_KEY],
                args=[str(email), str(int(bytes_used))],
            )
        )
    except Exception as exc:
        log(f"atomic deduct {email}: {exc}")
        return -1


def get_all_users():
    if not redis_client:
        return {}

    try:
        raw_users = redis_client.hgetall(REDIS_USERS_KEY)
        users = {}
        for email, raw_data in raw_users.items():
            try:
                data = json.loads(raw_data)
                if isinstance(data, dict):
                    users[email] = data
            except (TypeError, json.JSONDecodeError) as exc:
                log(f"Invalid Redis data for {email}: {exc}")
        return users
    except Exception as exc:
        log(f"Redis read error: {exc}")
        return {}


def user_is_active(user):
    """المستخدم فعال فقط إذا كانت الكوتا والمدة الزمنية لم تنتهيا."""
    try:
        quota = int(user.get("quota_bytes", 0) or 0)
        expire_at = user.get("expire_at")

        if expire_at not in (None, "", 0):
            if time.time() >= float(expire_at):
                return False

        return quota > 0
    except (TypeError, ValueError):
        return False


# ==============================================================================
# Xray API
# ==============================================================================
def run_xray_api(args):
    return subprocess.run(
        [XRAY_BIN, "api", *args],
        capture_output=True,
        text=True,
        timeout=5,
    )


def xray_api_add_user(email, uuid):
    """إضافة مستخدم باستخدام صيغة adu الحديثة: ملف يحوي inbounds."""
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

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="xray_add_",
            delete=False,
        ) as temp_file:
            json.dump(user_config, temp_file)
            temp_path = temp_file.name

        result = run_xray_api(
            ["adu", f"--server={XRAY_API_SERVER}", temp_path]
        )

        if result.returncode == 0:
            return True

        log(
            f"adu failed for {email}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False
    except Exception as exc:
        log(f"adu exception for {email}: {exc}")
        return False
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def xray_api_remove_user(email):
    """حذف المستخدم من inbound، وبالتالي قطع اتصالاته الحالية."""
    try:
        result = run_xray_api(
            [
                "rmu",
                f"--server={XRAY_API_SERVER}",
                f"-tag={XRAY_INBOUND_TAG}",
                email,
            ]
        )
        if result.returncode == 0:
            return True

        log(
            f"rmu failed for {email}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False
    except Exception as exc:
        log(f"rmu exception for {email}: {exc}")
        return False


def get_online_users_ips():
    """إرجاع {email: [ip, ...]} إذا كان Xray يدعم statsonlineiplist."""
    try:
        result = run_xray_api(
            [
                "statsonlineiplist",
                f"--server={XRAY_API_SERVER}",
                "-all",
            ]
        )
        if result.returncode != 0:
            return {}

        data = json.loads(result.stdout)
        output = {}

        for user in data.get("users", []):
            email = user.get("email", "")
            raw_ips = user.get("ips", [])
            ips = []

            if isinstance(raw_ips, list):
                for item in raw_ips:
                    if isinstance(item, dict) and item.get("ip"):
                        ips.append(str(item["ip"]))
                    elif isinstance(item, str):
                        ips.append(item)
            elif isinstance(raw_ips, dict):
                ips = [str(ip) for ip in raw_ips]

            if email and ips:
                output[email] = list(dict.fromkeys(ips))

        return output
    except Exception:
        return {}


# ==============================================================================
# مراقبة الاتصالات
# ==============================================================================
ips_seen = defaultdict(dict)  # email -> {ip: timestamp}
ips_lock = threading.Lock()

blocked_users = {}  # email -> unblock timestamp
blocked_lock = threading.Lock()

ACCESS_LINE_RE = re.compile(
    r"from\s+(?:\[(?P<ip6>[0-9a-fA-F:]+)\]|(?P<ip4>[0-9.]+)):"
    r"\d+\s+accepted\s+.*?email:\s*(?P<email>\S+)"
)


def normalize_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def handle_new_connection(email, ip):
    now = time.time()
    ip = normalize_ip(ip)

    with blocked_lock:
        if now < blocked_users.get(email, 0):
            return

    with ips_lock:
        table = ips_seen[email]
        for old_ip in list(table):
            if now - table[old_ip] > IP_TTL_SECONDS:
                del table[old_ip]

        table[ip] = now
        distinct_ips = list(table)

    if len(distinct_ips) == 1:
        log(f"connected: {email} from {ip}")
        return

    log(
        f"ACCOUNT SHARING: {email} has {len(distinct_ips)} IPs: "
        f"{', '.join(distinct_ips)}"
    )
    kick_and_block(email)


def kick_and_block(email):
    # منع تنفيذ rmu عدة مرات بسبب وصول عدة أسطر متتالية من access.log.
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(email, 0):
            return
        blocked_users[email] = now + BLOCK_DURATION

    users = get_all_users()
    if email not in users:
        return

    log(f"Removing {email} and cutting all connections")
    xray_api_remove_user(email)

    with ips_lock:
        ips_seen[email].clear()

    log(f"Blocked {email} for {BLOCK_DURATION} seconds")


def unblock_worker():
    while True:
        try:
            now = time.time()
            to_unblock = []

            with blocked_lock:
                for email, unblock_at in list(blocked_users.items()):
                    if now >= unblock_at:
                        to_unblock.append(email)
                        del blocked_users[email]

            users = get_all_users()
            for email in to_unblock:
                # إعادة التحقق من عدم إعادة الحظر أثناء الفترة الانتقالية
                with blocked_lock:
                    if now < blocked_users.get(email, 0):
                        continue  # تم حظره مرة أخرى، تجاوز

                user = users.get(email)
                if not user or not user_is_active(user):
                    continue

                uuid = user.get("uuid")
                if uuid:
                    log(f"Unblocking {email}")
                    xray_api_add_user(email, uuid)
        except Exception as exc:
            log(f"unblock worker error: {exc}")

        time.sleep(1)


def access_log_tailer():
    while not os.path.exists(XRAY_ACCESS_LOG):
        time.sleep(1)

    process = subprocess.Popen(
        ["tail", "-F", "-n", "0", XRAY_ACCESS_LOG],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    log(f"Tailing {XRAY_ACCESS_LOG}")

    if not process.stdout:
        return

    for line in process.stdout:
        if "accepted" not in line or "email:" not in line:
            continue

        match = ACCESS_LINE_RE.search(line.strip())
        if not match:
            continue

        ip = match.group("ip6") or match.group("ip4")
        email = match.group("email")

        if ip in {"127.0.0.1", "::1"}:
            continue

        try:
            handle_new_connection(email, ip)
        except Exception as exc:
            log(f"connection handler error: {exc}")


# ==============================================================================
# بناء وتشغيل Xray
# ==============================================================================
def build_config(users, blocked_emails=None):
    """بناء الكونفيغ مع استبعاد المستخدمين المحظورين مؤقتاً."""
    if blocked_emails is None:
        blocked_emails = set()

    clients = [
        {"id": data["uuid"], "email": email}
        for email, data in users.items()
        if user_is_active(data)
        and data.get("uuid")
        and email not in blocked_emails
    ]

    return {
        "log": {
            "access": XRAY_ACCESS_LOG,
            "error": XRAY_ERROR_LOG,
            "loglevel": "info",
        },
        "api": {
            "tag": "api",
            "listen": XRAY_API_SERVER,
            "services": ["HandlerService", "StatsService"],
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "statsUserOnline": True,
                }
            }
        },
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 5000,
                "protocol": "vless",
                "tag": XRAY_INBOUND_TAG,
                "settings": {
                    "clients": clients,
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/@pycorav1"},
                    "sockopt": {
                        "acceptProxyProtocol": ACCEPT_PROXY_PROTOCOL
                    },
                },
            }
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
        ],
    }


def restart_xray(users, blocked_emails=None):
    """إعادة تشغيل Xray مع استبعاد المحظورين من الكونفيغ."""
    config = build_config(users, blocked_emails)
    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)

    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2, ensure_ascii=False)

    test = subprocess.run(
        [XRAY_BIN, "run", "-test", "-config", XRAY_CONFIG_PATH],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if test.returncode != 0:
        log(f"Xray config test failed: {test.stderr.strip()}")
        return False

    subprocess.run(["pkill", "-TERM", "-x", "xray"],
                   stderr=subprocess.DEVNULL)
    time.sleep(0.7)
    subprocess.Popen(
        [XRAY_BIN, "run", "-config", XRAY_CONFIG_PATH],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    log("Xray restarted")
    return True


# ==============================================================================
# الإحصائيات والكوتا
# ==============================================================================
def get_user_traffic():
    try:
        result = run_xray_api(
            [
                "statsquery",
                f"--server={XRAY_API_SERVER}",
                "-pattern",
                "user",
            ]
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        traffic = {}
        for item in data.get("stat", []):
            name = item.get("name", "")
            value = int(item.get("value", 0) or 0)

            if "user>>>" in name and ">>>traffic>>>" in name:
                email = name.split(">>>")[1]
                traffic[email] = traffic.get(email, 0) + value

        return traffic
    except Exception as exc:
        log(f"statsquery error: {exc}")
        return None


# ==============================================================================
# التشغيل
# ==============================================================================
def main():
    # فشل الاتصال بـ Redis -> لا يمكن المتابعة بدون مسح المستخدمين
    if redis_client is None:
        log("FATAL: Redis is unavailable. Exiting to avoid wiping users.")
        raise SystemExit(1)

    os.makedirs(os.path.dirname(XRAY_ACCESS_LOG), exist_ok=True)
    open(XRAY_ACCESS_LOG, "a").close()

    users = get_all_users()
    # في البداية لا يوجد محظورون
    if not restart_xray(users, blocked_emails=set()):
        raise SystemExit("Invalid Xray configuration")

    time.sleep(2)
    last_stats = get_user_traffic() or {}

    last_active = {
        email for email, data in users.items()
        if user_is_active(data)
    }
    last_sync = time.time()
    last_quota = time.time()
    last_ip_scan = time.time()
    last_cleanup = time.time()

    threading.Thread(target=access_log_tailer, daemon=True).start()
    threading.Thread(target=unblock_worker, daemon=True).start()

    log(
        f"Started. active_users={len(last_active)}, "
        f"proxy_protocol={ACCEPT_PROXY_PROTOCOL}"
    )

    while True:
        now = time.time()

        # خصم الترافيك كل 3 ثوانٍ.
        if now - last_quota >= 3:
            current_stats = get_user_traffic()
            if current_stats is not None:
                for email, user in list(users.items()):
                    used = current_stats.get(email, 0) - last_stats.get(email, 0)
                    if used < 0:
                        used = current_stats.get(email, 0)

                    if used > 0:
                        new_quota = atomic_deduct_user_bytes(email, used)
                        if new_quota >= 0:
                            user["quota_bytes"] = new_quota
                            log(f"{email}: -{used}B, remaining={new_quota}B")

                            if new_quota <= 0:
                                xray_api_remove_user(email)
                                with ips_lock:
                                    ips_seen.pop(email, None)

                last_stats = current_stats
            last_quota = now

        # تنظيف ips_seen من المدخلات القديمة (كل 60 ثانية)
        if now - last_cleanup >= 60:
            with ips_lock:
                for email in list(ips_seen):
                    table = ips_seen[email]
                    for ip in list(table):
                        if now - table[ip] > IP_TTL_SECONDS:
                            del table[ip]
                    if not table:
                        del ips_seen[email]
            last_cleanup = now

        # مزامنة المستخدمين مع Redis.
        if now - last_sync >= 20:
            new_users = get_all_users()
            current_active = {
                email for email, data in new_users.items()
                if user_is_active(data)
            }

            # تجميع قائمة المحظورين حالياً لاستبعادهم من الكونفيغ
            with blocked_lock:
                blocked_now = {
                    email for email, unblock_at in blocked_users.items()
                    if now < unblock_at
                }

            # إعادة التشغيل عند الإضافة أو الحذف أو تغيير UUID.
            old_signature = {
                (email, users[email].get("uuid"))
                for email in last_active if email in users
            }
            new_signature = {
                (email, new_users[email].get("uuid"))
                for email in current_active
            }

            if current_active != last_active or old_signature != new_signature:
                users = new_users
                if restart_xray(users, blocked_emails=blocked_now):
                    time.sleep(1)
                    last_stats = get_user_traffic() or last_stats
            else:
                users = new_users

            last_active = current_active
            last_sync = now

        # فحص احتياطي عبر Xray API، إذا كان الأمر مدعوماً.
        if now - last_ip_scan >= IP_CHECK_INTERVAL:
            online = get_online_users_ips()
            for email, ips in online.items():
                if email not in users or not user_is_active(users[email]):
                    continue

                with blocked_lock:
                    if now < blocked_users.get(email, 0):
                        continue

                real_ips = [
                    ip for ip in ips
                    if ip not in {"127.0.0.1", "::1"}
                ]
                if len(set(real_ips)) > 1:
                    log(
                        f"API detected multiple IPs for {email}: "
                        f"{', '.join(real_ips)}"
                    )
                    kick_and_block(email)

            last_ip_scan = now

        time.sleep(0.4)


if __name__ == "__main__":
    main()
