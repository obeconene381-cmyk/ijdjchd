import json
import os
import re
import subprocess
import time
import redis

REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://:CoraNetRedis2026SecurePass@54.86.129.233:6379/0",
)
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
REDIS_USERS_KEY = "users:data"

# ==============================================================================
# إعدادات منع مشاركة الحساب: IP واحد فقط لكل UUID
# ==============================================================================
ACCESS_LOG_PATH = "/var/log/xray/access.log"
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"

# كل كم ثانية نفحص اللوج
IP_CHECK_INTERVAL = 1.0
# كم ثانية نعتبر بعدها أن IP القديم انتهى (إذا لم يظهر في لوج القبول)
IP_EXPIRY_SECONDS = 60
# عدد البايتات التي نقرأها من اللوج كل دورة
LOG_READ_CHUNK = 8192
# مدة حظر الـ UUID عند اكتشاف مشاركة حساب (IP ثاني على نفس الـ UUID)
BLOCK_DURATION = 15  # ثانية


def log(msg):
    print(f"[MANAGER] {msg}", flush=True)


try:
    r = redis.from_url(REDIS_URL, decode_responses=True, max_connections=5)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis error: {e}")
    r = None

# ==============================================================================
# كود Lua الخصم الذري
# ==============================================================================
DEDUCT_BYTES_LUA = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if raw then
    local data = cjson.decode(raw)
    local old_quota = tonumber(data["quota_bytes"]) or 0
    local used = tonumber(ARGV[2]) or 0
    
    local new_quota = old_quota - used
    if new_quota < 0 then new_quota = 0 end
    
    data["quota_bytes"] = new_quota
    
    local updated_json = cjson.encode(data)
    redis.call('HSET', KEYS[1], ARGV[1], updated_json)
    return new_quota
end
return -1
"""

deduct_script = r.register_script(DEDUCT_BYTES_LUA) if r else None


def atomic_deduct_user_bytes(email, bytes_used):
    if not deduct_script:
        return -1
    try:
        new_quota = deduct_script(
            keys=[REDIS_USERS_KEY], args=[str(email), str(bytes_used)]
        )
        return int(new_quota)
    except Exception as e:
        log(f"❌ Error in atomic deduct for {email}: {e}")
        return -1


def get_all_users():
    if not r:
        return {}
    try:
        raw = r.hgetall(REDIS_USERS_KEY)
        users = {}
        for email, data_json in raw.items():
            email = email.decode() if isinstance(email, bytes) else email
            data_str = (
                data_json.decode()
                if isinstance(data_json, bytes)
                else data_json
            )
            users[email] = json.loads(data_str)
        return users
    except Exception as e:
        log(f"❌ Redis read error: {e}")
        return {}


# ==============================================================================
# دوال إدارة المستخدمين عبر Xray API (إضافة/حذف بدون إعادة تشغيل)
# ==============================================================================
# صيغة ملف الـ JSON الذي يتوقعه أمر adu:
# {
#   "inbounds": [{
#     "tag": "vless-inbound",
#     "protocol": "vless",
#     "settings": {
#       "decryption": "none",
#       "clients": [{ "id": "<uuid>", "email": "<email>" }]
#     }
#   }]
# }
def xray_api_add_user(email, uuid):
    """يضيف مستخدم عبر Xray API دون إعادة تشغيل"""
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
    tmp = f"/tmp/xray_add_{int(time.time()*1000)}.json"
    with open(tmp, "w") as f:
        json.dump(user_config, f)
    try:
        result = subprocess.run(
            [
                "/usr/local/bin/xray", "api", "adu",
                f"--server={XRAY_API_SERVER}",
                tmp,
            ],
            capture_output=True, text=True, timeout=5,
        )
        os.unlink(tmp)
        if result.returncode == 0:
            return True
        else:
            log(f"❌ xray adu failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log(f"❌ xray adu exception: {e}")
        return False


def xray_api_remove_user(email):
    """يحذف مستخدم عبر Xray API — هذا يقطع كل اتصالاته فوراً"""
    try:
        result = subprocess.run(
            [
                "/usr/local/bin/xray", "api", "rmu",
                f"--server={XRAY_API_SERVER}",
                f"-tag={XRAY_INBOUND_TAG}",
                email,
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True
        else:
            log(f"❌ xray rmu failed: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"❌ xray rmu exception: {e}")
        return False


def kick_and_block_user(email):
    """
    يقطع كل اتصالات المستخدم فوراً ويحظر الـ UUID لمدة BLOCK_DURATION ثانية.
    1) حذف المستخدم (rmu) — يقطع كل اتصالاته فوراً (IP القديم والجديد)
    2) لا يتم إعادة إضافته الآن — يبقى محظوراً
    3) إعادة الإضافة تتم تلقائياً بعد انتهاء مدة الحظر في اللوب الرئيسي
    """
    users = get_all_users()
    if email not in users:
        log(f"⚠️ Cannot kick {email}: not found in Redis")
        return False
    uuid = users[email].get("uuid")
    if not uuid:
        log(f"⚠️ Cannot kick {email}: no UUID")
        return False

    log(f"🔌 Kicking {email} (rmu) — disconnecting ALL IPs...")
    if not xray_api_remove_user(email):
        return False
    log(f"⏳ {email} is now BLOCKED for {BLOCK_DURATION} seconds.")
    return True


def unblock_user(email):
    """يعيد تفعيل المستخدم بعد انتهاء مدة الحظر"""
    users = get_all_users()
    if email not in users:
        log(f"⚠️ Cannot unblock {email}: not found in Redis")
        return False
    uuid = users[email].get("uuid")
    if not uuid:
        log(f"⚠️ Cannot unblock {email}: no UUID")
        return False

    # تأكد أولاً أن المستخدم ليس مضافاً بالفعل
    log(f"🔄 Unblocking {email} (adu) — re-adding to allow connections...")
    if not xray_api_add_user(email, uuid):
        log(f"❌ Failed to re-add {email} after block!")
        return False
    log(f"✅ {email} unblocked — connections allowed again.")
    return True


# ==============================================================================
# مراقب اللوج: IP واحد فقط لكل UUID
# ==============================================================================
# الريجيكس يطابق سطر اللوج:
# 2026/08/19 18:48:49.713692 from 129.45.83.252:0 accepted tcp:57.144.204.196:443 [vless-inbound >> direct] email: tester@vpn.local
LOG_RE = re.compile(
    r"from\s+(\d{1,3}(?:\.\d{1,3}){3}):"   # المصدر IP
    r"\d+\s+"                                 # المنفذ المصدر (نتجاهله)
    r"accepted\b.*?"                          # "accepted ... "
    r"email:\s*(\S+)"                         # البريد
)


def tail_access_log():
    """مولّد يقرأ اللوج بشكل تزايدي مثل tail -f"""
    # نتأكد من وجود الملف
    os.makedirs(os.path.dirname(ACCESS_LOG_PATH), exist_ok=True)
    open(ACCESS_LOG_PATH, "a").close()

    # نبدأ من آخر حجم معروف للملف
    # (نتتبع الإزاحة في الذاكرة)
    inode = None
    offset = 0

    # إذا الملف موجود بالفعل، نبدأ من آخر موضع
    try:
        offset = os.path.getsize(ACCESS_LOG_PATH)
        inode = os.stat(ACCESS_LOG_PATH).st_ino
    except OSError:
        offset = 0

    while True:
        try:
            current_inode = os.stat(ACCESS_LOG_PATH).st_ino
        except OSError:
            yield ""
            time.sleep(0.3)
            continue

        # إذا تغيّر الـ inode (تدوير اللوج)، نبدأ من البداية
        if inode is None:
            inode = current_inode
            offset = 0
        elif inode != current_inode:
            inode = current_inode
            offset = 0

        try:
            with open(ACCESS_LOG_PATH, "r") as f:
                f.seek(offset)
                chunk = f.read(LOG_READ_CHUNK)
                new_offset = f.tell()
                if new_offset > offset:
                    offset = new_offset
                    yield chunk
                else:
                    # لا يوجد جديد
                    yield ""
        except OSError:
            yield ""
            time.sleep(0.3)


def extract_ip_email_pairs(text):
    """يستخرج أزواج (ip, email) من نص اللوج"""
    pairs = []
    for m in LOG_RE.finditer(text):
        ip = m.group(1)
        email = m.group(2).rstrip(",")
        pairs.append((ip, email))
    return pairs


# ==============================================================================
# إعادة بناء إعدادات Xray
# ==============================================================================
def restart_xray(users):
    config = {
        "log": {
            "access": "/var/log/xray/access.log",
            "error": "/var/log/xray/error.log",
            "loglevel": "warning",
        },
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}
        },
        "inbounds": [
            {
                "port": 5000,
                "listen": "127.0.0.1",
                "protocol": "vless",
                "tag": "vless-inbound",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": "/@pycorav1"},
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
            {"protocol": "blackhole", "tag": "api"},
        ],
    }
    for email, data in users.items():
        if data.get("quota_bytes", 0) > 0:
            config["inbounds"][0]["settings"]["clients"].append(
                {"id": data["uuid"], "email": email}
            )
    os.makedirs(os.path.dirname(XRAY_CONFIG_PATH), exist_ok=True)
    with open(XRAY_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    subprocess.run(["pkill", "-f", "xray"], stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    # وجّه stdout و stderr الخاص بـ Xray إلى ملف اللوج
    # لأن Cloud Run يجمع stdout فقط، لكن السكربت يحتاج قراءة الملف
    os.makedirs(os.path.dirname(ACCESS_LOG_PATH), exist_ok=True)
    log_file = open(ACCESS_LOG_PATH, "a")
    err_file = open("/var/log/xray/error.log", "a")
    subprocess.Popen(
        ["/usr/local/bin/xray", "run", "-config", XRAY_CONFIG_PATH],
        stdout=log_file,
        stderr=err_file,
    )
    log("Xray restarted.")


def get_user_traffic():
    cmd = [
        "/usr/local/bin/xray",
        "api",
        "statsquery",
        "--server=127.0.0.1:10085",
        "-pattern",
        "user",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log(f"Statsquery failed (will retry): {result.stderr.strip()}")
            return None
        data = json.loads(result.stdout)
        traffic = {}
        for item in data.get("stat", []):
            name = item["name"]
            value = int(item["value"])
            if "user>>>" in name and ">>>traffic>>>" in name:
                parts = name.split(">>>")
                email = parts[1]
                traffic[email] = traffic.get(email, 0) + value
        return traffic
    except Exception as e:
        log(f"❌ Statsquery exception: {e}")
        return None


# ==============================================================================
# نقطة الانطلاق
# ==============================================================================
os.makedirs("/var/log/xray", exist_ok=True)
open("/var/log/xray/access.log", "a").close()
users = get_all_users()
restart_xray(users)
time.sleep(2)

last_stats = None
for attempt in range(15):
    last_stats = get_user_traffic()
    if last_stats is not None:
        break
    time.sleep(1)
if last_stats is None:
    last_stats = {}
log(f"Initial stats: {last_stats}")

last_active = {e for e, d in users.items() if d.get("quota_bytes", 0) > 0}
last_sync_time = time.time()
last_quota_check = time.time()

# ==============================================================================
# حالة منع مشاركة الحساب
# ==============================================================================
# email -> {"ip": "x.x.x.x", "first_seen": timestamp, "last_seen": timestamp}
active_ips = {}
# email -> timestamp متى ينتهي الحظر (None = غير محظور)
blocked_users = {}  # email -> unblock_at_timestamp
last_ip_check = time.time()

# نبدأ بقراءة اللوج من البداية لمسح الحالة الحالية
log_gen = tail_access_log()


while True:
    # --- 1) فحص الحصة (الكوتا) ---
    if time.time() - last_quota_check >= 3:
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
                        log(
                            f"📉 {email}: -{used} bytes, remaining {new_quota} bytes"
                        )

                        if email in users:
                            users[email]["quota_bytes"] = new_quota

                        if new_quota <= 0:
                            xray_api_remove_user(email)
                            log(f"🚫 Quota finished: {email}")
                            # نظّف حالة IP لهذا المستخدم
                            if email in active_ips:
                                del active_ips[email]

            last_stats = current_stats
        else:
            log("⚠️ Skipping quota check (statsquery not ready)")

        last_quota_check = time.time()

    # --- 2) مزامنة المستخدمين مع Redis ---
    if time.time() - last_sync_time >= 20:
        try:
            users = get_all_users()
            current_active = {
                e for e, d in users.items() if d.get("quota_bytes", 0) > 0
            }
            if current_active - last_active:
                log("New/returned users, restarting Xray...")
                restart_xray(users)
                time.sleep(2)
                for _ in range(15):
                    new_stats = get_user_traffic()
                    if new_stats is not None:
                        last_stats = new_stats
                        break
                    time.sleep(1)
            last_active = current_active
            last_sync_time = time.time()
        except Exception as e:
            log(f"❌ Sync error: {e}")

    # --- 3) فك الحظر عن المستخدمين المحظورين بعد انتهاء المدة ---
    now = time.time()
    for email, unblock_at in list(blocked_users.items()):
        if now >= unblock_at:
            unblock_user(email)
            del blocked_users[email]
            # امسح حالة IP حتى يُسجَّل من جديد عند إعادة الاتصال
            if email in active_ips:
                del active_ips[email]

    # --- 4) منع مشاركة الحساب: IP واحد فقط لكل UUID ---
    if now - last_ip_check >= IP_CHECK_INTERVAL:
        # اقرأ أي أسطر جديدة في اللوج
        new_log_text = ""
        try:
            new_log_text = next(log_gen)
        except StopIteration:
            log_gen = tail_access_log()
            new_log_text = ""

        if new_log_text:
            pairs = extract_ip_email_pairs(new_log_text)
            for ip, email in pairs:
                # هل المستخدم معروف ولديه كوتا؟
                if email not in users:
                    continue
                if users[email].get("quota_bytes", 0) <= 0:
                    continue

                entry = active_ips.get(email)

                if entry is None:
                    # أول اتصال لهذا المستخدم
                    active_ips[email] = {
                        "ip": ip,
                        "first_seen": now,
                        "last_seen": now,
                    }
                    log(f"🟢 {email} connected from {ip}")

                elif entry["ip"] == ip:
                    # نفس IP — حدّث آخر ظهور
                    entry["last_seen"] = now

                else:
                    # IP مختلف! هذا هو سيناريو مشاركة الحساب
                    old_ip = entry["ip"]
                    log(
                        f"🚨 DUPLICATE IP DETECTED: {email} "
                        f"was on {old_ip}, now connecting from {ip}"
                    )

                    # اقطع الاتصال فوراً واحظر الـ UUID لمدة BLOCK_DURATION ثانية
                    if kick_and_block_user(email):
                        blocked_users[email] = now + BLOCK_DURATION
                        # امسح حالة IP — الـ UUID محظور الآن
                        if email in active_ips:
                            del active_ips[email]
                        log(
                            f"🚫 {email}: connection CUT, UUID blocked "
                            f"for {BLOCK_DURATION}s (was {old_ip}, tried {ip})"
                        )
                    else:
                        log(
                            f"❌ Failed to kick {email}, keeping old IP {old_ip}"
                        )

        # نظّف IPs المنتهية (لم تظهر منذ IP_EXPIRY_SECONDS)
        expired = []
        for email, entry in list(active_ips.items()):
            if now - entry["last_seen"] > IP_EXPIRY_SECONDS:
                expired.append((email, entry["ip"]))
                del active_ips[email]
        for email, old_ip in expired:
            log(f"⏰ {email} IP {old_ip} expired (no activity)")

        last_ip_check = now

    time.sleep(0.5)
