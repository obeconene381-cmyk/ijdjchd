import json
import os
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
XRAY_API_SERVER = "127.0.0.1:10085"
XRAY_INBOUND_TAG = "vless-inbound"

# كل كم ثانية نفحص الـ IPs النشطة
IP_CHECK_INTERVAL = 2.0
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
# دوال إدارة المستخدمين عبر Xray API
# ==============================================================================
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
    """يحذف مستخدم عبر Xray API — يقطع كل اتصالاته فوراً"""
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
    3) إعادة الإضافة تتم تلقائياً بعد انتهاء مدة الحظر
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

    log(f"🔄 Unblocking {email} (adu) — re-adding to allow connections...")
    if not xray_api_add_user(email, uuid):
        log(f"❌ Failed to re-add {email} after block!")
        return False
    log(f"✅ {email} unblocked — connections allowed again.")
    return True


# ==============================================================================
# دالة جديدة: الحصول على IPs النشطة لكل المستخدمين عبر Xray API
# ==============================================================================
def get_online_users_ips():
    """
    يستعلم من Xray API عن كل المستخدمين المتصلين و IPs النشطة لكل واحد.
    
    يستخدم الأمر: xray api statsonlineiplist --server=... -all
    
    يعيد dict: { email: [ip1, ip2, ...], ... }
    """
    try:
        result = subprocess.run(
            [
                "/usr/local/bin/xray", "api", "statsonlineiplist",
                f"--server={XRAY_API_SERVER}",
                "-all",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # قد يفشل إذا لا يوجد مستخدمون متصلون بعد
            return {}

        data = json.loads(result.stdout)
        users_ips = {}

        # الصيغة الجديدة (مع -all): { "users": [ { "email": "...", "ips": [...] } ] }
        if "users" in data:
            for user in data["users"]:
                email = user.get("email", "")
                ips = [entry["ip"] for entry in user.get("ips", [])]
                if email and ips:
                    users_ips[email] = ips

        # الصيغة القديمة: { "ips": { "1.2.3.4": timestamp } }
        elif "ips" in data:
            # هذا صيغة لمستخدم واحد - لن يحدث مع -all
            pass

        return users_ips
    except json.JSONDecodeError:
        return {}
    except Exception as e:
        log(f"❌ statsonlineiplist error: {e}")
        return {}


# ==============================================================================
# إعادة بناء إعدادات Xray — مع statsUserOnline
# ==============================================================================
def restart_xray(users):
    config = {
        "log": {
            "access": "none",
            "error": "/var/log/xray/error.log",
            "loglevel": "warning",
        },
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "statsUserOnline": True,  # مطلوب لـ statsonlineiplist
                }
            }
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
    subprocess.Popen(
        ["/usr/local/bin/xray", "run", "-config", XRAY_CONFIG_PATH]
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
# email -> أول IP مسجّل للمستخدم
active_ips = {}  # email -> "x.x.x.x"
# email -> timestamp متى ينتهي الحظر
blocked_users = {}  # email -> unblock_at_timestamp
last_ip_check = time.time()


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
            if email in active_ips:
                del active_ips[email]

    # --- 4) منع مشاركة الحساب: IP واحد فقط لكل UUID ---
    #      نستخدم Xray API مباشرة بدلاً من قراءة اللوج
    if now - last_ip_check >= IP_CHECK_INTERVAL:
        # استعلم عن كل المستخدمين المتصلين و IPs النشطة
        online_users = get_online_users_ips()

        if online_users:
            log(f"📡 Online users: {online_users}")

        for email, ips in online_users.items():
            # هل المستخدم معروف ولديه كوتا؟
            if email not in users:
                continue
            if users[email].get("quota_bytes", 0) <= 0:
                continue

            # هل المستخدم محظور حالياً؟ تجاهله
            if email in blocked_users:
                continue

            # كم IP نشط لديه؟
            if len(ips) <= 1:
                # IP واحد فقط — مسموح، سجّله
                if email not in active_ips and ips:
                    active_ips[email] = ips[0]
                    log(f"🟢 {email} connected from {ips[0]}")
                elif email in active_ips and ips:
                    # تأكد أنه نفس IP
                    if ips[0] != active_ips[email]:
                        # IP تغيّر — حدّث
                        active_ips[email] = ips[0]
                        log(f"🔄 {email} IP changed to {ips[0]}")
                continue

            # أكثر من IP! هذا هو سيناريو مشاركة الحساب
            log(
                f"🚨 DUPLICATE IP DETECTED: {email} "
                f"has {len(ips)} IPs: {', '.join(ips)}"
            )

            # اقطع الاتصال فوراً واحظر الـ UUID لمدة BLOCK_DURATION ثانية
            if kick_and_block_user(email):
                blocked_users[email] = now + BLOCK_DURATION
                if email in active_ips:
                    del active_ips[email]
                log(
                    f"🚫 {email}: connection CUT, UUID blocked "
                    f"for {BLOCK_DURATION}s (IPs: {', '.join(ips)})"
                )
            else:
                log(f"❌ Failed to kick {email}")

        last_ip_check = now

    time.sleep(0.5)
