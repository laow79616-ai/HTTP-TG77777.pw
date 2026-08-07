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
LOGIN_USER = "admin"
LOGIN_PASS = "Ab123456987"
CONFIG_FILE = "/root/bot_agent/config.json"
BLACKLIST_FILE = "/root/bot_agent/blacklist.json"
AVAILABLE_FILE = "/root/bot_agent/available_usernames.json"
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

# ============ 认证中间件 ============
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
    data = request.json
    bot_id = data.get('id', '')
    config = load_config()
    config['bots'] = [b for b in config.get('bots', []) if b.get('id') != bot_id]
    save_config(config)
    return jsonify({"success": True, "message": "水军已删除"})

# ============ Telethon 手机号登录（异步） ============
pending_clients = {}

def get_api_config(api_id=None, api_hash=None):
    """获取 API 配置。优先使用传入的自定义 api_id/api_hash，否则随机从系统配置中选取。"""
    if api_id and api_hash:
        try:
            return {"api_id": int(api_id), "api_hash": str(api_hash).strip()}
        except (ValueError, TypeError):
            pass
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
    
    # 支持前端传入自定义 API 配置
    api_id = data.get('api_id') or data.get('apiId')
    api_hash = data.get('api_hash') or data.get('apiHash')
    
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

# ============ 批量检测接口 ============
@app.route('/api/check', methods=['POST'])
@require_auth
def api_check_usernames():
    data = request.json
    usernames = data.get('usernames', [])
    if not usernames:
        return jsonify({"error": "请提供用户名列表"}), 400
    
    results = []
    available = load_available()
    blacklist = load_blacklist()
    
    for username in usernames:
        username = username.strip().lstrip('@')
        if not username:
            continue
        is_blacklisted = any(kw.lower() in username.lower() for kw in blacklist)
        if is_blacklisted:
            continue
        if f"@{username}" not in available:
            results.append({"username": username, "status": "pending"})
    
    return jsonify({"results": results, "total": len(results)})

@app.route('/api/check/result', methods=['POST'])
@require_auth
def api_check_result():
    data = request.json
    usernames = data.get('available', [])
    
    available = load_available()
    blacklist = load_blacklist()
    added = 0
    
    for username in usernames:
        username = username.strip().lstrip('@')
        if not username:
            continue
        formatted = f"@{username}"
        is_blacklisted = any(kw.lower() in username.lower() for kw in blacklist)
        if is_blacklisted:
            continue
        if formatted not in available:
            available.append(formatted)
            added += 1
    
    save_available(available)
    return jsonify({"success": True, "added": added, "total": len(available)})

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
    blacklist = load_blacklist()
    bots = config.get('bots', [])
    stats = config.get('stats', {})
    return jsonify({
        "bots": len(bots),
        "available": len(available),
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
