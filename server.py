import sqlite3
import uuid
import secrets
import datetime
import json
import os
import base64
import requests
from flask import Flask, request, jsonify, redirect

app = Flask(__name__)
DB_FILE = "keys.db"
ADMIN_SECRET = "CHANGE_THIS_TO_A_SECRET_PASSWORD"  # Used by the bot to generate keys
CONFIG_FILE = "bot_config.json"
DISCORD_API_BASE = "https://discord.com/api"

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def get_discord_oauth_config():
    cfg = load_config()
    client_id = os.environ.get("DISCORD_CLIENT_ID") or cfg.get("discord_client_id")
    client_secret = os.environ.get("DISCORD_CLIENT_SECRET") or cfg.get("discord_client_secret")
    redirect_uri = os.environ.get("DISCORD_REDIRECT_URI") or cfg.get("discord_redirect_uri")
    return client_id, client_secret, redirect_uri

def send_discord_webhook(title, description, color, fields=None):
    config = load_config()
    webhook_url = config.get('webhook_url')
    if not webhook_url:
        return
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "footer": {"text": "Pillow Player Runtime Logs"}
    }
    
    if fields:
        embed["fields"] = fields
        
    payload = {"embeds": [embed]}
    
    try:
        requests.post(webhook_url, json=payload, timeout=2)
    except:
        pass # Don't block auth if logging fails

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS licenses
                 (key_code TEXT PRIMARY KEY, 
                  status TEXT, 
                  hwid TEXT, 
                  device_name TEXT, 
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Check for new columns and add them if missing (Migration)
    c.execute("PRAGMA table_info(licenses)")
    columns = [info[1] for info in c.fetchall()]
    
    if 'duration_hours' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN duration_hours INTEGER DEFAULT 0")
    if 'expires_at' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN expires_at TIMESTAMP")
    if 'note' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN note TEXT")
    if 'redeemed_at' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN redeemed_at TIMESTAMP")
    if 'discord_id' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN discord_id TEXT")
    if 'run_count' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN run_count INTEGER DEFAULT 0")
    if 'ip_address' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN ip_address TEXT")
    if 'last_seen' not in columns:
        c.execute("ALTER TABLE licenses ADD COLUMN last_seen TIMESTAMP")
        
    # Create Blacklist Table
    c.execute('''CREATE TABLE IF NOT EXISTS blacklist
                 (hwid TEXT PRIMARY KEY, 
                  reason TEXT, 
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Create Credits Table (PCredit)
    c.execute('''CREATE TABLE IF NOT EXISTS user_credits
                 (discord_id TEXT PRIMARY KEY, 
                  balance INTEGER DEFAULT 0,
                  last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        
    conn.commit()
    conn.close()

@app.route('/')
def home():
    return "I am alive!", 200

@app.route('/verify', methods=['POST'])
def verify_key():
    data = request.json
    key = data.get('key')
    hwid = data.get('hwid')
    device_name = data.get('device_name')

    if not key or not hwid:
        return jsonify({"valid": False, "message": "Missing key or HWID"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Check Blacklist
    c.execute("SELECT * FROM blacklist WHERE hwid=?", (hwid,))
    if c.fetchone():
        conn.close()
        return jsonify({"valid": False, "message": "HWID Blacklisted"}), 403

    c.execute("SELECT status, hwid, duration_hours, expires_at FROM licenses WHERE key_code=?", (key,))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"valid": False, "message": "Invalid Key"}), 403

    status, stored_hwid, duration, expires_at = row

    # Retrieve Discord User Info if available
    c.execute("SELECT discord_id FROM licenses WHERE key_code=?", (key,))
    discord_id = c.fetchone()[0]
    
    # [STRICT] Enforce Key Claiming
    if not discord_id:
        conn.close()
        return jsonify({"valid": False, "message": "Key must be claimed first!"}), 403
    
    # Count user's total active keys
    total_keys = 0
    if discord_id:
        c.execute("SELECT COUNT(*) FROM licenses WHERE discord_id=?", (discord_id,))
        total_keys = c.fetchone()[0]

    # Check expiration if active
    if expires_at:
        expiry_dt = datetime.datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S.%f') if '.' in expires_at else datetime.datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S')
        if datetime.datetime.now() > expiry_dt:
            conn.close()
            return jsonify({"valid": False, "message": "Key Expired"}), 403

    if status == "unused":
        # First activation
        new_expires_at = None
        if duration and duration > 0:
            new_expires_at = datetime.datetime.now() + datetime.timedelta(hours=duration)
        
        redeemed_time = datetime.datetime.now()
        client_ip = request.remote_addr
        c.execute("UPDATE licenses SET status='used', hwid=?, device_name=?, expires_at=?, redeemed_at=?, last_seen=?, ip_address=? WHERE key_code=?", 
                  (hwid, device_name, new_expires_at, redeemed_time, redeemed_time, client_ip, key))
        conn.commit()
        conn.close()
        
        # LOG ACTIVATION
        user_str = f"<@{discord_id}>" if discord_id else "Unknown User"
        fields = [
            {"name": "👤 User", "value": user_str, "inline": True},
            {"name": "🔑 Key", "value": f"`{key}`", "inline": True},
            {"name": "💻 Device", "value": f"{device_name}", "inline": True},
            {"name": "🔢 Total Accounts", "value": f"{total_keys}", "inline": True}
        ]
        send_discord_webhook("🟢 New Activation", f"Key activated by {user_str}", 65280, fields) # Green
        
        return jsonify({"valid": True, "message": "Key Activated Successfully!", "discord_id": discord_id})

    elif status == "used":
        if stored_hwid == hwid:
            # Increment run count and update last seen
            client_ip = request.remote_addr
            last_seen = datetime.datetime.now()
            c.execute("UPDATE licenses SET run_count = run_count + 1, last_seen=?, ip_address=? WHERE key_code=?", (last_seen, client_ip, key))
            conn.commit()
            conn.close()
            
            # LOG USAGE (SESSION START)
            user_str = f"<@{discord_id}>" if discord_id else "Unknown User"
            fields = [
                {"name": "👤 User", "value": user_str, "inline": True},
                {"name": "🔑 Key", "value": f"`{key}`", "inline": True},
                {"name": "💻 Device", "value": f"{device_name}", "inline": True},
                {"name": "🔢 Total Accounts", "value": f"{total_keys}", "inline": True}
            ]
            send_discord_webhook("🔵 Session Started", f"User {user_str} launched the software.", 3447003, fields) # Blue
        
            return jsonify({"valid": True, "message": "Welcome back!", "discord_id": discord_id})
        else:
            conn.close()
            
            # LOG FAILED ATTEMPT (HWID Mismatch)
            user_str = f"<@{discord_id}>" if discord_id else "Unknown User"
            fields = [
                {"name": "👤 User", "value": user_str, "inline": True},
                {"name": "🔑 Key", "value": f"`{key}`", "inline": True},
                {"name": "💻 Expected HWID", "value": f"`{stored_hwid}`", "inline": True},
                {"name": "⚠️ Attempted HWID", "value": f"`{hwid}`", "inline": True}
            ]
            send_discord_webhook("⚠️ Suspicious Login Attempt", f"HWID Mismatch for {user_str}", 16711680, fields) # Red

            return jsonify({"valid": False, "message": "Key already used on another device!"}), 403

    conn.close()
    return jsonify({"valid": False, "message": "Unknown Error"}), 500

@app.route('/generate', methods=['POST'])
def generate_key():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    amount = data.get('amount', 1)
    duration = data.get('duration_hours', 0)
    note = data.get('note', None)
    discord_id = data.get('discord_id', None)
    
    if not isinstance(amount, int) or amount < 1:
        amount = 1
    
    generated_keys = []
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        for _ in range(amount):
            # Generate format: PILLOW-PLAYER-XXXXXX
            random_part = secrets.token_hex(3).upper() # 6 chars
            key = f"PILLOW-PLAYER-{random_part}"
            
            try:
                c.execute("INSERT INTO licenses (key_code, status, hwid, device_name, duration_hours, note, discord_id) VALUES (?, 'unused', NULL, NULL, ?, ?, ?)", (key, duration, note, discord_id))
                generated_keys.append(key)
            except sqlite3.IntegrityError:
                # Retry once if collision (rare)
                random_part = secrets.token_hex(3).upper()
                key = f"PILLOW-PLAYER-{random_part}"
                try:
                    c.execute("INSERT INTO licenses (key_code, status, hwid, device_name, duration_hours, note, discord_id) VALUES (?, 'unused', NULL, NULL, ?, ?, ?)", (key, duration, note, discord_id))
                    generated_keys.append(key)
                except:
                    continue # Skip if fails twice

        conn.commit()
        conn.close()
        return jsonify({"keys": generated_keys, "count": len(generated_keys)})
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/link_discord', methods=['POST'])
def link_discord():
    data = request.json
    # No admin check needed here as users verify themselves via the bot interaction, 
    # BUT for security, the bot should be the only one calling this with a secret.
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    key = data.get('key')
    discord_id = data.get('discord_id')
    
    if not key or not discord_id:
        return jsonify({"error": "Missing key or discord_id"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Verify key exists
    c.execute("SELECT discord_id FROM licenses WHERE key_code=?", (key,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Invalid Key"}), 404
        
    current_owner = row[0]
    
    # Check if user already has a key
    c.execute("SELECT key_code FROM licenses WHERE discord_id=?", (discord_id,))
    user_keys = c.fetchall()
    
    if user_keys:
        # User has at least one key.
        # Check if it's the SAME key they are trying to claim
        has_this_key = False
        for k_tuple in user_keys:
            if k_tuple[0] == key:
                has_this_key = True
                break
        
        if has_this_key:
             print(f"DEBUG: Key {key} already linked to {discord_id} - returning success")
             conn.close()
             return jsonify({"success": True, "message": "Key is already linked to your account."})
        else:
             # User has a different key. Deny.
             print(f"DEBUG: User {discord_id} tried to claim {key} but already has another key")
             conn.close()
             return jsonify({"error": "You can only claim ONE key per account."}), 403

    # Check if key is claimed by SOMEONE ELSE
    if current_owner and current_owner != discord_id:
        conn.close()
        return jsonify({"error": "This key is already claimed by another user."}), 403

    # Link the key
    print(f"DEBUG: Linking key {key} to {discord_id}")
    c.execute("UPDATE licenses SET discord_id=? WHERE key_code=?", (discord_id, key))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "message": "Discord Account Linked"})

@app.route('/get_user_keys', methods=['POST'])
def get_user_keys():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
        
    discord_id = data.get('discord_id')
    if not discord_id:
        return jsonify({"error": "Missing discord_id"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM licenses WHERE discord_id=?", (discord_id,))
    rows = c.fetchall()
    
    keys = []
    for row in rows:
        k = dict(row)
        # Check if HWID is blacklisted
        is_banned = False
        if k['hwid']:
            c_bl = conn.cursor()
            c_bl.execute("SELECT 1 FROM blacklist WHERE hwid=?", (k['hwid'],))
            if c_bl.fetchone():
                is_banned = True
        
        k['is_banned'] = is_banned
        keys.append(k)
        
    conn.close()
    
    return jsonify({"keys": keys})

@app.route('/auth/discord/start')
def discord_auth_start():
    client_id, client_secret, redirect_uri = get_discord_oauth_config()
    if not client_id or not redirect_uri:
        return "Discord OAuth not configured", 500
    state_raw = secrets.token_hex(16)
    state = base64.urlsafe_b64encode(state_raw.encode()).decode().rstrip("=")
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "identify",
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "consent"
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    url = f"https://discord.com/api/oauth2/authorize?{query}"
    return redirect(url)

@app.route('/auth/discord/callback')
def discord_auth_callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400
    client_id, client_secret, redirect_uri = get_discord_oauth_config()
    if not client_id or not client_secret or not redirect_uri:
        return "Discord OAuth not configured", 500
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    token_resp = requests.post(f"{DISCORD_API_BASE}/oauth2/token", data=data, headers=headers)
    if token_resp.status_code != 200:
        return "Failed to fetch token", 400
    token_json = token_resp.json()
    access_token = token_json.get("access_token")
    if not access_token:
        return "No access token", 400
    user_headers = {
        "Authorization": f"Bearer {access_token}"
    }
    user_resp = requests.get(f"{DISCORD_API_BASE}/users/@me", headers=user_headers)
    if user_resp.status_code != 200:
        return "Failed to fetch user", 400
    user_info = user_resp.json()
    discord_id = str(user_info.get("id"))
    if not discord_id:
        return "Missing user id", 400
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM licenses WHERE discord_id=?", (discord_id,))
    rows = c.fetchall()
    keys = [dict(row) for row in rows]
    conn.close()
    result = {
        "discord_id": discord_id,
        "username": user_info.get("username"),
        "global_name": user_info.get("global_name"),
        "keys": keys
    }
    return jsonify(result)

@app.route('/stats', methods=['POST'])
def get_stats():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Fetch all data for Python-side processing (easier for date math)
    c.execute("SELECT status, duration_hours, expires_at, created_at, key_code, device_name FROM licenses ORDER BY created_at DESC")
    all_rows = c.fetchall()
    
    total = len(all_rows)
    used = 0
    unused = 0
    active = 0
    expired = 0
    lifetime = 0
    limited = 0
    created_24h = 0
    
    now = datetime.datetime.now()
    one_day_ago = now - datetime.timedelta(hours=24)
    
    # Recent keys for list
    recent_keys = []
    
    for i, row in enumerate(all_rows):
        # Convert row to dict for recent keys (first 10)
        if i < 10:
            recent_keys.append(dict(row))
            
        r_status = row['status']
        r_duration = row['duration_hours']
        r_expires_at = row['expires_at']
        r_created_at = row['created_at']
        
        # Status Counts
        if r_status == 'used':
            used += 1
            # Check Active vs Expired
            is_active_key = True
            if r_expires_at:
                try:
                    # Handle potential fractional seconds
                    if '.' in r_expires_at:
                        exp_dt = datetime.datetime.strptime(r_expires_at, '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        exp_dt = datetime.datetime.strptime(r_expires_at, '%Y-%m-%d %H:%M:%S')
                        
                    if exp_dt < now:
                        is_active_key = False
                except:
                    is_active_key = False # Error parsing = assume issue/expired
            
            if is_active_key:
                active += 1
            else:
                expired += 1
        else:
            unused += 1
            
        # Duration Type
        if r_duration == 0:
            lifetime += 1
        else:
            limited += 1
            
        # Created Recently
        try:
            # created_at is usually YYYY-MM-DD HH:MM:SS
            creat_dt = datetime.datetime.strptime(r_created_at, '%Y-%m-%d %H:%M:%S')
            if creat_dt > one_day_ago:
                created_24h += 1
        except:
            pass

    # Get Recently Redeemed (Last 5)
    c.execute("SELECT key_code, device_name, redeemed_at FROM licenses WHERE status='used' AND redeemed_at IS NOT NULL ORDER BY redeemed_at DESC LIMIT 5")
    recently_redeemed = [dict(row) for row in c.fetchall()]

    conn.close()
    
    return jsonify({
        "total": total,
        "used": used,
        "unused": unused,
        "active": active,
        "expired": expired,
        "lifetime": lifetime,
        "limited": limited,
        "created_24h": created_24h,
        "recent_keys": recent_keys,
        "recently_redeemed": recently_redeemed
    })

@app.route('/reset', methods=['POST'])
def reset_key():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    key = data.get('key')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE licenses SET status='unused', hwid=NULL, device_name=NULL WHERE key_code=?", (key,))
    conn.commit()
    conn.close()
    return jsonify({"message": f"Key {key} reset successfully"})

@app.route('/delete', methods=['POST'])
def delete_key():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    key = data.get('key')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM licenses WHERE key_code=?", (key,))
    
    if c.rowcount == 0:
        conn.close()
        return jsonify({"error": "Key not found"}), 404
        
    conn.commit()
    conn.close()
    return jsonify({"message": f"Key {key} deleted successfully"})

@app.route('/delete_batch', methods=['POST'])
def delete_batch_keys():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    keys = data.get('keys', [])
    if not keys:
        return jsonify({"message": "No keys provided"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        # Use placeholders for secure list handling
        placeholders = ','.join('?' for _ in keys)
        c.execute(f"DELETE FROM licenses WHERE key_code IN ({placeholders})", keys)
        deleted_count = c.rowcount
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
        
    conn.close()
    return jsonify({"message": f"Successfully deleted {deleted_count} keys."})

@app.route('/ban_key', methods=['POST'])
def ban_key():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    keys = data.get('keys', [])
    reason = data.get('reason', 'Banned by Admin')
    
    if not keys:
        return jsonify({"message": "No keys provided"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        placeholders = ','.join('?' for _ in keys)
        c.execute(f"UPDATE licenses SET status='banned', note=note || ' [BANNED: ' || ? || ']' WHERE key_code IN ({placeholders})", [reason] + keys)
        count = c.rowcount
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
        
    conn.close()
    return jsonify({"message": f"Successfully banned {count} keys."})

@app.route('/recover_key', methods=['POST'])
def recover_key():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    keys = data.get('keys', [])
    if not keys:
        return jsonify({"message": "No keys provided"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        placeholders = ','.join('?' for _ in keys)
        # Restore status based on HWID presence
        c.execute(f"UPDATE licenses SET status = CASE WHEN hwid IS NOT NULL THEN 'used' ELSE 'unused' END, note = note || ' [RECOVERED]' WHERE key_code IN ({placeholders})", keys)
        count = c.rowcount
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
        
    conn.close()
    return jsonify({"message": f"Successfully recovered {count} keys."})

@app.route('/reset_batch', methods=['POST'])
def reset_batch_keys():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    keys = data.get('keys', [])
    if not keys:
        return jsonify({"message": "No keys provided"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        placeholders = ','.join('?' for _ in keys)
        c.execute(f"UPDATE licenses SET status='unused', hwid=NULL, device_name=NULL WHERE key_code IN ({placeholders})", keys)
        reset_count = c.rowcount
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

    conn.close()
    return jsonify({"message": f"Successfully reset {reset_count} keys."})

@app.route('/info', methods=['POST'])
def key_info():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    key = data.get('key')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM licenses WHERE key_code=?", (key,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return jsonify(dict(row))
    else:
        return jsonify({"error": "Key not found"}), 404

@app.route('/list', methods=['POST'])
def list_keys():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Get all keys ordered by creation
    c.execute("SELECT * FROM licenses ORDER BY created_at DESC")
    rows = c.fetchall()
    keys = [dict(row) for row in rows]
    conn.close()
    
    return jsonify({"keys": keys})

@app.route('/blacklist/manage', methods=['POST'])
def manage_blacklist():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
        
    action = data.get('action') # 'add', 'remove', 'list'
    hwid = data.get('hwid')
    reason = data.get('reason', 'No reason provided')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if action == 'add':
        if not hwid:
            conn.close()
            return jsonify({"error": "Missing HWID"}), 400
        try:
            c.execute("INSERT INTO blacklist (hwid, reason) VALUES (?, ?)", (hwid, reason))
            conn.commit()
            msg = f"HWID {hwid} added to blacklist."
        except sqlite3.IntegrityError:
            msg = "HWID already blacklisted."
            
    elif action == 'remove':
        if not hwid:
            conn.close()
            return jsonify({"error": "Missing HWID"}), 400
        c.execute("DELETE FROM blacklist WHERE hwid=?", (hwid,))
        conn.commit()
        msg = f"HWID {hwid} removed from blacklist."
        
    elif action == 'list':
        conn.row_factory = sqlite3.Row
        # Re-create cursor with row factory
        c = conn.cursor()
        c.execute("SELECT * FROM blacklist")
        rows = c.fetchall()
        result = [dict(row) for row in rows]
        conn.close()
        return jsonify({"blacklist": result})
        
    else:
        conn.close()
        return jsonify({"error": "Invalid action"}), 400
        
    conn.close()
    return jsonify({"success": True, "message": msg})

@app.route('/pcredit/manage', methods=['POST'])
def manage_pcredit():
    data = request.json
    if data.get('admin_secret') != ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    action = data.get('action') # 'add', 'remove', 'set'
    discord_id = data.get('discord_id')
    amount = data.get('amount')

    if not discord_id:
        return jsonify({"error": "Missing discord_id"}), 400
    
    if action in ['add', 'remove', 'set'] and (amount is None or not isinstance(amount, int)):
        return jsonify({"error": "Invalid or missing amount"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Ensure user exists in table
    c.execute("INSERT OR IGNORE INTO user_credits (discord_id, balance) VALUES (?, 0)", (discord_id,))

    new_balance = 0
    msg = ""

    if action == 'add':
        c.execute("UPDATE user_credits SET balance = balance + ?, last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
        msg = f"Added {amount} credits to {discord_id}"
    elif action == 'remove':
        c.execute("UPDATE user_credits SET balance = MAX(0, balance - ?), last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
        msg = f"Removed {amount} credits from {discord_id}"
    elif action == 'set':
        c.execute("UPDATE user_credits SET balance = ?, last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
        msg = f"Set credits for {discord_id} to {amount}"
    else:
        conn.close()
        return jsonify({"error": "Invalid action"}), 400

    conn.commit()
    
    # Get new balance
    c.execute("SELECT balance FROM user_credits WHERE discord_id=?", (discord_id,))
    new_balance = c.fetchone()[0]
    
    conn.close()
    return jsonify({"success": True, "message": msg, "new_balance": new_balance})

@app.route('/pcredit/balance', methods=['POST'])
def get_pcredit_balance():
    data = request.json
    # No admin secret check needed if we want users to check their own balance via bot
    # But bot will provide the secret anyway
    if data.get('admin_secret') != ADMIN_SECRET:
         return jsonify({"error": "Unauthorized"}), 401

    discord_id = data.get('discord_id')
    if not discord_id:
        return jsonify({"error": "Missing discord_id"}), 400

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance FROM user_credits WHERE discord_id=?", (discord_id,))
    row = c.fetchone()
    conn.close()

    balance = row[0] if row else 0
    return jsonify({"discord_id": discord_id, "balance": balance})

if __name__ == '__main__':
    init_db()
    print("==========================================")
    print("  Pillow Auth Server - ONE KEY LIMIT: ON  ")
    print("==========================================")
    print("Server running on port 5000...")
    app.run(host='0.0.0.0', port=5000)
