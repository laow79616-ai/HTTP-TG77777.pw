#!/usr/bin/env python3
"""TG用户名检测工具 - 后端Agent
运行在8899端口，提供水军管理、批量检测、关键词黑名单等API
修复：使用独立线程运行asyncio事件循环，解决Telethon兼容性问题
"""
import os
import json
import time
import asyncio
import threading
import random
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify



app = Flask(__name__)

# ============ 配置 ============
AUTH_KEY = "fe570f573d2840308f6a298daa3ad4a0"
API_POOL_FILE = "/root/bot_agent/api_pool.json"
PROXY_POOL_FILE = "/root/bot_agent/proxy_pool.json"
COOLDOWN_FILE = "/root/bot_agent/bot_cooldown.json"
DAILY_STATS_FILE = "/root/bot_agent/bot_daily_stats.json"
DEFAULT_DAILY_LIMIT = 500
MAX_BATCH_SIZE = 300

LOGIN_USER = "admin"
LOGIN_PASS = "Ab123456987"
CONFIG_FILE = "/root/bot_agent/config.json"
BLACKLIST_FILE = "/root/bot_agent/blacklist.json"
AVAILABLE_FILE = "/root/bot_agent/available_usernames.json"
PREMIUM_FILE = "/root/bot_agent/premium_usernames.json"
TARGETS_FILE = "/root/bot_agent/target_usernames.json"
SESSIONS_DIR = "/root/bot_agent/sessions"

# Telethon API 配置（多组轮换）
API_CONFIGS = [
    {"api_id": 31034207, "api_hash": "c6d49c6a93371381efb3fa3033d7c73a"},
    {"api_id": 37900420, "api_hash": "417d6f1b7e58418e81dff5b2c4f33943"},
    {"api_id": 38928927, "api_hash": "e5e54a03a61c3c9083899d9dffecabaf"},
    {"api_id": 36404424, "api_hash": "4fe722fcee7095dd78dab10ce3a7d1f0"},
    {"api_id": 24701885, "api_hash": "6FE8f295d213368f7b489982c68d6b6d"},
]

# 确保目录存在
os.makedirs("/root/bot_agent", exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ============ 独立的asyncio事件循环（在单独线程中运行） ============
_loop = asyncio.new_event_loop()
_loop_thread = None

def start_event_loop():
    """在独立线程中运行事件循环"""
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def ensure_loop_running():
    """确保事件循环线程在运行"""
    global _loop_thread
    if _loop_thread is None or not _loop_thread.is_alive():
        _loop_thread = threading.Thread(target=start_event_loop, daemon=True)
        _loop_thread.start()

def run_async(coro):
    """在独立事件循环中运行协程并等待结果"""
    ensure_loop_running()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=60)

# ============ 工具函数 ============
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"bots": [], "settings": {}}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, 'r') as f:
            return json.load(f)
    return []

def save_blacklist(keywords):
    with open(BLACKLIST_FILE, 'w') as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)

def load_available():
    if os.path.exists(AVAILABLE_FILE):
        with open(AVAILABLE_FILE, 'r') as f:
            return json.load(f)
    return []

def save_available(usernames):
    with open(AVAILABLE_FILE, 'w') as f:
        json.dump(usernames, f, ensure_ascii=False, indent=2)


def load_targets():
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, "r") as f:
            return json.load(f)
    return []

def save_targets(usernames):
    with open(TARGETS_FILE, "w") as f:
        json.dump(usernames, f, ensure_ascii=False, indent=2)

def load_premium():
    if os.path.exists(PREMIUM_FILE):
        with open(PREMIUM_FILE, 'r') as f:
            return json.load(f)
    return []

def save_premium(usernames):
    with open(PREMIUM_FILE, 'w') as f:
        json.dump(usernames, f, ensure_ascii=False, indent=2)

# ============ 认证中间件 ============

# 广告用户识别关键词（昵称/用户名命中则标记为广告）
AD_KEYWORDS = [
    "代理", "开户", "博彩", "网赌", "赌", "彩票", "六合", "时时彩",
    "兼职", "日结", "刷单", "打字员", "招聘", "招代理",
    "加微信", "加v", "加V", "威信", "薇信",
    "优惠", "折扣", "促销", "免费领", "领取", "红包",
    "约炮", "交友", "裸聊", "色情",
    "币商", "换汇", "USDT", "承兑", "代付", "代收",
    "出粉", "引流", "精准粉", "电报粉",
    "赌场", "棋牌", "菠菜", "百乐", "视讯",
]

def is_ad_account(username, first_name="", last_name=""):
    text = f"{username or ''} {first_name or ''} {last_name or ''}".lower()
    for kw in AD_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

def is_bot_like_username(username):
    import re
    u = (username or "").strip().lstrip("@")
    if not u:
        return True
    # 正常短用户名（如 durov）不算水军
    digits = sum(1 for ch in u if ch.isdigit())
    if digits == 0 and len(u) <= 12:
        return False
    if len(u) >= 12 and digits >= 5:
        return True
    # 英文名+至少5位数字：典型批量马甲
    if re.match(r"^[A-Za-z]{3,}\d{5,}$", u):
        return True
    if re.match(r"^[A-Za-z]{4,}[A-Za-z]+\d{5,}$", u):
        return True
    if re.search(r"\d{6,}", u):
        return True
    return False

def classify_last_online(status_obj):
    from datetime import datetime, timezone, timedelta
    if status_obj is None:
        return "unknown"
    name = type(status_obj).__name__
    if name in ("UserStatusOnline", "UserStatusRecently", "UserStatusLastWeek"):
        return "active"
    if name == "UserStatusLastMonth":
        return "active"
    if name == "UserStatusOffline":
        was = getattr(status_obj, "was_online", None)
        if was is None:
            return "unknown"
        if getattr(was, "tzinfo", None) is None:
            was = was.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - was > timedelta(days=30):
            return "stale"
        return "active"
    if name in ("UserStatusEmpty",):
        return "unknown"
    return "unknown"


def load_api_pool():
    if os.path.exists(API_POOL_FILE):
        with open(API_POOL_FILE, "r") as f:
            return json.load(f)
    return list(API_CONFIGS)

def save_api_pool(pool):
    with open(API_POOL_FILE, "w") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

def load_proxy_pool():
    if os.path.exists(PROXY_POOL_FILE):
        with open(PROXY_POOL_FILE, "r") as f:
            return json.load(f)
    return []

def save_proxy_pool(pool):
    with open(PROXY_POOL_FILE, "w") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

def pick_api_evenly():
    """按当前水军已占用的 api_id 数量，选使用最少的 API"""
    pool = load_api_pool()
    if not pool:
        return random.choice(API_CONFIGS)
    config = load_config()
    usage = {}
    for b in config.get("bots", []):
        aid = str(b.get("api_id") or "")
        if aid:
            usage[aid] = usage.get(aid, 0) + 1
    best = None
    best_count = 10**9
    for item in pool:
        aid = str(item.get("api_id"))
        cnt = usage.get(aid, 0)
        if cnt < best_count:
            best_count = cnt
            best = item
    return best or pool[0]

def pick_proxy_evenly():
    """选使用最少的代理；池为空则返回 None"""
    pool = load_proxy_pool()
    if not pool:
        return None
    config = load_config()
    usage = {}
    for b in config.get("bots", []):
        px = b.get("proxy") or ""
        if px:
            usage[px] = usage.get(px, 0) + 1
    best = None
    best_count = 10**9
    for item in pool:
        px = item if isinstance(item, str) else (item.get("proxy") or item.get("url") or "")
        if not px:
            continue
        cnt = usage.get(px, 0)
        if cnt < best_count:
            best_count = cnt
            best = px
    return best

def parse_proxy(proxy_url):
    """支持:
    - socks5://user:pass@host:port
    - http://host:port
    - host:port:user:pass
    - host:port
    """
    if not proxy_url:
        return None
    try:
        import python_socks
        from urllib.parse import urlparse
        u = str(proxy_url).strip()
        # host:port:user:pass
        if "://" not in u and u.count(":") >= 3:
            parts = u.split(":")
            host, port, user, pwd = parts[0], int(parts[1]), parts[2], ":".join(parts[3:])
            return (python_socks.ProxyType.SOCKS5, host, port, True, user, pwd)
        if "://" not in u and u.count(":") == 1:
            host, port = u.split(":")
            return (python_socks.ProxyType.SOCKS5, host, int(port), True, None, None)
        if "://" not in u:
            u = "socks5://" + u
        p = urlparse(u)
        scheme = (p.scheme or "socks5").lower()
        host = p.hostname
        port = p.port or 1080
        user = p.username
        pwd = p.password
        if scheme.startswith("socks5"):
            ptype = python_socks.ProxyType.SOCKS5
        elif scheme.startswith("socks4"):
            ptype = python_socks.ProxyType.SOCKS4
        else:
            ptype = python_socks.ProxyType.HTTP
        return (ptype, host, port, True, user, pwd)
    except Exception as e:
        print("parse_proxy error", proxy_url, e)
        return None



def load_cooldown():
    import json, os, time
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        data = json.load(open(COOLDOWN_FILE))
    except Exception:
        return {}
    now = time.time()
    # 清过期
    data = {k: v for k, v in data.items() if float(v.get("until", 0) or 0) > now}
    return data

def save_cooldown(data):
    import json
    json.dump(data, open(COOLDOWN_FILE, "w"), ensure_ascii=False, indent=2)

def set_bot_cooldown(bot_key, seconds, reason="FloodWait"):
    import time
    data = load_cooldown()
    until = time.time() + max(int(seconds), 60)
    data[str(bot_key)] = {"until": until, "reason": reason, "seconds": int(seconds)}
    save_cooldown(data)
    return until

def is_bot_cooling(bot_key):
    import time
    data = load_cooldown()
    item = data.get(str(bot_key))
    if not item:
        return False, 0
    left = float(item.get("until", 0)) - time.time()
    return left > 0, max(int(left), 0)

def load_daily_stats():
    import json, os, datetime
    today = datetime.date.today().isoformat()
    if not os.path.exists(DAILY_STATS_FILE):
        return {"date": today, "counts": {}}
    try:
        data = json.load(open(DAILY_STATS_FILE))
    except Exception:
        return {"date": today, "counts": {}}
    if data.get("date") != today:
        return {"date": today, "counts": {}}
    return data

def save_daily_stats(data):
    import json
    json.dump(data, open(DAILY_STATS_FILE, "w"), ensure_ascii=False, indent=2)

def incr_bot_daily(bot_key):
    data = load_daily_stats()
    k = str(bot_key)
    data.setdefault("counts", {})
    data["counts"][k] = int(data["counts"].get(k, 0)) + 1
    save_daily_stats(data)
    return data["counts"][k]

def bot_daily_left(bot_key, limit=None):
    limit = int(limit or DEFAULT_DAILY_LIMIT)
    data = load_daily_stats()
    used = int(data.get("counts", {}).get(str(bot_key), 0))
    return max(limit - used, 0), used, limit

def list_workable_bots(config=None):
    """未冷却且未超每日上限的水军"""
    if config is None:
        config = load_config()
    bots = config.get("bots") or []
    out = []
    for b in bots:
        key = b.get("id") or b.get("phone") or b.get("session_path")
        cooling, left = is_bot_cooling(key)
        if cooling:
            b = dict(b)
            b["_status"] = "cooldown"
            b["_cooldown_left"] = left
            continue
        remain, used, limit = bot_daily_left(key)
        if remain <= 0:
            continue
        bb = dict(b)
        bb["_status"] = "work"
        bb["_daily_used"] = used
        bb["_daily_left"] = remain
        bb["_daily_limit"] = limit
        out.append(bb)
    return out


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if auth != f'Bearer {AUTH_KEY}':
            return jsonify({"error": "未授权"}), 401
        return f(*args, **kwargs)
    return decorated

# ============ 登录接口 ============
@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username', '')
    password = data.get('password', '')
    if username == LOGIN_USER and password == LOGIN_PASS:
        return jsonify({"success": True, "token": AUTH_KEY})
    return jsonify({"error": "用户名或密码错误"}), 401

# ============ 水军管理接口 ============
@app.route('/api/bot/list', methods=['GET'])
@require_auth
def api_bot_list():
    config = load_config()
    bots = config.get('bots', [])
    safe_bots = []
    for bot in bots:
        safe_bots.append({
            "id": bot.get("id", ""),
            "name": bot.get("name", ""),
            "username": bot.get("username", ""),
            "phone": bot.get("phone", ""),
            "first_name": bot.get("first_name", ""),
            "status": bot.get("status", "unknown"),
            "type": bot.get("type", "userbot"),
            "added_time": bot.get("added_time", "")
        })
    return jsonify({"bots": safe_bots})

@app.route('/api/bot/remove', methods=['POST'])
@require_auth
def api_bot_remove():
    data = request.json or {}
    bot_id = data.get('id', '') or data.get('bot_id', '')
    config = load_config()
    config['bots'] = [b for b in config.get('bots', []) if b.get('id') != bot_id]
    save_config(config)
    return jsonify({"success": True, "message": "水军已删除"})

@app.route('/api/bot/start', methods=['POST'])
@require_auth
def api_bot_start():
    """启动水军：校验 session 可用后标记为 running"""
    data = request.json or {}
    bot_id = data.get('bot_id', '') or data.get('id', '')
    config = load_config()
    bots = config.get('bots', [])
    found = None
    for b in bots:
        if b.get('id') == bot_id:
            found = b
            break
    if not found:
        return jsonify({"error": "水军不存在"}), 404

    session_path = found.get('session_path', '')
    if not session_path:
        return jsonify({"error": "无 session 文件，请重新登录添加"}), 400

    # 尝试连接验证
    try:
        from telethon import TelegramClient
        api_id = int(found.get('api_id') or API_CONFIGS[0]['api_id'])
        api_hash = found.get('api_hash') or API_CONFIGS[0]['api_hash']

        async def _check():
            client = TelegramClient(session_path, api_id, api_hash, loop=_loop)
            await client.connect()
            ok = await client.is_user_authorized()
            await client.disconnect()
            return ok

        ok = run_async(_check())
        if not ok:
            return jsonify({"error": "Session 已失效，请重新登录该水军"}), 400
    except Exception as e:
        return jsonify({"error": f"启动失败: {str(e)}"}), 400

    found['status'] = 'running'
    save_config(config)
    return jsonify({"success": True, "message": f"水军 {found.get('name', bot_id)} 已启动", "status": "running"})

@app.route('/api/bot/stop', methods=['POST'])
@require_auth
def api_bot_stop():
    data = request.json or {}
    bot_id = data.get('bot_id', '') or data.get('id', '')
    config = load_config()
    found = None
    for b in config.get('bots', []):
        if b.get('id') == bot_id:
            found = b
            break
    if not found:
        return jsonify({"error": "水军不存在"}), 404
    found['status'] = 'ready'
    save_config(config)
    return jsonify({"success": True, "message": f"水军 {found.get('name', bot_id)} 已停止", "status": "ready"})

@app.route('/api/bot/delete', methods=['POST'])
@require_auth
def api_bot_delete():
    """兼容前端 delete 调用"""
    data = request.json or {}
    bot_id = data.get('bot_id', '') or data.get('id', '')
    config = load_config()
    config['bots'] = [b for b in config.get('bots', []) if b.get('id') != bot_id]
    save_config(config)
    return jsonify({"success": True, "message": "水军已删除"})

# ============ Telethon 手机号登录（异步） ============
pending_clients = {}
FLOOD_COOLDOWN = {}  # phone -> unix timestamp until
CHECK_LOCK = None

def get_api_config(api_id=None, api_hash=None):
    """添加水军默认从 API 池均匀分配；仅明确传入时才用自定义。"""
    if api_id and api_hash:
        try:
            return {"api_id": int(api_id), "api_hash": str(api_hash).strip()}
        except (ValueError, TypeError):
            pass
    try:
        picked = pick_api_evenly()
        return {"api_id": int(picked["api_id"]), "api_hash": str(picked["api_hash"])}
    except Exception:
        return random.choice(API_CONFIGS)

async def async_send_code(phone, api_id=None, api_hash=None):
    """异步发送验证码。支持自定义 api_id / api_hash。"""
    from telethon import TelegramClient
    
    api_config = get_api_config(api_id, api_hash)
    api_id = api_config["api_id"]
    api_hash = api_config["api_hash"]
    
    session_file = os.path.join(SESSIONS_DIR, f"session_{phone.replace('+', '').replace(' ', '')}")
    client = TelegramClient(session_file, api_id, api_hash, loop=_loop)
    await client.connect()
    
    result = await client.send_code_request(phone)
    
    pending_clients[phone] = {
        "client": client,
        "phone_code_hash": result.phone_code_hash,
        "api_id": api_id,
        "api_hash": api_hash,
        "session_file": session_file
    }
    
    return {"success": True, "message": f"验证码已发送到 {phone}", "phone_code_hash": result.phone_code_hash}

async def async_verify_code(phone, code, password=None):
    """异步验证码验证"""
    if phone not in pending_clients:
        return {"success": False, "error": "请先发送验证码"}
    
    pending = pending_clients[phone]
    client = pending["client"]
    phone_code_hash = pending["phone_code_hash"]
    
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        err_str = str(e)
        if "Two-steps verification" in err_str or "password" in err_str.lower() or "SessionPasswordNeeded" in err_str:
            if not password:
                return {"success": False, "error": "该账号需要两步验证密码"}
            await client.sign_in(password=password)
        else:
            raise e
    
    me = await client.get_me()
    account = {
        "username": me.username or "",
        "phone": phone,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "user_id": me.id
    }
    
    await client.disconnect()
    del pending_clients[phone]
    
    return {
        "success": True,
        "account": account,
        "session_file": pending["session_file"],
        "api_id": pending["api_id"],
        "api_hash": pending["api_hash"]
    }

@app.route('/api/bot/send_code', methods=['POST'])
@require_auth
def api_send_code():
    """发送验证码到手机号。支持自定义 api_id / api_hash。"""
    data = request.json or {}
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({"error": "请提供手机号"}), 400
    
    # 添加水军不再使用前端 API，统一从 API 池均匀分配
    api_id = None
    api_hash = None
    
    try:
        result = run_async(async_send_code(phone, api_id=api_id, api_hash=api_hash))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"发送验证码失败: {str(e)}"}), 400

@app.route('/api/bot/verify', methods=['POST'])
@require_auth
def api_verify_code():
    """验证码验证并添加水军"""
    data = request.json
    phone = data.get('phone', '').strip()
    code = data.get('code', '').strip()
    password = data.get('password', '').strip()
    name = data.get('name', '').strip()
    
    if not phone or not code:
        return jsonify({"error": "请提供手机号和验证码"}), 400
    
    try:
        result = run_async(async_verify_code(phone, code, password if password else None))
        
        if not result["success"]:
            return jsonify({"error": result["error"]}), 400
        
        # 登录成功，添加到水军列表
        account = result["account"]
        config = load_config()
        bot_id = f"soldier_{name}_{int(time.time())}"
        new_bot = {
            "id": bot_id,
            "name": name if name else account.get("first_name", phone),
            "username": account.get("username", ""),
            "phone": phone,
            "first_name": account.get("first_name", ""),
            "session_path": result.get("session_file", ""),
            "api_id": str(result.get("api_id", "")),
            "api_hash": result.get("api_hash", ""),
            "proxy": pick_proxy_evenly() or "",
            "status": "ready",
            "type": "userbot",
            "added_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        config.setdefault('bots', []).append(new_bot)
        save_config(config)
        
        return jsonify({
            "success": True,
            "message": "水军添加成功",
            "account": account
        })
    except Exception as e:
        return jsonify({"error": f"验证失败: {str(e)}"}), 400


# ============ API 池 / 代理池 ============
@app.route('/api/pool/api', methods=['GET'])
@require_auth
def api_pool_list():
    return jsonify({"pool": load_api_pool(), "total": len(load_api_pool())})

@app.route('/api/pool/api', methods=['POST'])
@require_auth
def api_pool_add():
    data = request.json or {}
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    label = data.get("label") or f"API-{api_id}"
    if not api_id or not api_hash:
        return jsonify({"error": "需要 api_id 和 api_hash"}), 400
    pool = load_api_pool()
    # 去重
    pool = [x for x in pool if str(x.get("api_id")) != str(api_id)]
    pool.append({"api_id": int(api_id), "api_hash": str(api_hash).strip(), "label": label})
    save_api_pool(pool)
    return jsonify({"success": True, "total": len(pool), "pool": pool})


@app.route('/api/pool/api/batch', methods=['POST'])
@require_auth
def api_pool_add_batch():
    """批量添加 API。支持每行: api_id-api_hash 或 api_id,api_hash 或 api_id api_hash"""
    data = request.json or {}
    text = data.get("lines") or data.get("text") or ""
    if isinstance(text, list):
        lines = [str(x).strip() for x in text if str(x).strip()]
    else:
        lines = [x.strip() for x in str(text).replace("\r", "\n").split("\n") if x.strip()]
    pool = load_api_pool()
    existing = {str(x.get("api_id")) for x in pool}
    added = 0
    for line in lines:
        line = line.strip()
        api_id, api_hash = None, None
        if "-" in line and line.split("-", 1)[0].strip().isdigit():
            a, b = line.split("-", 1)
            api_id, api_hash = a.strip(), b.strip()
        elif "," in line:
            a, b = line.split(",", 1)
            api_id, api_hash = a.strip(), b.strip()
        elif "\t" in line:
            a, b = line.split("\t", 1)
            api_id, api_hash = a.strip(), b.strip()
        else:
            parts = line.split()
            if len(parts) >= 2 and parts[0].isdigit():
                api_id, api_hash = parts[0], parts[1]
        if not api_id or not api_hash:
            continue
        if str(api_id) in existing:
            continue
        try:
            pool.append({"api_id": int(api_id), "api_hash": api_hash, "label": f"API-{api_id}"})
            existing.add(str(api_id))
            added += 1
        except Exception:
            continue
    save_api_pool(pool)
    return jsonify({"success": True, "added": added, "total": len(pool)})

@app.route('/api/pool/api/delete', methods=['POST'])
@require_auth
def api_pool_del():
    data = request.json or {}
    api_id = str(data.get("api_id", ""))
    pool = [x for x in load_api_pool() if str(x.get("api_id")) != api_id]
    save_api_pool(pool)
    return jsonify({"success": True, "total": len(pool)})

@app.route('/api/pool/api/redistribute', methods=['POST'])
@require_auth
def api_pool_redistribute():
    """一键均匀分配 API 到所有水军"""
    pool = load_api_pool()
    if not pool:
        return jsonify({"error": "API 池为空"}), 400
    config = load_config()
    bots = config.get("bots", [])
    for i, b in enumerate(bots):
        item = pool[i % len(pool)]
        b["api_id"] = int(item["api_id"])
        b["api_hash"] = item["api_hash"]
    save_config(config)
    return jsonify({"success": True, "message": f"已均匀分配 {len(bots)} 个水军", "bots": len(bots), "apis": len(pool)})

@app.route('/api/pool/proxy', methods=['GET'])
@require_auth
def proxy_pool_list():
    return jsonify({"pool": load_proxy_pool(), "total": len(load_proxy_pool())})

@app.route('/api/pool/proxy', methods=['POST'])
@require_auth
def proxy_pool_add():
    data = request.json or {}
    # 支持单条或批量 lines
    lines = data.get("proxies") or data.get("lines") or []
    if isinstance(lines, str):
        lines = [x.strip() for x in lines.split("\n") if x.strip()]
    single = data.get("proxy") or data.get("url")
    if single:
        lines.append(single)
    pool = load_proxy_pool()
    existing = set()
    for x in pool:
        existing.add(x if isinstance(x, str) else (x.get("proxy") or x.get("url") or ""))
    added = 0
    for line in lines:
        line = line.strip()
        if not line or line in existing:
            continue
        pool.append({"proxy": line, "label": line[:40]})
        existing.add(line)
        added += 1
    save_proxy_pool(pool)
    return jsonify({"success": True, "added": added, "total": len(pool)})

@app.route('/api/pool/proxy/delete', methods=['POST'])
@require_auth
def proxy_pool_del():
    data = request.json or {}
    px = data.get("proxy") or ""
    pool = []
    for x in load_proxy_pool():
        val = x if isinstance(x, str) else (x.get("proxy") or x.get("url") or "")
        if val != px:
            pool.append(x)
    save_proxy_pool(pool)
    return jsonify({"success": True, "total": len(pool)})

@app.route('/api/pool/proxy/redistribute', methods=['POST'])
@require_auth
def proxy_pool_redistribute():
    """一键均匀分配代理到所有水军"""
    pool = load_proxy_pool()
    if not pool:
        return jsonify({"error": "代理池为空"}), 400
    config = load_config()
    bots = config.get("bots", [])
    for i, b in enumerate(bots):
        item = pool[i % len(pool)]
        px = item if isinstance(item, str) else (item.get("proxy") or item.get("url") or "")
        b["proxy"] = px
    save_config(config)
    return jsonify({"success": True, "message": f"已均匀分配代理到 {len(bots)} 个水军", "bots": len(bots), "proxies": len(pool)})


# ============ 批量检测接口（真实 Telegram 查询） ============

async def async_check_one_username(client, username):
    """真实检测单个用户名：available / taken / deleted / invalid / error"""
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError, FloodWaitError
    try:
        result = await client(ResolveUsernameRequest(username))
        users = getattr(result, 'users', None) or []
        if users:
            user = users[0]
            if getattr(user, 'deleted', False):
                return {"username": username, "status": "deleted", "premium": False}
            is_premium = bool(getattr(user, 'premium', False))
            fn = getattr(user, 'first_name', '') or ''
            ln = getattr(user, 'last_name', '') or ''
            ad = is_ad_account(username, fn, ln)
            return {
                "username": username,
                "status": "ad" if ad and not is_premium else "taken",
                "premium": is_premium,
                "is_ad": ad,
                "user_id": getattr(user, 'id', None),
                "first_name": fn,
                "last_name": ln,
            }
        chats = getattr(result, 'chats', None) or []
        if chats:
            return {"username": username, "status": "taken", "premium": False}
        return {"username": username, "status": "taken", "premium": False}
    except UsernameNotOccupiedError:
        return {"username": username, "status": "available", "premium": False}
    except UsernameInvalidError:
        return {"username": username, "status": "invalid", "premium": False}
    except FloodWaitError as e:
        return {"username": username, "status": "error", "error": f"FloodWait {e.seconds}s", "premium": False}
    except Exception as e:
        err = str(e)
        if "No user has" in err or "USERNAME_NOT_OCCUPIED" in err:
            return {"username": username, "status": "available", "premium": False}
        if "USERNAME_INVALID" in err:
            return {"username": username, "status": "invalid", "premium": False}
        return {"username": username, "status": "error", "error": err[:120], "premium": False}

async def async_check_usernames_batch(usernames, bot_sessions):
    """使用已登录水军轮流检测用户名"""
    from telethon import TelegramClient
    results = []
    if not bot_sessions:
        for u in usernames:
            results.append({"username": u, "status": "error", "error": "无可用水军账号"})
        return results

    clients = []
    try:
        for sess in bot_sessions:
            try:
                sp = sess["session_path"]
                # Telethon 接受不带 .session 后缀的路径
                client = TelegramClient(
                    sp,
                    int(sess["api_id"]),
                    str(sess["api_hash"]),
                )
                await client.connect()
                ok = await client.is_user_authorized()
                if ok:
                    clients.append(client)
                else:
                    await client.disconnect()
            except Exception as e:
                print(f"[check] session fail {sess.get('session_path')}: {e}")
                continue

        if not clients:
            for u in usernames:
                results.append({"username": u, "status": "error", "error": "水军未授权或session失效"})
            return results

        for i, username in enumerate(usernames):
            client = clients[i % len(clients)]
            r = await async_check_one_username(client, username)
            results.append(r)
            await asyncio.sleep(0.35 + random.random() * 0.4)

        return results
    finally:
        for c in clients:
            try:
                await c.disconnect()
            except Exception:
                pass

@app.route('/api/check', methods=['POST'])
@require_auth
def api_check_usernames():
    """真实批量检测用户名（available / taken / deleted / invalid）"""
    data = request.json or {}
    usernames = data.get('usernames', [])
    if not usernames:
        return jsonify({"error": "请提供用户名列表"}), 400

    cleaned = []
    seen = set()
    blacklist = load_blacklist()
    for u in usernames:
        u = u.strip().lstrip('@')
        if not u or u.lower() in seen:
            continue
        if any(kw.lower() in u.lower() for kw in blacklist):
            continue
        seen.add(u.lower())
        cleaned.append(u)

    if not cleaned:
        return jsonify({"results": [], "total": 0})

    config = load_config()
    bot_sessions = []
    for bot in config.get('bots', []):
        if bot.get('session_path'):
            bot_sessions.append({
                "session_path": bot["session_path"],
                "api_id": bot.get("api_id") or API_CONFIGS[0]["api_id"],
                "api_hash": bot.get("api_hash") or API_CONFIGS[0]["api_hash"],
            })

    try:
        results = run_async(async_check_usernames_batch(cleaned, bot_sessions))
        return jsonify({"results": results, "total": len(results)})
    except Exception as e:
        return jsonify({"error": f"检测失败: {str(e)}"}), 500


@app.route('/api/check/one', methods=['POST'])
@require_auth
def api_check_one():
    """检测单个用户名：只用水军工作池；FloodWait 写入冷却仓；尊重每日上限"""
    import time, asyncio
    data = request.json or {}
    username = (data.get('username') or '').strip().lstrip('@')
    if not username:
        return jsonify({"error": "请提供用户名", "status": "error", "username": ""}), 400

    config = load_config()
    workable = list_workable_bots(config)
    if not workable:
        # 区分全冷却 / 全日额满
        all_bots = config.get('bots') or []
        any_cool = False
        for b in all_bots:
            key = b.get('id') or b.get('phone') or ''
            cool, left = is_bot_cooling(key)
            if cool:
                any_cool = True
                break
        msg = "全部水军冷却中或已达每日上限" if any_cool or all_bots else "无可用水军账号"
        return jsonify({
            "username": username,
            "status": "flood" if any_cool else "error",
            "error": msg,
            "premium": False,
            "collect": False,
        })

    # 最多试 3 个工作号
    last_err = None
    max_try = min(3, len(workable))
    # 简单轮询：按 daily_used 少的优先
    workable = sorted(workable, key=lambda b: int(b.get('_daily_used') or 0))

    for i in range(max_try):
        bot = workable[i]
        bot_key = bot.get('id') or bot.get('phone') or bot.get('session_path')
        sp = bot.get('session_path')
        if not sp:
            continue
        try:
            api_id = int(bot.get('api_id') or 31034207)
            api_hash = str(bot.get('api_hash') or '')
            if not api_hash:
                continue

            async def _run():
                from telethon import TelegramClient
                from telethon.tl.functions.contacts import ResolveUsernameRequest
                from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError, FloodWaitError
                client = TelegramClient(sp, api_id, api_hash, loop=_loop)
                try:
                    await asyncio.wait_for(client.connect(), timeout=8)
                    if not await client.is_user_authorized():
                        return {"username": username, "status": "error", "error": "session未授权", "premium": False, "collect": False, "_bot": bot_key}
                    try:
                        result = await asyncio.wait_for(client(ResolveUsernameRequest(username)), timeout=8)
                    except FloodWaitError as e:
                        return {"username": username, "status": "flood", "error": f"FloodWait {e.seconds}s", "premium": False, "collect": False, "_flood_seconds": int(e.seconds), "_bot": bot_key}
                    except UsernameNotOccupiedError:
                        return {"username": username, "status": "available", "premium": False, "collect": False, "_bot": bot_key}
                    except UsernameInvalidError:
                        return {"username": username, "status": "invalid", "premium": False, "collect": False, "_bot": bot_key}

                    users = list(getattr(result, 'users', None) or [])
                    chats = list(getattr(result, 'chats', None) or [])
                    if users:
                        user = users[0]
                        if getattr(user, 'deleted', False):
                            return {"username": username, "status": "deleted", "premium": False, "collect": False, "_bot": bot_key}
                        is_premium = bool(getattr(user, 'premium', False))
                        is_bot = bool(getattr(user, 'bot', False))
                        fn = getattr(user, 'first_name', '') or ''
                        ln = getattr(user, 'last_name', '') or ''
                        ad = is_ad_account(username, fn, ln) if 'is_ad_account' in globals() else False
                        bot_like = (is_bot_like_username(username) if 'is_bot_like_username' in globals() else False) or is_bot
                        # 在线粗分
                        st_obj = getattr(user, 'status', None)
                        st_name = type(st_obj).__name__ if st_obj is not None else ''
                        inactive = st_name in ('UserStatusEmpty', 'UserStatusOffline') and False
                        # 若有 classify_last_online 则用
                        if 'classify_last_online' in dir() or 'classify_last_online' in globals():
                            try:
                                online_flag, inactive = classify_last_online(user)
                            except Exception:
                                inactive = st_name in ('UserStatusLastMonth', 'UserStatusEmpty')
                        if ad:
                            st = 'ad'
                            collect = False
                        elif bot_like:
                            st = 'spam'
                            collect = False
                        elif inactive:
                            st = 'inactive'
                            collect = False
                        else:
                            st = 'clean'
                            collect = True
                        return {
                            "username": username,
                            "status": st,
                            "premium": is_premium,
                            "collect": collect,
                            "is_ad": ad,
                            "is_spam": bot_like,
                            "first_name": fn,
                            "last_name": ln,
                            "user_id": getattr(user, 'id', None),
                            "online": st_name,
                            "_bot": bot_key,
                        }
                    if chats:
                        return {"username": username, "status": "unavailable", "premium": False, "collect": False, "entity_type": type(chats[0]).__name__, "_bot": bot_key}
                    return {"username": username, "status": "unavailable", "premium": False, "collect": False, "_bot": bot_key}
                finally:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

            out = run_async(_run())
            if not out:
                last_err = "empty result"
                continue

            # FloodWait → 冷却仓，换号或返回
            if out.get('status') == 'flood' or (out.get('error') or '').startswith('FloodWait'):
                sec = int(out.get('_flood_seconds') or 0)
                if not sec:
                    # 从 error 文本解析
                    import re as _re
                    m = _re.search(r'(\d+)\s*s', str(out.get('error') or ''))
                    sec = int(m.group(1)) if m else 3600
                set_bot_cooldown(bot_key, sec, reason=out.get('error') or 'FloodWait')
                last_err = out.get('error')
                # 尝试下一个工作号
                continue

            # 成功占用一次每日额度（含 clean/ad/spam/deleted 等有效响应）
            if out.get('status') not in ('error',):
                try:
                    incr_bot_daily(bot_key)
                except Exception:
                    pass

            # 去掉内部字段
            out.pop('_bot', None)
            out.pop('_flood_seconds', None)
            return jsonify(out)

        except Exception as e:
            last_err = str(e)[:160]
            # session 类错误不整池冷却，仅换号
            continue

    # 全部尝试失败
    return jsonify({
        "username": username,
        "status": "error",
        "error": last_err or "水军未授权或session失效",
        "premium": False,
        "collect": False,
    })



def api_check_one():
    """检测单个用户名：最多试3个水军，单号8秒超时，FloodWait自动冷却"""
    import time
    data = request.json or {}
    username = (data.get('username') or '').strip().lstrip('@')
    if not username:
        return jsonify({"error": "请提供用户名"}), 400

    config = load_config()
    bots = [b for b in config.get('bots', []) if b.get('session_path')]
    if not bots:
        return jsonify({"username": username, "status": "error", "error": "无可用水军账号"})

    now = time.time()
    # 过滤还在冷却的号
    ready = []
    for b in bots:
        phone = b.get("phone") or b.get("id") or ""
        until = FLOOD_COOLDOWN.get(phone, 0)
        if until > now:
            continue
        ready.append(b)
    if not ready:
        ready = bots  # 全在冷却则仍尝试，避免完全不可用

    start_idx = sum(ord(ch) for ch in username) % len(ready)
    ordered = ready[start_idx:] + ready[:start_idx]
    ordered = ordered[:3]  # 最多试3个号

    async def _one():
        from telethon import TelegramClient
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError, FloodWaitError
        last_err = "全部水军失败"
        for bot in ordered:
            phone = bot.get("phone") or bot.get("id") or ""
            sp = bot.get("session_path")
            client = None
            try:
                api_id = int(bot.get("api_id") or API_CONFIGS[0]["api_id"])
                api_hash = str(bot.get("api_hash") or API_CONFIGS[0]["api_hash"])
                # 暂时不用代理，避免断连
                client = TelegramClient(sp, api_id, api_hash)
                await asyncio.wait_for(client.connect(), timeout=8)
                if not await asyncio.wait_for(client.is_user_authorized(), timeout=5):
                    last_err = f"{phone} 未授权"
                    await client.disconnect()
                    continue
                try:
                    entity = await asyncio.wait_for(client.get_entity(username), timeout=10)
                    et = type(entity).__name__
                    if et == "User" or (hasattr(entity, "first_name") and hasattr(entity, "bot") and not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False)):
                        user = entity
                        if getattr(user, 'deleted', False):
                            out = {"username": username, "status": "deleted", "premium": False, "collect": False}
                        else:
                            fn = getattr(user, 'first_name', '') or ''
                            ln = getattr(user, 'last_name', '') or ''
                            premium = bool(getattr(user, 'premium', False))
                            is_bot = bool(getattr(user, 'bot', False))
                            ad = is_ad_account(username, fn, ln)
                            bot_like = is_bot_like_username(username) or is_bot
                            online_kind = classify_last_online(getattr(user, 'status', None))
                            if ad:
                                st = "ad"
                                collect = False
                            elif bot_like:
                                st = "spam"
                                collect = False
                            elif online_kind == "stale":
                                st = "inactive"
                                collect = False
                            else:
                                st = "clean"
                                collect = True
                            out = {
                                "username": username,
                                "status": st,
                                "premium": premium,
                                "is_ad": ad,
                                "is_spam": bot_like,
                                "online": online_kind,
                                "collect": collect,
                                "user_id": getattr(user, 'id', None),
                                "first_name": fn,
                                "last_name": ln,
                            }
                    else:
                        # 频道/群：不是个人用户采集对象
                        out = {"username": username, "status": "unavailable", "premium": False, "collect": False, "is_spam": False, "entity_type": type(entity).__name__}
                    await client.disconnect()
                    return out
                except UsernameNotOccupiedError:
                    await client.disconnect()
                    return {"username": username, "status": "available", "premium": False, "collect": False}
                except UsernameInvalidError:
                    await client.disconnect()
                    return {"username": username, "status": "invalid", "premium": False, "collect": False}
                except ValueError as e:
                    # get_entity 找不到时常见 ValueError
                    msg = str(e).lower()
                    await client.disconnect()
                    if "no user" in msg or "not found" in msg or "nobody" in msg:
                        return {"username": username, "status": "available", "premium": False, "collect": False}
                    return {"username": username, "status": "error", "error": str(e)[:100], "premium": False}
                except FloodWaitError as e:
                    # 冷却：最多记 6 小时，避免一次记 20 小时导致全废
                    sec = min(int(e.seconds), 6 * 3600)
                    FLOOD_COOLDOWN[phone] = time.time() + sec
                    last_err = f"FloodWait {e.seconds}s"
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue
                except asyncio.TimeoutError:
                    last_err = "查询超时"
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    last_err = str(e)[:100]
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    continue
            except asyncio.TimeoutError:
                last_err = "连接超时"
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                continue
            except Exception as e:
                last_err = str(e)[:100]
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                continue
        return {"username": username, "status": "error", "error": last_err, "premium": False}

    try:
        result = run_async(_one())
        return jsonify(result)
    except Exception as e:
        return jsonify({"username": username, "status": "error", "error": str(e)[:120]}), 500


@app.route('/api/check/result', methods=['POST'])
@require_auth
def api_check_result():
    """只允许 available 写入有用文档，deleted 一律拒绝"""
    data = request.json or {}
    items = data.get('available', data.get('results', []))
    
    available = load_available()
    blacklist = load_blacklist()
    added = 0
    skipped_deleted = 0

    for item in items:
        if isinstance(item, dict):
            username = (item.get('username') or '').strip().lstrip('@')
            status = item.get('status', 'available')
            if status == 'deleted':
                skipped_deleted += 1
                continue
            if status != 'available':
                continue
        else:
            username = str(item).strip().lstrip('@')

        if not username:
            continue
        formatted = f"@{username}"
        if any(kw.lower() in username.lower() for kw in blacklist):
            continue
        if formatted not in available:
            available.append(formatted)
            added += 1

    save_available(available)
    return jsonify({
        "success": True,
        "added": added,
        "skipped_deleted": skipped_deleted,
        "total": len(available)
    })

@app.route('/api/available', methods=['GET'])
@require_auth
def api_get_available():
    available = load_available()
    return jsonify({"usernames": available, "total": len(available)})

@app.route('/api/available/clear', methods=['POST'])
@require_auth
def api_clear_available():
    save_available([])
    return jsonify({"success": True, "message": "已清空"})

@app.route('/api/available/export', methods=['GET'])
@require_auth
def api_export_available():
    available = load_available()
    text = "\n".join(available)
    return text, 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ============ Telegram 会员（Premium）采集接口 ============

@app.route('/api/targets', methods=['GET'])
@require_auth
def api_get_targets():
    t = load_targets()
    return jsonify({"usernames": t, "total": len(t)})

@app.route('/api/targets/clear', methods=['POST'])
@require_auth
def api_clear_targets():
    save_targets([])
    return jsonify({"success": True})

@app.route('/api/targets/export', methods=['GET'])
@require_auth
def api_export_targets():
    t = load_targets()
    return "\n".join(t), 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.route('/api/targets/result', methods=['POST'])
@require_auth
def api_targets_result():
    """只写入干净活跃用户（非广告/非水军/非销号/非长期未在线）"""
    data = request.json or {}
    items = data.get("targets") or data.get("results") or data.get("usernames") or []
    targets = load_targets()
    existing = {str(x).lower().lstrip("@") for x in targets}
    added = 0
    for item in items:
        if isinstance(item, dict):
            st = (item.get("status") or "").lower()
            if st in ("ad", "spam", "inactive", "deleted", "available", "invalid", "error", "flood", "unavailable"):
                continue
            if item.get("is_ad") or item.get("is_spam"):
                continue
            if item.get("collect") is False and st != "clean":
                continue
            if st not in ("clean", "taken") and not item.get("collect"):
                continue
            username = (item.get("username") or "").strip().lstrip("@")
        else:
            continue
        if not username:
            continue
        key = username.lower()
        if key in existing:
            continue
        targets.append("@" + username)
        existing.add(key)
        added += 1
    save_targets(targets)
    return jsonify({"success": True, "added": added, "total": len(targets)})

@app.route('/api/premium', methods=['GET'])
@require_auth
def api_get_premium():
    premium = load_premium()
    return jsonify({"usernames": premium, "total": len(premium)})

@app.route('/api/premium/clear', methods=['POST'])
@require_auth
def api_clear_premium():
    save_premium([])
    return jsonify({"success": True, "message": "已清空会员列表"})

@app.route('/api/premium/export', methods=['GET'])
@require_auth
def api_export_premium():
    premium = load_premium()
    text = "\n".join(premium)
    return text, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/api/premium/result', methods=['POST'])
@require_auth
def api_premium_result():
    """将 Premium 会员写入会员文档"""
    data = request.json or {}
    items = data.get('premium', data.get('results', data.get('usernames', [])))
    premium = load_premium()
    added = 0
    for item in items:
        if isinstance(item, dict):
            if not item.get('premium'):
                continue
            username = (item.get('username') or '').strip().lstrip('@')
        else:
            username = str(item).strip().lstrip('@')
        if not username:
            continue
        formatted = f"@{username}"
        if formatted not in premium:
            premium.append(formatted)
            added += 1
    save_premium(premium)
    return jsonify({"success": True, "added": added, "total": len(premium)})

# ============ 恢复数据接口 ============
@app.route('/api/restore', methods=['POST'])
@require_auth
def api_restore_data():
    backup_file = "/var/www/tg77777.pw/tg_check_available.txt"
    if not os.path.exists(backup_file):
        backup_file = "/root/HTTP-TG77777.pw/tg_check_available.txt"
    
    if not os.path.exists(backup_file):
        return jsonify({"error": "备份文件不存在"}), 404
    
    with open(backup_file, 'r') as f:
        lines = f.read().strip().split('\n')
    
    available = load_available()
    blacklist = load_blacklist()
    added = 0
    
    for line in lines:
        username = line.strip()
        if not username:
            continue
        if not username.startswith('@'):
            username = f"@{username}"
        is_blacklisted = any(kw.lower() in username.lstrip('@').lower() for kw in blacklist)
        if is_blacklisted:
            continue
        if username not in available:
            available.append(username)
            added += 1
    
    save_available(available)
    return jsonify({"success": True, "added": added, "total": len(available)})

# ============ 关键词黑名单接口 ============
@app.route('/api/blacklist', methods=['GET'])
@require_auth
def api_get_blacklist():
    keywords = load_blacklist()
    return jsonify({"keywords": keywords})

@app.route('/api/blacklist/add', methods=['POST'])
@require_auth
def api_add_blacklist():
    data = request.json
    keywords = data.get('keywords', [])
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split('\n') if k.strip()]
    
    current = load_blacklist()
    added = 0
    for kw in keywords:
        kw = kw.strip()
        if kw and kw not in current:
            current.append(kw)
            added += 1
    
    save_blacklist(current)
    
    # 自动从可用列表中删除匹配的用户名
    available = load_available()
    removed = 0
    new_available = []
    for username in available:
        name = username.lstrip('@').lower()
        if any(kw.lower() in name for kw in current):
            removed += 1
        else:
            new_available.append(username)
    save_available(new_available)
    
    return jsonify({"success": True, "added": added, "removed": removed, "total_keywords": len(current)})

@app.route('/api/blacklist/remove', methods=['POST'])
@require_auth
def api_remove_blacklist():
    data = request.json
    keyword = data.get('keyword', '').strip()
    current = load_blacklist()
    current = [k for k in current if k != keyword]
    save_blacklist(current)
    return jsonify({"success": True, "total": len(current)})

@app.route('/api/blacklist/clear', methods=['POST'])
@require_auth
def api_clear_blacklist():
    save_blacklist([])
    return jsonify({"success": True})

# ============ 统计接口 ============

# ============ 状态接口（兼容前端） ============

@app.route('/api/bots/work_status', methods=['GET'])
@require_auth
def api_bots_work_status():
    import time
    config = load_config()
    bots = config.get("bots") or []
    cd = load_cooldown()
    daily = load_daily_stats()
    rows = []
    work = 0
    cool = 0
    capped = 0
    for b in bots:
        key = str(b.get("id") or b.get("phone") or "")
        cooling, left = is_bot_cooling(key)
        remain, used, limit = bot_daily_left(key)
        if cooling:
            st = "cooldown"
            cool += 1
        elif remain <= 0:
            st = "daily_capped"
            capped += 1
        else:
            st = "work"
            work += 1
        rows.append({
            "id": key,
            "phone": b.get("phone"),
            "name": b.get("name"),
            "status": st,
            "cooldown_left": left if cooling else 0,
            "daily_used": used,
            "daily_left": remain,
            "daily_limit": limit,
        })
    return jsonify({
        "bots": rows,
        "summary": {"work": work, "cooldown": cool, "daily_capped": capped, "total": len(rows)},
        "max_batch": MAX_BATCH_SIZE,
        "default_daily_limit": DEFAULT_DAILY_LIMIT,
    })


@app.route('/api/status', methods=['GET'])
@require_auth
def api_status():
    """返回水军列表和状态（前端仪表盘使用）"""
    config = load_config()
    bots = config.get('bots', [])
    safe_bots = []
    for bot in bots:
        safe_bots.append({
            "id": bot.get("id", ""),
            "name": bot.get("name", ""),
            "username": bot.get("username", ""),
            "phone": bot.get("phone", ""),
            "first_name": bot.get("first_name", ""),
            "status": bot.get("status", "ready"),
            "type": bot.get("type", "userbot"),
            "added_time": bot.get("added_time", "")
        })
    return jsonify({"bots": safe_bots, "total": len(safe_bots)})

# ============ 统计接口（兼容前端） ============
@app.route('/api/stats', methods=['GET'])
@require_auth
def api_stats():
    config = load_config()
    available = load_available()
    premium = load_premium()
    blacklist = load_blacklist()
    bots = config.get('bots', [])
    stats = config.get('stats', {})
    return jsonify({
        "bots": len(bots),
        "available": len(available),
        "premium": len(premium),
        "blacklist": len(blacklist),
        "stats": {
            "today_sent": stats.get("today_sent", 0),
            "total_sent": stats.get("total_sent", 0),
            "total_success": stats.get("total_success", 0),
            "total_failed": stats.get("total_failed", 0),
            "today_success": stats.get("today_success", 0)
        }
    })

# ============ 健康检查 ============
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# ============ 启动 ============
if __name__ == '__main__':
    ensure_loop_running()
    print(f"[{datetime.now()}] TG用户名检测工具 Agent 启动在端口 8899")
    print(f"[{datetime.now()}] Asyncio事件循环已在独立线程中运行")
    app.run(host='0.0.0.0', port=8899, debug=False)
