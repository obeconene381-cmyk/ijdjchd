#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ipaddress
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
# الإعدادات العامة
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
IP_TTL_SECONDS = int(os.environ.get("IP_TTL_SECONDS", "60"))  # نافذة احتساب الشبكات
SYNC_INTERVAL = 40  # دورة المزامنة وإعادة التشغيل الموحدة (كل 40 ثانية)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8812248294:AAHbQnTWwkkneggwN8G8yTg_1HyYoy95S5I"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5813081202")

# نمط استخراج الآيبي والـ ID بدقة
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


def get_ip_subnet(ip_str):
    """تجميع العناوين بنطاق /24 للـ IPv4 و /64 للـ IPv6 لمنع حظر 4G الذاتي"""
    try:
        clean_ip = ip_str.strip("[]")
        ip_obj = ipaddress.ip_address(clean_ip)
        if ip_obj.version == 4:
            return str(ipaddress.ip_network(f"{clean_ip}/24", strict=False))
        else:
            return str(ipaddress.ip_network(f"{clean_ip}/64", strict=False))
    except Exception:
        return ip_str


# ==============================================================================
# اتصال Redis والخصم الأتمي للكوتا
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
    log(f"Xray reloaded cleanly with {len(clients)} active clients.")


def xray_api_remove_user(user_id):
    """طرد فوري للمستخدم لحظة رصد المخالفة دون إعادة تشغيل"""
    cmd = f'{XRAY_BIN} api rmu --server={XRAY_API_SERVER} -tag="{XRAY_INBOUND_TAG}" "{user_id}"'
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res.returncode == 0


# ==============================================================================
# كشف تعدد الأجهزة الذكي (تمييز وضع الطيران عن المشاركة الفعلية)
# ==============================================================================
device_tracker = defaultdict(lambda: {"last_subnet": None, "subnets": {}})
tracker_lock = threading.Lock()
blocked_users = {}  # user_id -> unblock_timestamp
blocked_lock = threading.Lock()


def kick_and_block(user_id, distinct_ips):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return
        blocked_users[user_id] = now + BLOCK_DURATION

    # طرد فوري للجلسة النشطة
    xray_api_remove_user(user_id)

    with tracker_lock:
        device_tracker.pop(user_id, None)

    log(f"🚨 Sharing detected! Kicked: {user_id} | IPs: {distinct_ips}")
    send_telegram(
        f"🚨 <b>تم كشف مشاركة الحساب</b>\n"
        f"👤 المعرف (ID): <code>{user_id}</code>\n"
        f"🌐 عدد الأجهزة: {len(distinct_ips)}\n"
        f"📋 العناوين: <code>{', '.join(distinct_ips)}</code>\n"
        f"⛔ تم قطع الاتصال فوراً وحظر الحساب مؤقتاً.\n"
        f"⚠️ <i>تنبيه: أوقف الـ VPN في الجهاز الآخر قبل معاودة الاتصال.</i>"
    )


def handle_new_connection(user_id, ip):
    now = time.time()
    with blocked_lock:
        if now < blocked_users.get(user_id, 0):
            return

    current_sub = get_ip_subnet(ip)

    with tracker_lock:
        state = device_tracker[user_id]
        subnets = state["subnets"]

        # مسح النطاقات الخاملة لأكثر من 60 ثانية
        for sub in list(subnets.keys()):
            if now - subnets[sub]["time"] > IP_TTL_SECONDS:
                del subnets[sub]

        last_sub = state["last_subnet"]

        # الحالة 1: أول اتصال للمستخدم
        if last_sub is None:
            state["last_subnet"] = current_sub
            subnets[current_sub] = {"ip": ip, "time": now, "count": 1}
            return

        # الحالة 2: نفس الجهاز مستمر في التصفح من نفس النطاق
        if current_sub == last_sub:
            subnets[current_sub]["time"] = now
            subnets[current_sub]["ip"] = ip
            subnets[current_sub]["count"] = subnets[current_sub].get("count", 0) + 1
            return

        # الحالة 3: وصول طلب من نطاق يختلف عن آخر نطاق نشط
        # فحص ظاهرة التبادل (Ping-Pong): إذا كان النطاق الحالي شوهد مسبقاً وما زال القديم نشطاً
        # هذا يثبت وجود جهازين يرسلان طلبات بالتناوب في نفس اللحظة
        if current_sub in subnets:
            prev_time = subnets[last_sub]["time"]
            if (now - prev_time) <= 35:
                # تبادل متزامن مؤكد بنسبة 100%
                active_ips = list({v["ip"] for v in subnets.values()} | {ip})
                kick_and_block(user_id, active_ips)
                return

        # فحص التزامن اللحظي المباشر (طلب من جهازين خلال أقل من 10 ثوانٍ)
        prev_sub_time = subnets.get(last_sub, {}).get("time", 0)
        if (now - prev_sub_time) <= 10 and subnets.get(last_sub, {}).get("count", 0) >= 2:
            # جهازان نشطان معاً في نفس اللحظة
            active_ips = [subnets[last_sub]["ip"], ip]
            kick_and_block(user_id, active_ips)
            return

        # إذا مر وقت كافٍ وانقطع النطاق القديم: انتقال طبيعي للشبكة (مثل وضع الطيران)
        subnets[current_sub] = {"ip": ip, "time": now, "count": 1}
        state["last_subnet"] = current_sub


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
    if os.path.exists(XRAY_ACCESS_LOG):
        try:
            os.remove(XRAY_ACCESS_LOG)
        except Exception:
            pass
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

    last_stats = get_user_traffic() or {}
    last_loaded_clients = {
        str(u_id): d["uuid"]
        for u_id, d in users.items()
        if d.get("quota_bytes", 0) > 0 and d.get("uuid")
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
                                with tracker_lock:
                                    device_tracker.pop(uid_str, None)
                                log(f"🚫 Quota finished: {uid_str}")
                                send_telegram(
                                    f"🚫 نفدت كوتا الحساب: <code>{uid_str}</code>"
                                )
                last_stats = current_stats
            last_quota_check = now

        # دورة المزامنة وفك الحظر الموحدة (كل 40 ثانية بالضبط)
        if now - last_sync_time >= SYNC_INTERVAL:
            try:
                users = get_all_users()

                # استخراج الحسابات التي انتهت مدة حظرها
                unblocked_users = []
                with blocked_lock:
                    for uid, unblock_time in list(blocked_users.items()):
                        if now >= unblock_time:
                            unblocked_users.append(uid)
                            del blocked_users[uid]

                    currently_blocked = set(str(k) for k in blocked_users)

                target_clients = {
                    str(u_id): data["uuid"]
                    for u_id, data in users.items()
                    if data.get("quota_bytes", 0) > 0
                    and data.get("uuid")
                    and str(u_id) not in currently_blocked
                }

                # إعادة تشغيل Xray فقط عند وجود فك حظر أو تغيير في المستخدمين
                if target_clients != last_loaded_clients or unblocked_users:
                    log("Sync cycle (40s): Changes detected, reloading Xray once...")
                    restart_xray(users, blocked_set=currently_blocked)
                    time.sleep(0.5)
                    last_loaded_clients = target_clients
                    last_stats = get_user_traffic() or last_stats

                    # تنظيف سجل التتبع وإرسال الإشعار بعد أن أصبح Xray جاهزاً فعلياً
                    with tracker_lock:
                        for uid in unblocked_users:
                            device_tracker.pop(uid, None)

                    for uid in unblocked_users:
                        log(f"✅ Unblocked and live: {uid}")
                        send_telegram(
                            f"✅ <b>انتهى الحظر المؤقت للحساب:</b>\n<code>{uid}</code>\n"
                            f"🚀 تم تجهيز السيرفر بنجاح، يمكنك الاتصال الآن بأمان."
                        )
                else:
                    log("Sync cycle (40s): Stable. No client changes.")

                last_sync_time = now
            except Exception as e:
                log(f"❌ Sync cycle error: {e}")
                last_sync_time = now

        time.sleep(0.5)


if __name__ == "__main__":
    main()
