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


def log(msg):
    print(f"[MANAGER] {msg}", flush=True)


try:
    # تفعيل decode_responses=True لضمان قراءة النصوص مباشرة
    r = redis.from_url(REDIS_URL, decode_responses=True, max_connections=5)
    r.ping()
    log("✅ Connected to Redis.")
except Exception as e:
    log(f"❌ Redis error: {e}")
    r = None

# ==============================================================================
# كود Lua الخصم الذري: يخصم البايتات فقط داخل Redis دون المساس بالنقاط أو الحقول الأخرى
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
    """خصم استهلاك الترافيك أتمياً في Redis دون مسح النقاط أو تعديلات البوت"""
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

while True:
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
                    # الخصم الأتمي في Redis فوراً بدلاً من حفظ كائن JSON الكامل
                    new_quota = atomic_deduct_user_bytes(email, used)

                    if new_quota >= 0:
                        log(
                            f"📉 {email}: -{used} bytes, remaining {new_quota} bytes"
                        )

                        # تحديث النسخة المؤقتة محلياً فقط للفحص
                        if email in users:
                            users[email]["quota_bytes"] = new_quota

                        if new_quota <= 0:
                            subprocess.run(
                                f'/usr/local/bin/xray api rmu --server=127.0.0.1:10085 -tag="vless-inbound" "{email}"',
                                shell=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            log(f"🚫 Quota finished: {email}")

            last_stats = current_stats
        else:
            log("⚠️ Skipping quota check (statsquery not ready)")

        last_quota_check = time.time()

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

    time.sleep(0.5)
