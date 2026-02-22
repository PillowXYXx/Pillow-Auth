import discord
import asyncio
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select, Button
import requests
import json
import io
import datetime
import time
import os
import sqlite3
import uuid
import secrets
from user_utils import resolve_users_map

# CONFIGURATION
# Token must be provided via environment variable DISCORD_TOKEN (no token in code)
BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set; bot cannot start.")
# Use Public URL for Cloud, Localhost for testing
# If we find a "RENDER_EXTERNAL_URL" environment variable, we use that.
if os.environ.get("RENDER"):
    API_URL = "https://pillow-auth.onrender.com"
else:
    API_URL = "http://127.0.0.1:5000"

# SERVER URL for new commands
SERVER_URL = API_URL

ADMIN_SECRET = "CHANGE_THIS_TO_A_SECRET_PASSWORD" # Must match server.py
CONFIG_FILE = "bot_config.json"
DB_FILE = os.path.join(os.path.dirname(__file__), "keys.db")
ticket_lock = set()

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# --- OFFLINE DB FALLBACK HELPERS ---
def _db_query_fallback_sync(endpoint, payload):
    """
    Attempts to call the API endpoint. 
    If it fails (ConnectionError), falls back to direct SQLite access.
    Returns a tuple: (status_code, json_response)
    """
    url = f"{API_URL}{endpoint}"
    try:
        response = requests.post(url, json=payload, timeout=2)
        return response.status_code, response.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        print(f"⚠️ API unreachable ({endpoint}). Switching to Offline DB Mode.")
        return execute_offline_db(endpoint, payload)
    except Exception as e:
        return 500, {"error": f"Request Failed: {e}"}

async def db_query_fallback(endpoint, payload):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_query_fallback_sync, endpoint, payload)

def execute_offline_db(endpoint, payload):
    """Executes the equivalent SQL logic for supported endpoints."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        response = {}
        status = 200

        # --- KEY GENERATION ---
        if endpoint == "/generate":
            amount = payload.get('amount', 1)
            duration_hours = payload.get('duration_hours', 0)
            note = payload.get('note')
            discord_id = payload.get('discord_id') # Optional, for direct grant
            
            new_keys = []
            for _ in range(amount):
                key = "KEY-" + secrets.token_hex(16).upper()
                created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                expires_at = None
                
                # We store duration, but expires_at is calculated on redemption usually. 
                # However, the server logic might be different. 
                # Checking server.py (from memory/previous context), expires_at is NULL until redemption if duration > 0.
                # If it's a fixed date expiration, it would be set here. 
                # But 'duration_hours' implies it starts on use.
                
                c.execute("INSERT INTO licenses (key_code, status, duration_hours, created_at, note, discord_id) VALUES (?, ?, ?, ?, ?, ?)",
                          (key, 'unused', duration_hours, created_at, note, discord_id))
                new_keys.append(key)
            
            conn.commit()
            response = {"keys": new_keys, "count": len(new_keys)}

        # --- LIST KEYS ---
        elif endpoint == "/list":
            c.execute("SELECT * FROM licenses ORDER BY created_at DESC")
            rows = c.fetchall()
            keys = []
            for row in rows:
                k = dict(row)
                # Check ban status from blacklist
                is_banned = False
                if k['hwid']:
                    c_bl = conn.cursor()
                    c_bl.execute("SELECT 1 FROM blacklist WHERE hwid=?", (k['hwid'],))
                    if c_bl.fetchone(): is_banned = True
                k['is_banned'] = is_banned
                keys.append(k)
            response = {"keys": keys}

        # --- GET USER KEYS ---
        elif endpoint == "/get_user_keys":
            discord_id = payload.get('discord_id')
            c.execute("SELECT * FROM licenses WHERE discord_id=?", (discord_id,))
            rows = c.fetchall()
            keys = []
            for row in rows:
                k = dict(row)
                is_banned = False
                if k['hwid']:
                    c_bl = conn.cursor()
                    c_bl.execute("SELECT 1 FROM blacklist WHERE hwid=?", (k['hwid'],))
                    if c_bl.fetchone(): is_banned = True
                k['is_banned'] = is_banned
                keys.append(k)
            response = {"keys": keys}

        # --- PCREDIT BALANCE ---
        elif endpoint == "/pcredit/balance":
            discord_id = payload.get('discord_id')
            c.execute("SELECT balance FROM user_credits WHERE discord_id=?", (discord_id,))
            row = c.fetchone()
            balance = row[0] if row else 0
            response = {"discord_id": discord_id, "balance": balance}

        # --- PCREDIT MANAGE ---
        elif endpoint == "/pcredit/manage":
            action = payload.get('action')
            discord_id = payload.get('discord_id')
            amount = payload.get('amount')
            
            c.execute("INSERT OR IGNORE INTO user_credits (discord_id, balance) VALUES (?, 0)", (discord_id,))
            msg = ""
            new_balance = 0
            
            if action == 'add':
                c.execute("UPDATE user_credits SET balance = balance + ?, last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
                msg = f"Added {amount} credits (Offline Mode)"
            elif action == 'remove':
                c.execute("UPDATE user_credits SET balance = MAX(0, balance - ?), last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
                msg = f"Removed {amount} credits (Offline Mode)"
            elif action == 'set':
                c.execute("UPDATE user_credits SET balance = ?, last_updated = CURRENT_TIMESTAMP WHERE discord_id=?", (amount, discord_id))
                msg = f"Set credits (Offline Mode)"
            
            conn.commit()
            c.execute("SELECT balance FROM user_credits WHERE discord_id=?", (discord_id,))
            new_balance = c.fetchone()[0]
            response = {"success": True, "message": msg, "new_balance": new_balance}
            
        # --- LINK DISCORD (CLAIM) ---
        elif endpoint == "/link_discord":
            key = payload.get('key')
            discord_id = payload.get('discord_id')
            
            c.execute("SELECT discord_id FROM licenses WHERE key_code=?", (key,))
            row = c.fetchone()
            if not row:
                status = 404
                response = {"error": "Invalid Key"}
            else:
                current_owner = row[0]
                # Check existing key
                c.execute("SELECT key_code FROM licenses WHERE discord_id=?", (discord_id,))
                user_keys = c.fetchall()
                has_other = False
                for uk in user_keys:
                    if uk[0] != key: has_other = True
                
                if has_other:
                    status = 403
                    response = {"error": "You can only claim ONE key per account."}
                elif current_owner and current_owner != discord_id:
                    status = 403
                    response = {"error": "Key already claimed."}
                else:
                    c.execute("UPDATE licenses SET discord_id=? WHERE key_code=?", (discord_id, key))
                    conn.commit()
                    response = {"success": True, "message": "Key Linked (Offline Mode)"}

        # --- BLACKLIST MANAGE ---
        elif endpoint == "/blacklist/manage":
            action = payload.get('action')
            hwid = payload.get('hwid')
            reason = payload.get('reason', 'No reason provided')
            
            if action == 'add':
                if not hwid:
                    status = 400
                    response = {"error": "Missing HWID"}
                else:
                    c.execute("INSERT OR IGNORE INTO blacklist (hwid, reason) VALUES (?, ?)", (hwid, reason))
                    conn.commit()
                    response = {"success": True, "message": f"HWID {hwid} blacklisted."}
            elif action == 'remove':
                c.execute("DELETE FROM blacklist WHERE hwid=?", (hwid,))
                conn.commit()
                response = {"success": True, "message": f"HWID {hwid} removed from blacklist."}
            elif action == 'list':
                c.execute("SELECT * FROM blacklist")
                rows = c.fetchall()
                bl = [dict(r) for r in rows]
                response = {"blacklist": bl}

        # --- BAN KEY ---
        elif endpoint == "/ban_key":
            keys_to_ban = payload.get('keys', [])
            reason = payload.get('reason', 'Banned')
            for k in keys_to_ban:
                c.execute("UPDATE licenses SET status='banned', note=note || ? WHERE key_code=?", (f" [BANNED: {reason}]", k))
            conn.commit()
            response = {"success": True, "message": f"Banned {len(keys_to_ban)} keys."}

        # --- STATS ---
        elif endpoint == "/stats":
            c.execute("SELECT COUNT(*) FROM licenses")
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM licenses WHERE status='unused'")
            unused = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM licenses WHERE status='active'")
            active = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM licenses WHERE status='expired'")
            expired = c.fetchone()[0]
            
            # Simple stats for offline mode
            response = {
                "total": total,
                "unused": unused,
                "used": total - unused,
                "active": active,
                "expired": expired,
                "lifetime": 0, # Simplified
                "limited": 0, # Simplified
                "created_24h": 0, # Simplified
                "recently_redeemed": [],
                "recent_keys": []
            }

        # --- RESET BATCH ---
        elif endpoint == "/reset_batch":
            keys_to_reset = payload.get('keys', [])
            for k in keys_to_reset:
                c.execute("UPDATE licenses SET hwid=NULL, status='unused', device_name=NULL, ip_address=NULL, last_seen=NULL WHERE key_code=?", (k,))
            conn.commit()
            response = {"success": True, "message": f"Reset {len(keys_to_reset)} keys."}

        # --- RECOVER KEY ---
        elif endpoint == "/recover_key":
            keys_to_recover = payload.get('keys', [])
            for k in keys_to_recover:
                # Set status to unused. Optionally we could try to clean up the note, but that's complex in SQL.
                c.execute("UPDATE licenses SET status='unused' WHERE key_code=?", (k,))
            conn.commit()
            response = {"success": True, "message": f"Recovered {len(keys_to_recover)} keys."}

        # --- DELETE BATCH ---
        elif endpoint == "/delete_batch":
            keys_to_delete = payload.get('keys', [])
            for k in keys_to_delete:
                c.execute("DELETE FROM licenses WHERE key_code=?", (k,))
            conn.commit()
            response = {"success": True, "message": f"Deleted {len(keys_to_delete)} keys."}

        else:
            status = 501
            response = {"error": f"Endpoint {endpoint} not supported in Offline Mode"}

        conn.close()
        return status, response
    except Exception as e:
        return 500, {"error": f"Offline DB Error: {e}"}

# Setup Bot
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        # IMPORTANT: You must enable "Server Members Intent" in the Discord Developer Portal
        # for Invite Tracking to work. If the bot crashes, keep these commented out.
        intents.members = True 
        intents.invites = True
        super().__init__(command_prefix="!", intents=intents)
        self.invite_cache = {}

    async def setup_hook(self):
        # Register persistent views here so buttons work after restart
        self.add_view(UserDashboardView())
        self.add_view(PurchaseView())
        self.add_view(TicketView())
        self.add_view(RedeemSystemView())
        print("Bot setup complete. Run '!sync' in your server to enable slash commands.")

bot = MyBot()

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
    else:
        print(f"App Command Error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ An error occurred: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ An error occurred: {error}", ephemeral=True)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use this command.")
    elif isinstance(error, commands.CommandNotFound):
        pass # Ignore unknown commands
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument! Usage: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
    else:
        print(f"Command Error: {error}")
        await ctx.send(f"❌ An error occurred: {error}")

@bot.event
async def on_ready():
    # Force sync on this version update to ensure commands are refreshed
    if hasattr(bot, 'synced_commands_v3_fix_timeout'):
        return
    bot.synced_commands_v3_fix_timeout = True

    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    print("!!! NEW VERSION LOADED - DEBUG MODE !!!")
    print("⏳ Auto-syncing commands... (This might take a moment)")
    
    # Run sync in background to avoid blocking
    bot.loop.create_task(background_sync())
    
    # Try to sync immediately to known guilds if background sync is slow
    try:
        if bot.guilds:
            first_guild = bot.guilds[0]
            print(f"⏳ Fast-syncing to first guild: {first_guild.name} ({first_guild.id})")
            bot.tree.copy_global_to(guild=first_guild)
            await bot.tree.sync(guild=first_guild)
            print("✅ Fast-sync complete!")
    except Exception as e:
        print(f"⚠️ Fast-sync failed: {e}")
    
    # Cache Invites
    print("⏳ Caching invites for tracking...")
    for guild in bot.guilds:
        try:
            bot.invite_cache[guild.id] = await guild.invites()
            print(f"✅ Cached invites for: {guild.name}")
        except Exception as e:
            print(f"❌ Failed to cache invites for {guild.name}: {e}")

async def background_sync():
    print("⏳ Starting background sync...")
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands to: {guild.name}")
        except Exception as e:
            print(f"❌ Failed to sync to {guild.name}: {e}")
    print("------ Sync Complete ------")

@bot.event
async def on_member_join(member):
    config = load_config()
    welcome_channel_id = config.get('welcome_channel_id')
    
    if welcome_channel_id:
        channel = member.guild.get_channel(welcome_channel_id)
        if channel:
            embed = discord.Embed(
                title=f"👋 Welcome to {member.guild.name}!",
                description=f"Hello {member.mention}, welcome to the community! We're glad to have you here.",
                color=discord.Color.teal()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(
                name="🚀 **Getting Started**",
                value="• Read the rules in the rules channel.\n• Check out `#purchase` to get a license.\n• Need help? Open a ticket!",
                inline=False
            )
            embed.set_footer(text="Pillow Player • Automate your game")
            
            try:
                await channel.send(content=f"Welcome {member.mention}!", embed=embed)
            except Exception as e:
                print(f"Failed to send welcome message: {e}")

    # --- Invite Tracking ---
    try:
        inviter = None
        old_invites = bot.invite_cache.get(member.guild.id, [])
        new_invites = await member.guild.invites()
        bot.invite_cache[member.guild.id] = new_invites
        
        for invite in new_invites:
            # Find the invite that incremented in uses
            for old_invite in old_invites:
                if invite.code == old_invite.code and invite.uses > old_invite.uses:
                    inviter = invite.inviter
                    break
            if inviter: break
            
        if inviter:
            print(f"DEBUG: User {member.name} joined via invite from {inviter.name}")
            
            # ANTI-BOT CHECK: Do not reward if the inviter or the new member is a bot
            if member.bot:
                print(f"DEBUG: Member {member.name} is a bot. No invite reward.")
                return
            if inviter.bot:
                print(f"DEBUG: Inviter {inviter.name} is a bot. No invite reward.")
                return

            # Add 1 Credit
            payload = {
                "admin_secret": ADMIN_SECRET,
                "action": "add",
                "discord_id": str(inviter.id),
                "amount": 1
            }
            # Use fallback to ensure it works even if server is off
            status, data = await db_query_fallback("/pcredit/manage", payload)
            
            if status == 200:
                new_bal = data.get("new_balance", "?")
                # Log it
                try:
                    log_embed = discord.Embed(title="🤝 Invite Reward Claimed", color=discord.Color.green())
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.add_field(name="📥 Inviter", value=f"{inviter.mention}\n`{inviter.name}`", inline=True)
                    log_embed.add_field(name="👤 New Member", value=f"{member.mention}\n`{member.name}`", inline=True)
                    log_embed.add_field(name="🎁 Reward", value="**+1 PCredit**", inline=True)
                    log_embed.add_field(name="💰 New Balance", value=f"**{new_bal}** Credits", inline=False)
                    log_embed.set_footer(text="Pillow Player Invite System", icon_url=inviter.display_avatar.url)
                    log_embed.timestamp = datetime.datetime.now()
                    
                    # Try to find 'inv-reward' channel
                    target_channel = next((c for c in member.guild.text_channels if "inv-reward" in c.name), None)
                    
                    if target_channel:
                        await target_channel.send(embed=log_embed)
                    else:
                        await send_log_embed(member.guild, log_embed)
                except: pass
            else:
                print(f"Failed to add credit for invite: {data}")
                
    except Exception as e:
        print(f"Invite tracking error: {e}")

@bot.tree.command(name="review", description="Leave a review for Pillow Player")
@app_commands.describe(rating="Rate from 1 to 5 stars", comment="Your feedback")
async def review(interaction: discord.Interaction, rating: app_commands.Range[int, 1, 5], comment: str):
    # Defer immediately to prevent "Application did not respond" timeout
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Load config
        config = load_config()
        review_channel_id = config.get('review_channel_id')
        
        # Create Embed
        stars = "⭐" * rating
        embed = discord.Embed(title="🌟 **NEW REVIEW**", color=discord.Color.gold())
        embed.add_field(name="Rating", value=stars, inline=False)
        embed.add_field(name="Comment", value=f"\"{comment}\"", inline=False)
        
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Reviewer", value=f"{interaction.user.mention}", inline=False)
        
        embed.set_footer(text=f"Pillow Player Review • {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}", icon_url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png")

        # Send
        if review_channel_id:
            channel = interaction.guild.get_channel(review_channel_id)
            if channel:
                await channel.send(embed=embed)
                await interaction.followup.send("✅ Review submitted! Thank you.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Review channel not found. Please contact admin.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Review channel not configured.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

user_reset_last = {}

@bot.tree.command(name="mykeys", description="View your keys")
async def mykeys(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    payload = {"discord_id": str(interaction.user.id), "admin_secret": ADMIN_SECRET}
    status, data = await db_query_fallback("/get_user_keys", payload)
    keys = data.get("keys", [])
    embed = discord.Embed(title="Your Keys", color=discord.Color.blurple())
    if not keys:
        embed.description = "No keys linked."
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    lines = []
    if isinstance(keys[0], dict):
        for k in keys[:20]:
            code = k.get("key_code") or k.get("key") or "Unknown"
            st = k.get("status", "unknown")
            exp = k.get("expires_at", "")
            line = f"• {code} — {st}" + (f" (exp: {exp})" if exp else "")
            lines.append(line)
    else:
        for code in keys[:20]:
            lines.append(f"• {code}")
    embed.description = "\n".join(lines)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="status", description="Show your license status")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    payload = {"discord_id": str(interaction.user.id), "admin_secret": ADMIN_SECRET}
    status, data = await db_query_fallback("/get_user_keys", payload)
    keys = data.get("keys", [])
    total = len(keys)
    active = 0
    unused = 0
    expired = 0
    if keys and isinstance(keys[0], dict):
        for k in keys:
            s = (k.get("status") or "").lower()
            if s in ["active", "used"]:
                active += 1
            elif s == "unused":
                unused += 1
            elif s == "expired":
                expired += 1
    embed = discord.Embed(title="License Status", color=discord.Color.green())
    if total == 0:
        embed.description = "No keys linked."
    else:
        embed.add_field(name="Total", value=str(total), inline=True)
        if active or unused or expired:
            embed.add_field(name="Active", value=str(active), inline=True)
            embed.add_field(name="Unused", value=str(unused), inline=True)
            embed.add_field(name="Expired", value=str(expired), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="reset_hwid", description="Reset HWID for your key")
@app_commands.describe(key="Your key code (required if multiple linked)")
async def reset_hwid(interaction: discord.Interaction, key: str = None):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    now = int(time.time())
    last = user_reset_last.get(uid, 0)
    if now - last < 3600:
        remain = 3600 - (now - last)
        await interaction.followup.send(f"Please wait {remain//60}m before requesting another reset.", ephemeral=True)
        return
    payload = {"discord_id": uid, "admin_secret": ADMIN_SECRET}
    status, data = await db_query_fallback("/get_user_keys", payload)
    keys = data.get("keys", [])
    owned = []
    if keys:
        if isinstance(keys[0], dict):
            owned = [k.get("key_code") or k.get("key") for k in keys if (k.get("key_code") or k.get("key"))]
        else:
            owned = keys
    if not owned:
        await interaction.followup.send("No keys linked.", ephemeral=True)
        return
    target = key
    if not target:
        if len(owned) == 1:
            target = owned[0]
        else:
            lines = "\n".join([f"• {k}" for k in owned[:20]])
            await interaction.followup.send(f"Multiple keys linked. Specify one:\n{lines}", ephemeral=True)
            return
    if target not in owned:
        await interaction.followup.send("That key is not linked to your account.", ephemeral=True)
        return
    rstatus, rdata = await db_query_fallback("/reset_batch", {"admin_secret": ADMIN_SECRET, "keys": [target]})
    if rstatus == 200:
        user_reset_last[uid] = now
        await interaction.followup.send(f"HWID reset for {target}.", ephemeral=True)
    else:
        msg = rdata.get("error") or "Reset failed."
        await interaction.followup.send(msg, ephemeral=True)
@bot.tree.command(name="postrules", description="Post the official Server Rules (Admin Only)")
async def postrules(interaction: discord.Interaction, channel: discord.TextChannel = None):
    # DEBUG: Print to console to verify command is hit
    print("DEBUG: /postrules command received!")
    
    # DEFER IMMEDIATELY to prevent timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="📜 **Community Guidelines & Rules**",
        description="To ensure a safe and enjoyable environment for everyone, please adhere to the following rules.",
        color=discord.Color.dark_red()
    )
    
    embed.add_field(
        name="🛡️ **1. Respect & Behavior**",
        value=(
            "• Treat everyone with respect. Harassment, hate speech, and discrimination are **strictly prohibited**.\n"
            "• Avoid toxic behavior, flame wars, and instigating conflicts.\n"
            "• Do not impersonate staff or other members."
        ),
        inline=False
    )
    
    embed.add_field(
        name="💬 **2. Chat & Content**",
        value=(
            "• Keep discussions relevant to the channel topic.\n"
            "• **No Spamming:** Avoid excessive caps, flooding, or repetitive messages.\n"
            "• **No NSFW:** Adult content, gore, or illegal material is not allowed.\n"
            "• No self-promotion or advertising without explicit staff permission."
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔒 **3. Privacy & Security**",
        value=(
            "• Do not share personal information (doxxing) of yourself or others.\n"
            "• Malicious links, IP grabbers, or malware distribution will result in an **immediate ban**."
        ),
        inline=False
    )

    embed.add_field(
        name="⚖️ **4. Pillow Player Usage**",
        value=(
            "• Support is provided for legitimate users only.\n"
            "• Sharing license keys or attempting to crack the software is prohibited.\n"
            "• Scamming other users in the marketplace will not be tolerated."
        ),
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/3208/3208726.png")
    embed.set_footer(text="Pillow Player • Failure to follow rules may result in a ban.", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed)
        await interaction.followup.send(f"✅ Rules posted to {target_channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}")

@bot.tree.command(name="postsetup", description="Post the official Setup Guide (Admin Only)")
async def postsetup(interaction: discord.Interaction, channel: discord.TextChannel = None):
    # DEBUG: Print to verify command is hit
    print("DEBUG: /postsetup command received!")

    # DEFER IMMEDIATELY to prevent timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return
        
    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="🛠️ **Pillow Player - Quick Setup Guide**",
        description="Get your multi-instance farm running in minutes with this step-by-step guide.",
        color=discord.Color.teal()
    )
    
    embed.add_field(
        name="1️⃣ **Installation**",
        value=(
            "• **Download Python**: [Click Here](https://www.python.org/downloads/)\n"
            "⚠️ **CRITICAL:** Check **\"Add Python to PATH\"** during installation.\n"
            "• **Download Pillow Player**: Get the `.zip` file from the official channel.\n"
            "• **Extract**: Unzip to a new folder. **DO NOT** run directly from the zip."
        ),
        inline=False
    )
    
    embed.add_field(
        name="2️⃣ **Add Accounts**",
        value=(
            "• Open `accounts.json` in a text editor.\n"
            "• Delete any example content.\n"
            "• Paste your Roblox cookies in the JSON format:\n"
            "```json\n"
            "[\n"
            "    {\n"
            "        \"cookie\": \"_|WARNING:-DO-NOT-SHARE-THIS...\"\n"
            "    }\n"
            "]\n"
            "```"
        ),
        inline=False
    )
    
    embed.add_field(
        name="3️⃣ **Configuration**",
        value=(
            "• Open `config.json` to tweak settings:\n"
            "• `PlaceId`: The Game ID you want to farm.\n"
            "• `Total Instance`: Number of accounts to launch.\n"
            "• `DelayOpen`: Seconds to wait between launches (Recommended: 5-10s)."
        ),
        inline=False
    )
    
    embed.add_field(
        name="4️⃣ **Launch & Enjoy**",
        value=(
            "• Run **`Run_Pillow_Player.bat`**.\n"
            "• Enter your **License Key** when prompted.\n"
            "• Watch your instances fly! 🚀"
        ),
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png")
    embed.set_footer(text="Pillow Player • Need help? Open a ticket.", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed)
        await interaction.followup.send(f"✅ Setup guide posted to {target_channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}")

@bot.tree.command(name="postrejoin", description="Post an explanation of the Pillow ReJoin monitor (Admin Only)")
async def postrejoin(interaction: discord.Interaction, channel: discord.TextChannel = None):
    print("DEBUG: /postrejoin command received!")
    
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel

    embed = discord.Embed(
        title="🧠 Pillow ReJoin – Auto Rejoin & DM Control Panel",
        description=(
            "**What it does**\n"
            "Pillow ReJoin keeps your Roblox accounts online by automatically detecting disconnects and relaunching them.\n"
            "It also gives you a Discord **DM Control Panel** to manage every instance.\n\n"
            "**How to use it**\n"
            "1. Run the latest Pillow Player client.\n"
            "2. After login, you will receive a DM from the Pillow ReJoin bot.\n"
            "3. Use the DM panel to:\n"
            "   • View which accounts are running\n"
            "   • Relaunch stuck/offline accounts\n"
            "   • Kill all Roblox instances\n"
            "   • Request a desktop screenshot\n\n"
            "**Notes**\n"
            "• Auto‑rejoin is fully automatic; the panel is optional.\n"
            "• Keep Pillow Player open on your host PC for the bot to stay online."
        ),
        color=discord.Color.blurple()
    )

    base_dir = os.path.dirname(os.path.abspath(__file__))
    banner_path = os.path.join(base_dir, "banner.png")
    file = None
    if os.path.exists(banner_path):
        try:
            file = discord.File(banner_path, filename="banner.png")
            embed.set_image(url="attachment://banner.png")
        except Exception as e:
            print(f"Failed to attach banner.png: {e}")

    try:
        if file:
            await target_channel.send(embed=embed, file=file)
        else:
            await target_channel.send(embed=embed)
        await interaction.followup.send(f"✅ ReJoin info posted to {target_channel.mention}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}", ephemeral=True)

@bot.tree.command(name="postpurchase", description="Post the Purchase Panel (Admin Only)")
async def postpurchase(interaction: discord.Interaction, channel: discord.TextChannel = None):
    # DEBUG: Print to console to verify command is hit
    print("DEBUG: /postpurchase command received!")
    
    # DEFER IMMEDIATELY to prevent timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="💎 **Pillow Player Premium**",
        description=(
            "**🚀 Elevate Your Roblox Automation**\n\n"
            "Unlock the full potential of your experience with our premium tools.\n"
            "Get instant access, reliable support, and powerful features."
        ),
        color=discord.Color.from_rgb(255, 215, 0) # Gold
    )
    
    embed.add_field(
        name="⚡ **Premium Features**",
        value=(
            "> **Unlimited Multi-Instance**\n"
            "> **Advanced FPS Unlocker**\n"
            "> **Smart Account Manager**\n"
            "> **Priority Support Access**"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💳 **Pricing Options**",
        value=(
            "**• PayPal:** `$3.99 USD` _(Instant Key)_\n"
            "**• Robux:** `800 Robux` _(Ticket Support)_"
        ),
        inline=False
    )

    embed.add_field(
        name="📥 **How to Buy**",
        value="Click a button below to start your purchase safely.",
        inline=False
    )
    
    # Use Guild Icon as "Custom Logo" instead of generic flaticon
    if interaction.guild and interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    else:
        # Fallback if no guild icon, but distinct from other panels
        embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2534/2534204.png") # Distinct Premium Icon
        
    embed.set_footer(text="Official Pillow Player Store", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed, view=PurchaseView())
        await interaction.followup.send(f"✅ Purchase panel posted to {target_channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}")

@bot.tree.command(name="postdashboard", description="Send the User Dashboard Panel (Admin Only)")
async def postdashboard(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="🛡️ **Pillow Player License Manager**",
        description=(
            "**Welcome to your personal control center.**\n\n"
            "Manage your license, check your subscription status, and sync your customer roles instantly.\n"
            "Select an option below to get started."
        ),
        color=discord.Color.from_rgb(46, 204, 113)
    )

    embed.add_field(
        name="🔗 **Activate License**",
        value="> Link your key to unlock access.",
        inline=True
    )
    embed.add_field(
        name="📊 **Subscription**",
        value="> View expiry & HWID status.",
        inline=True
    )
    embed.add_field(
        name="👑 **Customer Role**",
        value="> Sync your Verified Buyer role.",
        inline=True
    )
    embed.add_field(
        name="❓ **Support**",
        value="> Guides & troubleshooting.",
        inline=True
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png")
    embed.set_footer(text="Secure Auth System • Powered by Pillow Player", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    await target_channel.send(embed=embed, view=UserDashboardView())
    await interaction.response.send_message(f"✅ Dashboard sent to {target_channel.mention}", ephemeral=True)

@bot.tree.command(name="postfeatures", description="Post the official Pillow Player features list v2 (Admin Only)")
async def postfeatures(interaction: discord.Interaction, channel: discord.TextChannel = None):
    # DEBUG: Print to console to verify command is hit
    print("DEBUG: /postfeatures command received!")
    
    # DEFER IMMEDIATELY to prevent timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="🌟 **Pillow Player Premium Features**",
        description="Experience the ultimate Roblox multi-instance automation tool designed for performance and reliability.",
        color=discord.Color.dark_purple()
    )
    
    embed.add_field(
        name="🚀 **Advanced Multi-Instance**",
        value="Run unlimited clients simultaneously. Our proprietary **Mutex Cleaning Technology** ensures zero conflicts and smooth operation.",
        inline=False
    )
    embed.add_field(
        name="⚡ **Performance Boosters**",
        value="Includes built-in **FPS Unlocker** and resource optimization to keep your CPU usage low while running dozens of accounts.",
        inline=False
    )
    embed.add_field(
        name="📂 **Secure Account Manager**",
        value="Locally encrypted storage for your accounts. Switch profiles instantly without re-entering credentials.",
        inline=False
    )
    embed.add_field(
        name="🎮 **Smart Auto-Launch**",
        value="Create custom launch profiles with specific Place IDs and auto-join settings. Launch your entire farm in one click.",
        inline=False
    )
    embed.add_field(
        name="🖼️ **Window Management**",
        value="Automatically resize, rename, and organize game windows. Support for borderless mode and custom layouts.",
        inline=False
    )
    embed.add_field(
        name="🔒 **Enterprise Security**",
        value="HWID-locked licensing ensures your investment is protected. Offline verification support included.",
        inline=False
    )
    embed.add_field(
        name="🤖 **Discord Command Center**",
        value="Manage your license, check status, and get support directly from this Discord server.",
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png")
    embed.set_footer(text="Pillow Player • Elevate Your Gameplay", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed)
        await interaction.followup.send(f"✅ Features list posted to {target_channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}")

@bot.tree.command(name="postguide", description="Post the official User Command Guide (Admin Only)")
async def postguide(interaction: discord.Interaction, channel: discord.TextChannel = None):
    # DEBUG: Print to console to verify command is hit
    print("DEBUG: /postguide command received!")
    
    # DEFER IMMEDIATELY to prevent timeout
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="📘 **Pillow Player - User Command Guide**",
        description="Here is how to use the bot commands properly.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🔑 **How to Claim Your Key**",
        value=(
            "`/claim key:YOUR_KEY_HERE`\n"
            "*(Type `/claim` and select the command, then paste your key)*"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⭐ **How to Leave a Review**",
        value=(
            "`/review rating:5 comment:Great bot!`"
        ),
        inline=False
    )
    
    embed.add_field(
        name="❓ **Troubleshooting**",
        value=(
            "**Can't see the Slash (`/`) commands?**\n"
            "1. Try updating your Discord app (Ctrl+R).\n"
            "2. Check if you have 'Use Application Commands' enabled in your User Settings > Privacy."
        ),
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/682/682055.png")
    embed.set_footer(text="Pillow Player Support", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed)
        await interaction.followup.send(f"✅ Guide posted to {target_channel.mention}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to post: {e}")

@bot.command()
async def postfeatures(ctx, channel: discord.TextChannel = None):
    """(Text Command) Post features list. Use this if slash command fails."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission.")
        return

    target_channel = channel or ctx.channel
    
    embed = discord.Embed(
        title="🌟 **Pillow Player Features**",
        description="The ultimate tool for Roblox multi-instance management and automation.",
        color=discord.Color.purple()
    )
    
    embed.add_field(
        name="🚀 **Multi-Instance Manager**",
        value="Run unlimited Roblox accounts simultaneously without conflicts. Our advanced mutex cleaner ensures smooth multi-client operation.",
        inline=False
    )
    embed.add_field(
        name="⚡ **FPS Unlocker**",
        value="Break the 60 FPS limit for smoother, high-performance gameplay on all your instances.",
        inline=False
    )
    embed.add_field(
        name="📂 **Account Manager**",
        value="Securely save, load, and organize your Roblox accounts. Switch between accounts instantly.",
        inline=False
    )
    embed.add_field(
        name="🎮 **Auto-Launch**",
        value="One-click launch into your favorite games. Setup launch profiles for different scenarios.",
        inline=False
    )
    embed.add_field(
        name="🖼️ **Window Management**",
        value="Automatically rename and organize game windows for easy navigation.",
        inline=False
    )
    embed.add_field(
        name="🔒 **Secure Auth**",
        value="Enterprise-grade license protection with HWID locking ensures your access is secure.",
        inline=False
    )
    embed.add_field(
        name="🤖 **Discord Integration**",
        value="Control your session, check status, and manage licenses directly from this Discord server.",
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png")
    embed.set_footer(text="Pillow Player • Elevate Your Gameplay")
    
    try:
        await target_channel.send(embed=embed)
        await ctx.send(f"✅ Features list posted to {target_channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Failed to post: {e}")

@bot.command()
async def postguide(ctx, channel: discord.TextChannel = None):
    """(Text Command) Post guide. Use this if slash command fails."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission.")
        return

    target_channel = channel or ctx.channel
    
    embed = discord.Embed(
        title="📘 **Pillow Player - User Command Guide**",
        description="Here is how to use the bot commands properly.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🔑 **How to Claim Your Key**",
        value=(
            "`/claim key:YOUR_KEY_HERE`\n"
            "*(Type `/claim` and select the command, then paste your key)*"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⭐ **How to Leave a Review**",
        value=(
            "`/review rating:5 comment:Great bot!`"
        ),
        inline=False
    )
    
    embed.add_field(
        name="❓ **Troubleshooting**",
        value=(
            "**Can't see the Slash (`/`) commands?**\n"
            "1. Try updating your Discord app (Ctrl+R).\n"
            "2. Check if you have 'Use Application Commands' enabled in your User Settings > Privacy."
        ),
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/682/682055.png")
    embed.set_footer(text="Pillow Player Support")
    
    try:
        await target_channel.send(embed=embed)
        await ctx.send(f"✅ Guide posted to {target_channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Failed to post: {e}")

@bot.command()
async def postrules(ctx, channel: discord.TextChannel = None):
    """(Text Command) Post server rules."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission.")
        return

    target_channel = channel or ctx.channel
    
    embed = discord.Embed(
        title="📜 **Server Rules**",
        description="Please read and follow the rules below to ensure a safe and friendly community.",
        color=discord.Color.red()
    )
    
    embed.add_field(
        name="🛡️ **1. General Behavior**",
        value=(
            "• Be respectful to all members and staff.\n"
            "• No harassment, hate speech, or discrimination of any kind.\n"
            "• Do not spam, flood, or post malicious links.\n"
            "• Use English in the main channels."
        ),
        inline=False
    )
    
    embed.add_field(
        name="💬 **2. Chat Etiquette**",
        value=(
            "• Keep conversations relevant to the channel topic.\n"
            "• No excessive caps or formatting abuse.\n"
            "• Do not ping staff members without a valid reason.\n"
            "• No advertising or self-promotion without permission."
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚠️ **3. Content Guidelines**",
        value=(
            "• No NSFW, gore, or illegal content.\n"
            "• No doxxing or sharing personal information of others.\n"
            "• Do not discuss or distribute cheats/exploits for other games."
        ),
        inline=False
    )

    embed.add_field(
        name="⚖️ **4. Marketplace Rules**",
        value=(
            "• All sales must go through the official ticket system.\n"
            "• No scamming or attempting to defraud users.\n"
            "• Chargebacks will result in an immediate ban."
        ),
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/3208/3208726.png")
    embed.set_footer(text="By staying in this server, you agree to these rules. Staff decisions are final.")
    
    try:
        await target_channel.send(embed=embed)
        await ctx.send(f"✅ Rules posted to {target_channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Failed to post: {e}")

@bot.command()
async def postpurchase(ctx, channel: discord.TextChannel = None):
    """(Text Command) Post purchase panel."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission.")
        return

    target_channel = channel or ctx.channel
    
    embed = discord.Embed(
        title="💎 **Pillow Player Premium**",
        description=(
            "**🚀 Elevate Your Roblox Automation**\n\n"
            "Unlock the full potential of your experience with our premium tools.\n"
            "Get instant access, reliable support, and powerful features."
        ),
        color=discord.Color.from_rgb(255, 215, 0) # Gold
    )
    
    embed.add_field(
        name="⚡ **Premium Features**",
        value=(
            "> **Unlimited Multi-Instance**\n"
            "> **Advanced FPS Unlocker**\n"
            "> **Smart Account Manager**\n"
            "> **Priority Support Access**"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💳 **Pricing Options**",
        value=(
            "**• PayPal:** `$3.99 USD` _(Instant Key)_\n"
            "**• Robux:** `800 Robux` _(Ticket Support)_"
        ),
        inline=False
    )

    embed.add_field(
        name="📥 **How to Buy**",
        value="Click a button below to start your purchase safely.",
        inline=False
    )
    
    if ctx.guild and ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)
    else:
        embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2534/2534204.png")
        
    embed.set_footer(text="Official Pillow Player Store", icon_url=ctx.guild.icon.url if ctx.guild and ctx.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    try:
        await target_channel.send(embed=embed, view=PurchaseView())
        await ctx.send(f"✅ Purchase panel posted to {target_channel.mention}")
    except Exception as e:
        await ctx.send(f"❌ Failed to post: {e}")

@bot.command()
async def fix_permissions(ctx):
    """Forcefully syncs commands to this server to fix permission issues."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You need Admin permissions to run this fix.")
        return

    msg = await ctx.send("⏳ **Fixing Permissions (Deep Clean)...**\nStarting sync sequence...")
    
    try:
        # 1. Check if commands are loaded
        commands = [c.name for c in bot.tree.get_commands()]
        await msg.edit(content=f"⏳ **Fixing Permissions...**\nFound {len(commands)} commands in code.")
        
        # 2. Clear Guild Commands (Internal Cache Only)
        # We don't need to sync the empty state; we just clear the internal registry
        # so we can cleanly copy global commands over.
        bot.tree.clear_commands(guild=ctx.guild)
        
        # 3. Copy Global to Guild
        await msg.edit(content="⏳ **Step 2/3:** Registering fresh commands...")
        bot.tree.copy_global_to(guild=ctx.guild)
        
        # 4. Sync to Guild (The ONLY API Call)
        await msg.edit(content="⏳ **Step 3/3:** Pushing updates to Discord (This may take a moment)...")
        
        # Add timeout to prevent infinite hanging
        import asyncio
        try:
            synced = await asyncio.wait_for(bot.tree.sync(guild=ctx.guild), timeout=30.0)
        except asyncio.TimeoutError:
            await msg.edit(content="⚠️ **Sync Timed Out**\nDiscord didn't respond in time. This usually means:\n1. Rate limits (wait 5 mins)\n2. Discord API issues\n\nTry running `!claim` or `!review` as text commands instead!")
            return
        
        await msg.edit(content=f"✅ **Fixed!**\nSynced **{len(synced)}** commands to this server.\n\n👉 **Commands should appear immediately.**\nIf not, try restarting your Discord app (Ctrl+R).\n\n**Still stuck?** Use `!claim` and `!review` (text commands).")
        
    except Exception as e:
        print(f"Sync Error: {e}")
        await msg.edit(content=f"❌ **Fix Failed:** {e}")

@bot.command()
async def debug_user(ctx, member: discord.Member):
    """Checks why a user might not be able to see commands."""
    if not ctx.author.guild_permissions.administrator:
        return
        
    embed = discord.Embed(title=f"🔍 Debug: {member.display_name}", color=discord.Color.orange())
    
    # 1. Check Roles
    roles = [r.name for r in member.roles if r.name != "@everyone"]
    embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
    
    # 2. Check Permissions
    perms = member.guild_permissions
    embed.add_field(name="Administrator", value="✅ Yes" if perms.administrator else "❌ No", inline=True)
    embed.add_field(name="Use App Commands", value="✅ Yes" if perms.use_application_commands else "❌ NO (This is the issue!)", inline=True)
    
    await ctx.send(embed=embed)

@bot.command()
async def clear_global(ctx):
    """Clears global commands to remove duplicates (Admin Only)."""
    if not ctx.author.guild_permissions.administrator: return
    msg = await ctx.send("⏳ Clearing global commands...")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)
    # Restore commands to tree
    # We need to reload them or just assume they are still in memory? 
    # clear_commands removes them from the tree, so we would need to add them back.
    # Actually, it's safer to just restart the bot to restore them.
    await msg.edit(content="✅ Global commands cleared. **You must restart the bot now to reload commands.**")

# --- UTILITY COMMANDS ---

@bot.command()
async def sync(ctx):
    """Syncs slash commands to the current server (Admin Only)."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission to sync commands.")
        return

    msg = await ctx.send("⏳ Syncing commands...")
    try:
        # 1. Clear Global Commands to remove duplicates
        # (We temporarily remove them from the tree, sync global to wipe them, then restore them)
        global_commands = [c for c in bot.tree.get_commands()]
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync() # Wipes global commands from Discord
        
        # Restore commands to the tree for Guild sync
        for c in global_commands:
            bot.tree.add_command(c)
            
        # 2. Sync to Current Guild (Instant updates)
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        
        await msg.edit(content=f"✅ Successfully synced {len(synced)} commands to this server! (Duplicates removed)")
    except Exception as e:
        await msg.edit(content=f"❌ Sync failed: {e}")

# --- UI VIEWS ---

class TicketView(View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent view

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("⚠️ Ticket will close in 5 seconds...", ephemeral=True)
        await asyncio.sleep(5)
        await interaction.channel.delete()

class PurchaseView(View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent view

    @discord.ui.button(label="💳 Pay with PayPal ($3.99)", style=discord.ButtonStyle.primary, custom_id="purchase_paypal")
    async def paypal_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id in ticket_lock:
            await interaction.response.send_message("⏳ Please wait, processing your previous request...", ephemeral=True)
            return

        ticket_lock.add(interaction.user.id)
        try:
            guild = interaction.guild
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            # Sanitize channel name
            channel_name = f"paypal-{interaction.user.name}".lower().replace(" ", "-")
            
            # Check if ticket already exists
            existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if existing_channel:
                await interaction.response.send_message(f"❌ You already have a ticket open: {existing_channel.mention}", ephemeral=True)
                return

            try:
                channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)
                
                embed = discord.Embed(title="💳 PayPal Payment Instructions", color=discord.Color.blue())
                embed.description = (
                    "Please send **$3.99 USD** to our PayPal address:\n"
                    "📩 **pillowxyxx@gmail.com**\n\n"
                    "⚠️ **IMPORTANT:** Include your Discord Username in the payment note!\n"
                    "📸 **Proof:** Upload a screenshot of the payment here."
                )
                embed.set_footer(text="Support will review your payment shortly.")
                
                await channel.send(f"{interaction.user.mention}", embed=embed, view=TicketView())
                await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Failed to create ticket: {e}", ephemeral=True)
        finally:
            if interaction.user.id in ticket_lock:
                ticket_lock.remove(interaction.user.id)

    @discord.ui.button(label="💎 Pay with Robux (800)", style=discord.ButtonStyle.success, custom_id="purchase_robux")
    async def robux_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id in ticket_lock:
            await interaction.response.send_message("⏳ Please wait, processing your previous request...", ephemeral=True)
            return

        ticket_lock.add(interaction.user.id)
        try:
            guild = interaction.guild
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            # Sanitize channel name
            channel_name = f"robux-{interaction.user.name}".lower().replace(" ", "-")
            
            # Check if ticket already exists
            existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
            if existing_channel:
                await interaction.response.send_message(f"❌ You already have a ticket open: {existing_channel.mention}", ephemeral=True)
                return

            try:
                channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)
                
                embed = discord.Embed(title="💎 Robux Payment Instructions", color=discord.Color.green())
                embed.description = (
                    "Please purchase our Gamepass/T-Shirt for **800 Robux**:\n"
                    "🔗 **[INSERT ROBLOX GAMEPASS LINK HERE]**\n\n"
                    "⚠️ **IMPORTANT:** Roblox taxes are covered by you (if applicable).\n"
                    "📸 **Proof:** Upload a screenshot of the purchase transaction here."
                )
                embed.set_footer(text="Support will review your payment shortly.")
                
                await channel.send(f"{interaction.user.mention}", embed=embed, view=TicketView())
                await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Failed to create ticket: {e}", ephemeral=True)
        finally:
            if interaction.user.id in ticket_lock:
                ticket_lock.remove(interaction.user.id)

class ClaimKeyModal(discord.ui.Modal, title="Claim License Key"):
    key_input = discord.ui.TextInput(
        label="Enter your License Key",
        placeholder="XK-XXXXXXXX-XXXX",
        min_length=10,
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            payload = {
                "admin_secret": ADMIN_SECRET,
                "key": self.key_input.value.strip(),
                "discord_id": str(interaction.user.id)
            }
            status, data = await db_query_fallback("/link_discord", payload)
            
            if status == 200:
                # Use server message if available
                msg = data.get('message', f"Key `{self.key_input.value}` is now linked to your Discord account.")
                await interaction.followup.send(f"✅ {msg}", ephemeral=True)
            else:
                error_msg = data.get('error', 'Unknown Error')
                await interaction.followup.send(f"❌ Failed: {error_msg}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

class UserDashboardView(View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent view

    @discord.ui.button(label="🔗 Activate License", style=discord.ButtonStyle.success, custom_id="dashboard_claim")
    async def claim_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ClaimKeyModal())

    @discord.ui.button(label="📊 Subscription", style=discord.ButtonStyle.primary, custom_id="dashboard_status")
    async def status_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            payload = {
                "admin_secret": ADMIN_SECRET,
                "discord_id": str(interaction.user.id)
            }
            status, data = await db_query_fallback("/get_user_keys", payload)
            
            if status == 200:
                keys = data.get("keys", [])
                if not keys:
                    await interaction.followup.send("ℹ️ You don't have any keys linked to your account.", ephemeral=True)
                    return
                
                embed = discord.Embed(title="📊 My Subscription Status", color=discord.Color.blue())
                for k in keys:
                    is_banned = k.get('is_banned', False)
                    
                    status_emoji = "🟢" if k['status'] == 'unused' else "🔴"
                    if is_banned:
                        status_emoji = "🚫"
                        
                    hwid_status = "Linked" if k['hwid'] else "Not Linked"
                    if is_banned:
                        hwid_status = "⚠️ BANNED HWID"
                    
                    info = f"**Status:** {status_emoji} {k['status'].title()}"
                    if is_banned:
                        info += " (BANNED)"
                    info += "\n"
                    
                    info += f"**HWID:** {hwid_status}\n"
                    info += f"**Run Count:** {k.get('run_count', 0)}\n"
                    
                    if k['duration_hours'] > 0:
                         info += f"**Duration:** {k['duration_hours']} Hours\n"
                    else:
                         info += f"**Duration:** Lifetime\n"
                         
                    if k['expires_at']:
                        info += f"**Expires:** {k['expires_at']}\n"
                        
                    embed.add_field(name=f"🔑 {k['key_code']}", value=info, inline=False)
                    
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to fetch info: {data.get('error', 'Unknown Error')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="❓ Support Guide", style=discord.ButtonStyle.secondary, custom_id="dashboard_help")
    async def help_button(self, interaction: discord.Interaction, button: Button):
        embed = discord.Embed(
            title="❓ **Pillow Player Support**",
            description="Need help getting started? Follow these steps.",
            color=discord.Color.gold()
        )
        
        embed.add_field(
            name="1️⃣ **Get a License**",
            value="Don't have a key? Use `/postredeem` or visit the `#purchase` channel.",
            inline=False
        )
        embed.add_field(
            name="2️⃣ **Activate Account**",
            value="Click **'🔗 Activate License'** on the dashboard and paste your key.",
            inline=False
        )
        embed.add_field(
            name="3️⃣ **Install Software**",
            value="Download the latest version from `#download`. Extract the zip and run `Run_Pillow_Player.bat`.",
            inline=False
        )
        embed.add_field(
            name="4️⃣ **Login**",
            value="Enter your license key in the application to start.",
            inline=False
        )
        
        embed.add_field(
            name="🆘 **Still Stuck?**",
            value="Open a ticket in `#support` and our team will assist you.",
            inline=False
        )
        
        embed.set_footer(text="Pillow Player Support", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
        embed.timestamp = datetime.datetime.now()
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👑 Sync Role", style=discord.ButtonStyle.primary, custom_id="dashboard_getrole")
    async def get_role_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        
        # Load config to get role ID
        config = load_config()
        customer_role_id = config.get('customer_role_id')
        
        if not customer_role_id:
             await interaction.followup.send("❌ Customer Role is not configured by admin.", ephemeral=True)
             return

        # Check if user already has the role
        role = interaction.guild.get_role(customer_role_id)
        if not role:
             await interaction.followup.send("❌ Customer Role not found in server.", ephemeral=True)
             return
             
        if role in interaction.user.roles:
             await interaction.followup.send("✅ You already have the Customer role!", ephemeral=True)
             return

        # Check DB for ownership
        try:
            payload = {"discord_id": str(interaction.user.id)}
            status, data = await db_query_fallback("/get_user_keys", payload)
            
            if status == 200 and data.get("keys"):
                # User owns at least one key
                try:
                    await interaction.user.add_roles(role)
                    await interaction.followup.send(f"✅ **Success!** You have been granted the {role.mention} role.", ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send("❌ I don't have permission to manage roles. Please contact admin.", ephemeral=True)
                except Exception as e:
                    await interaction.followup.send(f"❌ Failed to assign role: {e}", ephemeral=True)
            else:
                await interaction.followup.send("❌ You do not own any products. Please claim a key first.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error checking database: {e}", ephemeral=True)

class KeySelect(Select):
    def __init__(self, keys, parent_view):
        self.parent_view = parent_view
        options = []
        for k in keys:
            status = "🟢" if k['status'] == 'unused' else "🔴"
            label = f"{k['key_code']}"
            
            # Resolve User Name
            user_id = k.get('discord_id')
            user_info = ""
            if user_id:
                # Use pre-resolved map if available
                if hasattr(parent_view, 'user_map') and parent_view.user_map and user_id in parent_view.user_map:
                     user_info = f" | User: {parent_view.user_map[user_id]}"
                else:
                    # Fallback to cache
                    try:
                        user = parent_view.timeout # Hacky access to bot? No, use interaction.client in callback usually, but here we are in init.
                        # We can't easily access bot instance here without global 'bot' which is available.
                        from __main__ import bot # Ensure we have access if needed, or rely on global scope
                        user = bot.get_user(int(user_id))
                        if user:
                            user_info = f" | User: {user.name}"
                        else:
                            user_info = f" | User: {user_id}"
                    except:
                        user_info = f" | User: {user_id}"

            description = f"Status: {k['status'].upper()} | Device: {k['device_name'] or 'None'}{user_info}"
            
            # Truncate if too long (max 100 chars)
            if len(description) > 100:
                description = description[:97] + "..."
                
            options.append(discord.SelectOption(label=label, description=description, emoji=status, value=k['key_code']))
        
        super().__init__(placeholder="Select one or more keys to manage...", min_values=1, max_values=min(len(options), 25), options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            keys = self.values
            # Update view with action buttons for these keys
            await interaction.response.edit_message(embed=self.parent_view.get_keys_embed(keys), view=KeyActionView(keys, self.parent_view))
        except Exception as e:
            print(f"Error in KeySelect callback: {e}")
            await interaction.response.send_message(f"❌ Error selecting key: {e}", ephemeral=True)

class KeyActionView(View):
    def __init__(self, keys, main_view):
        super().__init__(timeout=180)
        self.keys = keys
        self.main_view = main_view

    @discord.ui.button(label="Reset Selected", style=discord.ButtonStyle.primary, emoji="🔄")
    async def reset_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            status, data = await db_query_fallback("/reset_batch", {"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if status == 200:
                count = len(self.keys)
                await interaction.followup.send(f"✅ {count} keys have been reset.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Recover (Unban)", style=discord.ButtonStyle.success, emoji="🚑")
    async def recover_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            status, data = await db_query_fallback("/recover_key", {"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if status == 200:
                count = len(self.keys)
                await interaction.followup.send(f"✅ {count} keys have been recovered/unbanned.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Delete Selected", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            status, data = await db_query_fallback("/delete_batch", {"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if status == 200:
                count = len(self.keys)
                await interaction.followup.send(f"🗑️ {count} keys deleted.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Back to List", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(embed=self.main_view.main_embed, view=self.main_view)

class KeyManagementView(View):
    def __init__(self, keys, user_map=None):
        super().__init__(timeout=300)
        self.keys = keys
        self.user_map = user_map or {}
        self.main_embed = discord.Embed(title="🔑 Key Management Panel", description="Select keys from the dropdown below to manage them (Max 25 at a time).", color=discord.Color.blue())
        
        # Pagination check (Select menu max 25 items)
        latest_keys = keys[:25]
        
        if not latest_keys:
            self.add_item(Button(label="No Keys Found", disabled=True))
        else:
            self.add_item(KeySelect(latest_keys, self))

    def get_keys_embed(self, selected_key_codes):
        # Create summary embed for multiple keys
        count = len(selected_key_codes)
        embed = discord.Embed(title=f"Manage {count} Selected Key(s)", color=discord.Color.gold())
        
        if count == 1:
            # Detailed view for single key
            key_code = selected_key_codes[0]
            key_data = next((k for k in self.keys if k['key_code'] == key_code), None)
            if key_data:
                embed.description = f"**Key:** `{key_code}`"
                embed.add_field(name="Status", value=key_data['status'], inline=True)
                embed.add_field(name="HWID", value=str(key_data.get('hwid', 'None')), inline=True)
                embed.add_field(name="Device", value=str(key_data['device_name']), inline=True)
                embed.add_field(name="IP Address", value=str(key_data.get('ip_address', 'Unknown')), inline=True)
                embed.add_field(name="Last Seen", value=str(key_data.get('last_seen', 'Never')), inline=True)
                
                # Discord User
                discord_id = key_data.get('discord_id')
                discord_user = f"<@{discord_id}>" if discord_id else "None"
                embed.add_field(name="User", value=discord_user, inline=True)

                embed.add_field(name="Runs", value=str(key_data.get('run_count', 0)), inline=True)
                
                # New fields
                duration = key_data.get('duration_hours', 0)
                duration_str = "Lifetime" if duration == 0 else f"{duration} Hours"
                embed.add_field(name="Duration", value=duration_str, inline=True)
                
                expires_at = key_data.get('expires_at')
                if expires_at:
                    embed.add_field(name="Expires At", value=str(expires_at), inline=True)
                    
                note = key_data.get('note')
                if note:
                    embed.add_field(name="Note", value=str(note), inline=False)
                    
                embed.add_field(name="Created", value=key_data['created_at'], inline=False)
        else:
            # Summary view for multiple keys
            embed.description = f"**Selected Keys:**\n" + "\n".join([f"`{k}`" for k in selected_key_codes])
            embed.add_field(name="Actions", value="Choose an action below to apply to ALL selected keys.", inline=False)
            
        return embed

    async def refresh(self, interaction):
        # Re-fetch keys
        try:
            status, data = await db_query_fallback("/list", {"admin_secret": ADMIN_SECRET})
            if status == 200:
                new_keys = data.get("keys", [])
                new_user_map = await resolve_users_map(interaction, new_keys)
                new_view = KeyManagementView(new_keys, new_user_map)
                await interaction.message.edit(embed=new_view.main_embed, view=new_view)
            else:
                await interaction.followup.send("Failed to refresh list.", ephemeral=True)
        except:
             pass

class RedeemSystemView(View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent

    @discord.ui.button(label="🛒 Redeem License", style=discord.ButtonStyle.success, custom_id="redeem_buy")
    async def buy_button(self, interaction: discord.Interaction, button: Button):
        # ANTI-BOT CHECK
        if interaction.user.bot:
            await interaction.response.send_message("❌ Bots cannot buy keys.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        COST = 20
        
        try:
            # 1. Check Balance
            payload = {
                "admin_secret": ADMIN_SECRET,
                "discord_id": str(interaction.user.id)
            }
            status, data = await db_query_fallback("/pcredit/balance", payload)
            if status != 200:
                await interaction.followup.send(f"❌ Error checking balance: {data.get('error')}", ephemeral=True)
                return
                
            balance = data.get("balance", 0)
            
            if balance < COST:
                await interaction.followup.send(f"❌ You need **{COST}** credits to buy a license. You have **{balance}** credits.", ephemeral=True)
                return
                
            # 2. Deduct Credits
            payload_deduct = {
                "admin_secret": ADMIN_SECRET,
                "action": "remove",
                "discord_id": str(interaction.user.id),
                "amount": COST
            }
            status_d, data_d = await db_query_fallback("/pcredit/manage", payload_deduct)
            
            if status_d != 200:
                await interaction.followup.send(f"❌ Transaction failed: {data_d.get('error')}", ephemeral=True)
                return
                
            new_balance = data_d.get("new_balance")
            
            # 3. Generate Key
            payload_gen = {
                "admin_secret": ADMIN_SECRET,
                "amount": 1,
                "duration_hours": 0, # Lifetime
                "note": f"Purchased with {COST} PCredits by {interaction.user.name}",
            }
            
            status_g, data_g = await db_query_fallback("/generate", payload_gen)
            
            if status_g == 200:
                keys = data_g.get("keys", [])
                if keys:
                    key = keys[0]
                    
                    # Log Purchase
                    try:
                        log_embed = discord.Embed(title="🛒 Key Purchased", color=discord.Color.teal())
                        log_embed.add_field(name="👤 User", value=interaction.user.mention, inline=True)
                        log_embed.add_field(name="💰 Cost", value=f"**{COST}** Credits", inline=True)
                        log_embed.add_field(name="🔢 Remaining", value=f"**{new_balance}** Credits", inline=True)
                        log_embed.add_field(name="🔑 Key", value=f"`{key}`", inline=False)
                        log_embed.set_footer(text="Pillow Player Store", icon_url=interaction.user.display_avatar.url)
                        log_embed.timestamp = datetime.datetime.now()
                        await send_log_embed(interaction.guild, log_embed)
                    except: pass

                    # DM User
                    try:
                        embed = discord.Embed(title="🎉 Purchase Successful!", color=discord.Color.gold())
                        embed.description = f"You have redeemed **{COST}** credits for a license."
                        embed.add_field(name="Your License Key", value=f"```{key}```", inline=False)
                        embed.add_field(name="Instructions", value="Use `/claim` in the server to activate this key.", inline=False)
                        embed.set_footer(text="Thank you for your support!")
                        await interaction.user.send(embed=embed)
                        await interaction.followup.send(f"✅ **Purchase Successful!** Key sent to your DMs.\nNew Balance: **{new_balance}**", ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(f"✅ **Purchase Successful!**\n\n**Key:** `{key}`\n\n⚠️ I couldn't DM you, so here it is. Save it immediately!\nNew Balance: **{new_balance}**", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error generating key: {data_g.get('error')}", ephemeral=True)
                
        except Exception as e:
            await interaction.followup.send(f"❌ System Error: {e}", ephemeral=True)

    @discord.ui.button(label="💳 Check Balance", style=discord.ButtonStyle.primary, custom_id="redeem_balance")
    async def balance_button(self, interaction: discord.Interaction, button: Button):
        # ANTI-BOT CHECK
        if interaction.user.bot:
             await interaction.response.send_message("❌ Bots do not have credits.", ephemeral=True)
             return
             
        await interaction.response.defer(ephemeral=True)
        
        try:
            payload = {
                "admin_secret": ADMIN_SECRET,
                "discord_id": str(interaction.user.id)
            }
            status, data = await db_query_fallback("/pcredit/balance", payload)
            if status == 200:
                balance = data.get("balance", 0)
                await interaction.followup.send(f"💳 Your Balance: **{balance}** Credits", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error checking balance: {data.get('error')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ System Error: {e}", ephemeral=True)

    @discord.ui.button(label="❓ Earning Guide", style=discord.ButtonStyle.secondary, custom_id="redeem_help")
    async def help_button(self, interaction: discord.Interaction, button: Button):
        msg = (
            "**💳 Pillow Player Credit System**\n\n"
            "**How to earn credits:**\n"
            "• **Invite Friends:** Get **1 Credit** for every person you invite who joins the server.\n\n"
            "**Rewards:**\n"
            "• **20 Credits** = **1 Lifetime License Key**\n\n"
            "Click **'🛒 Redeem License'** to use your credits.\n"
            "Click **'💳 Check Balance'** to see how many credits you have."
        )
        await interaction.response.send_message(msg, ephemeral=True)

# --- SLASH COMMANDS ---

@bot.tree.command(name="blacklist", description="Manage HWID Blacklist (Admin Only)")
@app_commands.describe(action="Action to perform", hwid="Target HWID (optional if Key provided)", key="Target Key (to auto-find HWID)", reason="Reason for blacklisting (optional)")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
    app_commands.Choice(name="List", value="list")
])
async def blacklist(interaction: discord.Interaction, action: app_commands.Choice[str], hwid: str = None, key: str = None, reason: str = None):
    # DEBUG: Print to console
    print(f"DEBUG: /blacklist command received")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission to use this command.", ephemeral=True)
        return

    try:
        # Resolve HWID from Key if needed
        target_hwid = hwid
        
        if action.value in ['add', 'remove']:
            if not target_hwid and key:
                # Fetch key info to find HWID
                try:
                    status, data = await db_query_fallback("/list", {"admin_secret": ADMIN_SECRET})
                    if status == 200:
                        all_keys = data.get("keys", [])
                        found_key = next((k for k in all_keys if k['key_code'] == key), None)
                        if found_key:
                            target_hwid = found_key.get('hwid')
                            if not target_hwid:
                                await interaction.followup.send(f"❌ Key `{key}` has no HWID associated (unused?).")
                                return
                        else:
                            await interaction.followup.send(f"❌ Key `{key}` not found.")
                            return
                except Exception as e:
                     await interaction.followup.send(f"❌ Error looking up key: {e}")
                     return

            if not target_hwid:
                await interaction.followup.send(f"❌ You must provide either a `hwid` or a valid `key` for '{action.name}'.")
                return

        payload = {
            "admin_secret": ADMIN_SECRET,
            "action": action.value,
            "hwid": target_hwid,
            "reason": reason
        }
        status, data = await db_query_fallback("/blacklist/manage", payload)
        
        if status == 200:
            if action.value == 'list':
                bl_list = data.get("blacklist", [])
                if not bl_list:
                    await interaction.followup.send("📋 Blacklist is empty.")
                else:
                    embed = discord.Embed(title="🚫 HWID Blacklist", color=discord.Color.red())
                    desc = ""
                    for item in bl_list:
                        desc += f"• `{item['hwid']}`\n  Reason: {item['reason']}\n  Date: {item['created_at']}\n\n"
                    embed.description = desc
                    await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"✅ {data.get('message')}")
                
                # Log Blacklist Action
                await send_log(interaction.guild, f"🛡️ Blacklist {action.name}", f"Admin: {interaction.user.mention}\nAction: `{action.value.upper()}`\nHWID: `{target_hwid}`\nReason: {reason}", discord.Color.orange())

        else:
            await interaction.followup.send(f"❌ Server Error: {data.get('error', 'Unknown Error')}")

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to server: {e}")

@bot.tree.command(name="infocheck", description="Check detailed info about a user (Admin Only)")
@app_commands.describe(user="The user to check")
async def infocheck(interaction: discord.Interaction, user: discord.User):
    # DEBUG: Print to console
    print(f"DEBUG: /infocheck command received for user {user.id}")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "discord_id": str(user.id)
        }
        # Use fallback for offline support
        status, data = await db_query_fallback("/get_user_keys", payload)
        
        if status == 200:
            keys = data.get("keys", [])
            
            if not keys:
                await interaction.followup.send(f"ℹ️ User {user.mention} has no keys linked.")
                return

            embed = discord.Embed(
                title=f"👤 User Info: {user.name}",
                description=f"**User ID:** `{user.id}`\n**Total Keys:** {len(keys)}",
                color=discord.Color.blue()
            )
            embed.set_thumbnail(url=user.display_avatar.url)

            for i, k in enumerate(keys):
                status_emoji = "🟢" if k['status'] == 'unused' else "🔴"
                if k.get('is_banned'): status_emoji = "🚫"
                
                # Format Dates
                created = k.get('created_at', 'Unknown')
                expires = k.get('expires_at') or "Never"
                redeemed = k.get('redeemed_at') or "Not Redeemed"
                last_seen = k.get('last_seen') or "Never"
                ip_addr = k.get('ip_address') or "Unknown"
                
                # Duration
                dur = k.get('duration_hours', 0)
                dur_str = "Lifetime" if dur == 0 else f"{dur} Hours"

                details = (
                        f"**Key:** `{k['key_code']}`\n\n"
                        f"**Status:** {status_emoji} {k['status'].title()} | **Runs:** `{k.get('run_count', 0)}` | **Duration:** {dur_str}\n"
                        f"────────────────\n"
                        f"💻 **Device Info**\n"
                        f"**HWID:** `{k.get('hwid') or 'None'}`\n"
                        f"**Device:** `{k.get('device_name') or 'None'}`\n"
                        f"**IP:** `{ip_addr}`\n"
                        f"────────────────\n"
                        f"🕒 **Timestamps**\n"
                        f"**Last Execution Time:** {last_seen}\n"
                        f"**Redeemed:** {redeemed}\n"
                        f"**Expires:** {expires}\n"
                        f"**Created:** {created}\n"

                    )
                if k.get('note'):
                    details += f"\n📝 **Note:** {k['note']}\n"

                embed.add_field(name=f"🔑 License #{i+1}", value=details, inline=False)
                
            embed.set_footer(text="Pillow Player Auth System • Admin Access")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Error fetching data: {data.get('error', 'Unknown Error')}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Failed: {e}")

@bot.command(name="infocheck", aliases=["checkuser", "userinfo"])
async def infocheck_text(ctx, user: discord.User = None):
    """Check detailed info about a user (Admin Only)"""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ You do not have permission.")
        return

    if not user:
        await ctx.send("Usage: !infocheck @User")
        return

    async with ctx.typing():
        try:
            payload = {
                "admin_secret": ADMIN_SECRET,
                "discord_id": str(user.id)
            }
            status, data = await db_query_fallback("/get_user_keys", payload)
            
            if status == 200:
                keys = data.get("keys", [])
                
                if not keys:
                    await ctx.send(f"ℹ️ User {user.mention} has no keys linked.")
                    return

                embed = discord.Embed(
                    title=f"👤 User Info: {user.name}",
                    description=f"**User ID:** `{user.id}`\n**Total Keys:** {len(keys)}",
                    color=discord.Color.blue()
                )
                embed.set_thumbnail(url=user.display_avatar.url)

                for i, k in enumerate(keys):
                    status_emoji = "🟢" if k['status'] == 'unused' else "🔴"
                    if k.get('is_banned'): status_emoji = "🚫"
                    
                    # Format Dates
                    created = k.get('created_at', 'Unknown')
                    expires = k.get('expires_at') or "Never"
                    redeemed = k.get('redeemed_at') or "Not Redeemed"
                    last_seen = k.get('last_seen') or "Never"
                    ip_addr = k.get('ip_address') or "Unknown"
                    
                    # Duration
                    dur = k.get('duration_hours', 0)
                    dur_str = "Lifetime" if dur == 0 else f"{dur} Hours"

                    details = (
                        f"**Key:** `{k['key_code']}`\n\n"
                        f"**Status:** {status_emoji} {k['status'].title()} | **Runs:** `{k.get('run_count', 0)}` | **Duration:** {dur_str}\n"
                        f"────────────────\n"
                        f"💻 **Device Info**\n"
                        f"**HWID:** `{k.get('hwid') or 'None'}`\n"
                        f"**Device:** `{k.get('device_name') or 'None'}`\n"
                        f"**IP:** `{ip_addr}`\n"
                        f"────────────────\n"
                        f"🕒 **Timestamps**\n"
                        f"**Last Execution Time:** {last_seen}\n"
                        f"**Redeemed:** {redeemed}\n"
                        f"**Expires:** {expires}\n"
                        f"**Created:** {created}\n"
                    )
                    if k.get('note'):
                        details += f"\n📝 **Note:** {k['note']}\n"

                    embed.add_field(name=f"🔑 License #{i+1}", value=details, inline=False)
                
                embed.set_footer(text="Pillow Player Auth System • Admin Access")
                await ctx.send(embed=embed)
            else:
                await ctx.send(f"❌ Error fetching data: {data.get('error', 'Unknown Error')}")
                
        except Exception as e:
            await ctx.send(f"❌ Failed: {e}")


@bot.tree.command(name="grant", description="Generate and send a key to a specific user (Admin Only)")
@app_commands.describe(user="The user to grant the key to", duration="Duration in hours (0 for lifetime)", note="Optional note")
async def grant(interaction: discord.Interaction, user: discord.Member, duration: int = 0, note: str = None):
    # DEBUG: Print to console
    print(f"DEBUG: /grant command received for user {user.id}")

    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "amount": 1,
            "duration_hours": duration,
            "note": note or f"Granted to {user.name}",
            "discord_id": str(user.id)
        }
        
        status, data = await db_query_fallback("/generate", payload)
        
        if status == 200:
            keys = data.get("keys", [])
            if keys:
                key = keys[0]
                # DM the user
                try:
                    embed = discord.Embed(title="🎉 You've received a Pillow Player License!", description="Here is your license key and instructions on how to get started.", color=discord.Color.green())
                    
                    # Key Section with Code Block for easy copying
                    embed.add_field(name="🔑 Your License Key", value=f"```yaml\n{key}\n```", inline=False)
                    
                    if duration > 0:
                        embed.add_field(name="⏳ Duration", value=f"{duration} Hours", inline=True)
                    else:
                        embed.add_field(name="⏳ Duration", value="Lifetime", inline=True)

                    # Instructions Section
                    instructions = (
                        "**1. Download**\n"
                        "Download the latest version from the `#download` channel in our Discord.\n\n"
                        "**2. Install & Launch**\n"
                        "Run the installer and open Pillow Player.\n\n"
                        "**3. Activate**\n"
                        "Copy the key above and paste it into the login screen.\n\n"
                        "**Need Help?**\n"
                        "Check `#faq` or open a ticket in `#support`."
                    )
                    embed.add_field(name="📚 How to Use", value=instructions, inline=False)
                    
                    embed.set_footer(text="Thank you for using Pillow Player! • Do not share your key.")
                    
                    await user.send(embed=embed)
                    await interaction.followup.send(f"✅ Key generated and sent to {user.mention}.\nKey: `{key}`")
                except discord.Forbidden:
                    await interaction.followup.send(f"✅ Key generated, but I couldn't DM {user.mention} (DMs closed).\nKey: `{key}`")
            else:
                await interaction.followup.send("❌ Failed to generate key.")
        else:
            await interaction.followup.send(f"❌ Server Error: {data.get('error', 'Unknown Error')}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="claim", description="Link your existing license key to your Discord account")
@app_commands.describe(key="The license key to claim")
async def claim(interaction: discord.Interaction, key: str):
    # DEBUG: Print to console
    print(f"DEBUG: /claim command received for key {key}")
    
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    try:
        await _process_claim(interaction, key, interaction.user, interaction.guild)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

# --- Shared Logic for Claim ---
async def _process_claim(ctx_or_interaction, key, user, guild):
    """
    Handles claim logic for both Slash Commands (interaction) and Text Commands (ctx).
    ctx_or_interaction: Either discord.Interaction or commands.Context
    """
    is_interaction = isinstance(ctx_or_interaction, discord.Interaction)
    
    # Helper to send response
    async def send_response(msg, ephemeral=True):
        if is_interaction:
            await ctx_or_interaction.followup.send(msg, ephemeral=ephemeral)
        else:
            await ctx_or_interaction.reply(msg, mention_author=True)

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "key": key,
            "discord_id": str(user.id)
        }
        status, data = await db_query_fallback("/link_discord", payload)
        
        if status == 200:
            msg = f"✅ Success! Key `{key}` is now linked to your Discord account."
            
            # Auto-assign Role
            config = load_config()
            role_id = config.get('customer_role_id')
            if role_id:
                try:
                    role = guild.get_role(role_id)
                    if role:
                        await user.add_roles(role)
                        msg += f"\n🎉 You have been given the **{role.name}** role!"
                        
                        # Log Role Assignment
                        await send_log(guild, "🎭 Role Assigned", f"User {user.mention} claimed a key and received {role.mention}.", discord.Color.green())
                except Exception as e:
                    print(f"Failed to assign role: {e}")
                    # Don't fail the whole interaction if role fails
            
            await send_response(msg)
            
            # Log Claim
            await send_log(guild, "🔗 Key Claimed", f"User: {user.mention} (`{user.id}`)\nKey: `{key}`", discord.Color.blue())
            
        else:
            await send_response(f"❌ Failed: {data.get('error', 'Unknown Error')}")
            
    except Exception as e:
        await send_response(f"❌ Error: {e}")

@bot.command(name="claim")
async def claim_text(ctx, key: str = None):
    """(Text Command) Link your license key to your Discord account."""
    if not key:
        await ctx.send(f"❌ Please provide your key. Usage: `!claim YOUR-KEY-HERE`")
        return

    print(f"DEBUG: !claim text command received for key {key}")
    try:
        await ctx.message.delete() # Try to delete user message to protect key privacy
    except:
        pass # If missing permissions, ignore
        
    await _process_claim(ctx, key, ctx.author, ctx.guild)

@bot.tree.command(name="banuser", description="Ban all keys linked to a Discord user (Admin Only)")
@app_commands.describe(user="The user to ban", reason="Reason for ban")
async def banuser(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    # DEBUG: Print to console
    print(f"DEBUG: /banuser command received for user {user.id}")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return
    
    try:
        # 1. Get all keys
        status, data = await db_query_fallback("/list", {"admin_secret": ADMIN_SECRET})
        if status != 200:
            await interaction.followup.send("❌ Failed to fetch keys.")
            return
            
        keys = data.get("keys", [])
        target_id = str(user.id)
        
        # 2. Filter keys belonging to this user
        user_keys = [k for k in keys if str(k.get('discord_id')) == target_id]
        
        if not user_keys:
            await interaction.followup.send(f"ℹ️ No keys found linked to {user.mention}.")
            return
            
        # 3. Ban each key's HWID and Revoke Keys
        banned_count = 0
        hwids_banned = set()
        keys_to_ban = []
        
        for k in user_keys:
            keys_to_ban.append(k['key_code'])
            hwid = k.get('hwid')
            if hwid:
                # Add to blacklist
                bl_payload = {
                    "admin_secret": ADMIN_SECRET,
                    "action": "add",
                    "hwid": hwid,
                    "reason": f"Banned User {user.name} ({user.id}) - {reason or 'No reason'}"
                }
                await db_query_fallback("/blacklist/manage", bl_payload)
                hwids_banned.add(hwid)
        
        # Call server to set status='banned' for all keys
        if keys_to_ban:
            ban_payload = {
                "admin_secret": ADMIN_SECRET,
                "keys": keys_to_ban,
                "reason": reason or "Banned via Discord Command"
            }
            await db_query_fallback("/ban_key", ban_payload)
            
        await interaction.followup.send(f"🚫 Banned {user.mention}.\n• Revoked {len(keys_to_ban)} keys.\n• Blacklisted {len(hwids_banned)} Unique HWIDs.")
        
        # Log Ban
        await send_log(interaction.guild, "🚫 User Banned", f"Admin: {interaction.user.mention}\nTarget: {user.mention} (`{user.id}`)\nReason: {reason or 'No reason'}\nKeys Revoked: {len(keys_to_ban)}\nHWIDs Blacklisted: {len(hwids_banned)}", discord.Color.red())
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="lookup", description="Lookup key or user details (Admin Only)")
@app_commands.describe(query="Key or Device Name to search for")
async def lookup(interaction: discord.Interaction, query: str):
    # DEBUG: Print to console
    print(f"DEBUG: /lookup command received for query {query}")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return
    
    try:
        status, data = await db_query_fallback("/list", {"admin_secret": ADMIN_SECRET})
        if status == 200:
            keys = data.get("keys", [])
            # Search matches (Case-insensitive)
            query_lower = query.lower()
            matches = []
            for k in keys:
                k_code = k['key_code'].lower()
                k_device = (k['device_name'] or "").lower()
                k_note = (k.get('note') or "").lower()
                
                if query_lower in k_code or query_lower in k_device or query_lower in k_note:
                    matches.append(k)
            
            if not matches:
                await interaction.followup.send(f"🔍 No matches found for `{query}`.\n*(Searched Keys, Device Names, and Notes)*")
                return
                
            embed = discord.Embed(title=f"🔍 Search Results: {query}", color=discord.Color.blue())
            for k in matches[:10]: # Limit to 10 results
                info = f"**Status:** {k['status']}\n**HWID:** `{k.get('hwid') or 'None'}`\n**Device:** {k.get('device_name') or 'None'}"
                info += f"\n**Runs:** {k.get('run_count', 0)}"
                if k.get('note'):
                    info += f"\n**Note:** {k['note']}"
                embed.add_field(name=f"🔑 {k['key_code']}", value=info, inline=False)
            
            if len(matches) > 10:
                embed.set_footer(text=f"Showing 10 of {len(matches)} results.")
                
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("❌ Failed to fetch keys.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="genkey", description="Generate license keys (Admin Only)")
@app_commands.describe(amount="Number of keys to generate (default 1)", duration="Duration in hours (0 for lifetime)", note="Optional note for this batch")
async def genkey(interaction: discord.Interaction, amount: int = 1, duration: int = 0, note: str = None):
    # DEBUG: Print to console
    print(f"DEBUG: /genkey command received")

    try:
        await interaction.response.defer() # Not ephemeral -> Visible to everyone in channel
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission to use this command.", ephemeral=True)
        return

    try:
        payload = {"admin_secret": ADMIN_SECRET, "amount": amount, "duration_hours": duration, "note": note}
        status, data = await db_query_fallback("/generate", payload)
        
        if status == 200:
            keys = data.get("keys", [])
            count = data.get("count", 0)

            if count == 0:
                await interaction.followup.send("❌ No keys generated.")
                return
            
            # Log Generation
            await send_log(interaction.guild, "🔑 Keys Generated", f"Admin: {interaction.user.mention}\nAmount: `{count}`\nDuration: `{duration}h`\nNote: `{note or 'None'}`", discord.Color.gold())

            embed = discord.Embed(title="✅ Keys Generated Successfully", color=discord.Color.green())
            
            # Add Duration/Note info
            duration_text = "Lifetime" if duration == 0 else f"{duration} Hours"
            embed.add_field(name="Duration", value=duration_text, inline=True)
            if note:
                embed.add_field(name="Note", value=note, inline=True)

            if count <= 10:
                # List them in the embed
                key_text = "\n".join([f"`{k}`" for k in keys])
                embed.description = f"**Generated {count} Key(s):**\n\n{key_text}"
                await interaction.followup.send(embed=embed)
            else:
                # Send as file
                key_list_str = "\n".join(keys)
                file_obj = io.BytesIO(key_list_str.encode('utf-8'))
                discord_file = discord.File(file_obj, filename=f"generated_keys_{count}.txt")
                embed.description = f"**Generated {count} Keys.** See attached file."
                await interaction.followup.send(embed=embed, file=discord_file)

        else:
            await interaction.followup.send(f"❌ Error generating keys: {data.get('error', 'Unknown Error')}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.tree.command(name="managekeys", description="Open Key Management Dashboard (Admin Only)")
async def managekeys(interaction: discord.Interaction):
    # DEBUG: Print to console
    print(f"DEBUG: /managekeys command received")

    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission to use this command.", ephemeral=True)
        return

    try:
        # Fetch keys
        status, data = await db_query_fallback("/list", {"admin_secret": ADMIN_SECRET})
        if status == 200:
            keys = data.get("keys", [])
            user_map = await resolve_users_map(interaction, keys)
            view = KeyManagementView(keys, user_map)
            await interaction.followup.send(embed=view.main_embed, view=view)
        else:
            await interaction.followup.send(f"❌ Error fetching keys: {data.get('error', 'Unknown Error')}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.tree.command(name="keystatus", description="View key statistics (Admin Only)")
async def keystatus(interaction: discord.Interaction):
    # DEBUG: Print to console
    print(f"DEBUG: /keystatus command received")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission to use this command.", ephemeral=True)
        return

    try:
        payload = {"admin_secret": ADMIN_SECRET}
        status, data = await db_query_fallback("/stats", payload)
        
        if status == 200:
            
            # Extract Data
            total = data.get("total", 0)
            used = data.get("used", 0)
            unused = data.get("unused", 0)
            active = data.get("active", 0)
            expired = data.get("expired", 0)
            lifetime = data.get("lifetime", 0)
            limited = data.get("limited", 0)
            created_24h = data.get("created_24h", 0)
            
            embed = discord.Embed(title="📊 System Statistics", color=discord.Color.dark_theme())
            
            # Row 1: Key Inventory (Total, Unused, Used)
            embed.add_field(name="🔑 Key Inventory", value=f"**Total:** `{total}`\n**Unused:** `{unused}`\n**Used:** `{used}`", inline=True)
            
            # Row 2: Usage Health (Active vs Expired)
            health_emoji = "🟢" if active > 0 else "⚪"
            embed.add_field(name="📈 Usage Health", value=f"{health_emoji} **Active:** `{active}`\n🔴 **Expired:** `{expired}`", inline=True)
            
            # Row 3: Key Types (Lifetime vs Limited)
            embed.add_field(name="⏳ Key Types", value=f"**Lifetime:** `{lifetime}`\n**Limited:** `{limited}`", inline=True)
            
            # Row 4: Activity Summary
            embed.add_field(name="📅 Activity (24h)", value=f"**New Keys:** `+{created_24h}`", inline=False)
            
            # Row 5: Recently Redeemed
            redeemed_list = data.get("recently_redeemed", [])
            if redeemed_list:
                redeemed_text = ""
                for k in redeemed_list:
                    # Format: `KEY...` by Device (Time)
                    short_key = k['key_code'][:18] + "..." if len(k['key_code']) > 18 else k['key_code']
                    time_str = k['redeemed_at'].split('.')[0] if k.get('redeemed_at') else "Unknown"
                    redeemed_text += f"🔹 `{short_key}`\n   👤 **{k['device_name']}** at {time_str}\n"
                embed.add_field(name="📝 Recently Redeemed", value=redeemed_text, inline=False)
            else:
                 embed.add_field(name="📝 Recently Redeemed", value="No recent redemptions.", inline=False)

            # Row 6: Recently Generated
            recent_list = data.get("recent_keys", [])
            # Filter only unused ones to show "fresh" stock or just show last 3
            if recent_list:
                gen_text = ""
                for k in recent_list[:3]: # Show top 3
                     short_key = k['key_code'][:18] + "..." if len(k['key_code']) > 18 else k['key_code']
                     time_str = k['created_at'].split('.')[0]
                     gen_text += f"🆕 `{short_key}` ({time_str})\n"
                embed.add_field(name="✨ Recently Generated", value=gen_text, inline=False)
            
            # Footer
            embed.set_footer(text="Pillow Player Authentication System")
            embed.timestamp = interaction.created_at
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Error fetching stats: {data.get('error', 'Unknown Error')}")

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.command()
async def debug(ctx):
    """Simple text command to check if bot can read messages from non-admins."""
    await ctx.send(f"✅ **Hello {ctx.author.mention}!**\nI can see your messages.\nYour Permissions:\n- Administrator: {ctx.author.guild_permissions.administrator}\n- Use App Commands: {ctx.author.guild_permissions.use_application_commands}")

@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        embed = discord.Embed(title="🤖 Pillow Player Bot Help", description="Here are the available commands:", color=discord.Color.gold())
        
        # User Commands
        embed.add_field(name="👤 User Commands", value=(
            "`/claim [key]` - Link your license key.\n"
            "`!claim [key]` - (Text) Link key if slash commands fail.\n"
            "`/review [1-5] [text]` - Leave a review.\n"
            "`!review [1-5] [text]` - (Text) Leave a review.\n"
            "`!debug` - Check permission issues.\n"
        ), inline=False)

        # Admin Commands
        if interaction.user.guild_permissions.administrator:
            embed.add_field(name="🛠️ Admin Commands", value=(
                "`/panel` - Send the self-service User Dashboard.\n"
                "`/genkey [amount] [days] [type]` - Generate license keys.\n"
                "`/grant [user] [days] [type]` - Generate and DM a key to a user.\n"
                "`/managekeys` - Open the interactive Management Dashboard.\n"
                "`/lookup [query]` - Find details by Key, User ID, or Username.\n"
                "`/banuser [user]` - Ban all keys linked to a specific user.\n"
                "`/blacklist [action] [hwid]` - Manage HWID blacklist.\n"
                "`/keystatus` - View detailed system statistics.\n"
                "`/setrole [role]` - Set role to auto-assign on key claim.\n"
                "`/setlog [channel]` - Set channel for real-time Webhook logs.\n"
                "`/set_review_channel [channel]` - Set channel for reviews.\n"
                "`/setwelcome [channel]` - Set channel for new member welcome messages.\n"
                "`/pcredit [add|remove|set|balance]` - Manage PCredit system."
            ), inline=False)
        
        embed.set_footer(text="Pillow Player Authentication System")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

@bot.tree.command(name="setrole", description="Set the Customer Role to assign on key claim (Admin Only)")
@app_commands.describe(role="The role to assign")
async def setrole(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    config = load_config()
    config['customer_role_id'] = role.id
    save_config(config)
    
    await interaction.response.send_message(f"✅ Customer Role set to {role.mention}. Users will receive this role when they claim a key.")

@bot.tree.command(name="setlog", description="Set the Audit Log channel (Admin Only)")
@app_commands.describe(channel="The channel to send logs to")
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    # DEBUG: Print to console
    print(f"DEBUG: /setlog command received")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    # Create Webhook
    try:
        webhook = await channel.create_webhook(name="Pillow Logger")

        config = load_config()
        config['log_channel_id'] = channel.id
        config['webhook_url'] = webhook.url
        save_config(config)
        
        await interaction.followup.send(f"✅ Audit Log channel set to {channel.mention}. Webhook created.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to create webhook: {e}")

@bot.tree.command(name="set_review_channel", description="Set the channel where reviews will be posted (Admin Only)")
@app_commands.describe(channel="The channel to post reviews in")
async def set_review_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    try:
        await interaction.response.defer(ephemeral=True)
    except:
        pass

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    config = load_config()
    config['review_channel_id'] = channel.id
    save_config(config)
    
    await interaction.followup.send(f"✅ Review channel set to {channel.mention}")

@bot.tree.command(name="setwelcome", description="Set the Welcome channel for new members (Admin Only)")
@app_commands.describe(channel="The channel to send welcome messages to")
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel):
    # DEBUG: Print to console
    print(f"DEBUG: /setwelcome command received")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    try:
        config = load_config()
        config['welcome_channel_id'] = channel.id
        save_config(config)
        
        await interaction.followup.send(f"✅ Welcome channel set to {channel.mention}. New members will be greeted here.")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to set welcome channel: {e}")

# --- PCredit System ---
pcredit_group = app_commands.Group(name="pcredit", description="Manage PCredit System")
pcredit_group.default_permissions = discord.Permissions(send_messages=True)

@pcredit_group.command(name="balance", description="Check credit balance")
@app_commands.describe(user="The user to check (Defaults to yourself)")
async def pcredit_balance(interaction: discord.Interaction, user: discord.Member = None):
    # DEBUG: Print to console
    print(f"DEBUG: /pcredit balance command received")

    try:
        await interaction.response.defer()
    except Exception as e:
        print(f"DEBUG: Defer failed: {e}")
        return

    target_user = user or interaction.user
    
    # Check if admin if checking others
    if user and user != interaction.user and not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You can only check your own balance.", ephemeral=True)
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET, # Needed for authentication with server
            "discord_id": str(target_user.id)
        }
        
        status, data = await db_query_fallback("/pcredit/balance", payload)
        
        if status == 200:
            balance = data.get("balance", 0)
            
            embed = discord.Embed(
                title="💳 PCredit Balance",
                description=f"Balance for {target_user.mention}",
                color=discord.Color.gold()
            )
            embed.add_field(name="Current Balance", value=f"**{balance}** Credits", inline=False)
            embed.set_thumbnail(url=target_user.display_avatar.url)
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Error fetching balance: {data.get('error', 'Unknown Error')}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to server: {e}")

@pcredit_group.command(name="add", description="Add credits to a user (Admin Only)")
@app_commands.describe(user="The user to add credits to", amount="Amount to add")
async def pcredit_add(interaction: discord.Interaction, user: discord.Member, amount: int):
    print(f"DEBUG: /pcredit add command received")
    try:
        await interaction.response.defer()
    except:
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return
        
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be positive.")
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "action": "add",
            "discord_id": str(user.id),
            "amount": amount
        }
        status, data = await db_query_fallback("/pcredit/manage", payload)
        
        if status == 200:
            new_balance = data.get("new_balance")
            await interaction.followup.send(f"✅ Added **{amount}** credits to {user.mention}. New Balance: **{new_balance}**")
            
            # Log it
            try:
                embed = discord.Embed(title="💳 PCredit Added", color=discord.Color.green())
                embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="Amount", value=str(amount), inline=True)
                embed.add_field(name="New Balance", value=str(new_balance), inline=True)
                await send_log_embed(interaction.guild, embed)
            except:
                pass
        else:
            await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}")
    except Exception as e:
        await interaction.followup.send(f"❌ Connection failed: {e}")

@pcredit_group.command(name="remove", description="Remove credits from a user (Admin Only)")
@app_commands.describe(user="The user to remove credits from", amount="Amount to remove")
async def pcredit_remove(interaction: discord.Interaction, user: discord.Member, amount: int):
    print(f"DEBUG: /pcredit remove command received")
    try:
        await interaction.response.defer()
    except:
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    if amount <= 0:
        await interaction.followup.send("❌ Amount must be positive.")
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "action": "remove",
            "discord_id": str(user.id),
            "amount": amount
        }
        status, data = await db_query_fallback("/pcredit/manage", payload)
        
        if status == 200:
            new_balance = data.get("new_balance")
            await interaction.followup.send(f"✅ Removed **{amount}** credits from {user.mention}. New Balance: **{new_balance}**")
            
            try:
                embed = discord.Embed(title="💳 PCredit Removed", color=discord.Color.red())
                embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="Amount", value=str(amount), inline=True)
                embed.add_field(name="New Balance", value=str(new_balance), inline=True)
                await send_log_embed(interaction.guild, embed)
            except:
                pass
        else:
            await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}")
    except Exception as e:
        await interaction.followup.send(f"❌ Connection failed: {e}")

@pcredit_group.command(name="set", description="Set a user's credit balance (Admin Only)")
@app_commands.describe(user="The user to set credits for", amount="New balance")
async def pcredit_set(interaction: discord.Interaction, user: discord.Member, amount: int):
    print(f"DEBUG: /pcredit set command received")
    try:
        await interaction.response.defer()
    except:
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ You do not have permission.", ephemeral=True)
        return

    if amount < 0:
        await interaction.followup.send("❌ Amount cannot be negative.")
        return

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "action": "set",
            "discord_id": str(user.id),
            "amount": amount
        }
        status, data = await db_query_fallback("/pcredit/manage", payload)
        
        if status == 200:
            new_balance = data.get("new_balance")
            await interaction.followup.send(f"✅ Set credits for {user.mention} to **{new_balance}**.")
            
            try:
                embed = discord.Embed(title="💳 PCredit Set", color=discord.Color.orange())
                embed.add_field(name="Admin", value=interaction.user.mention, inline=True)
                embed.add_field(name="User", value=user.mention, inline=True)
                embed.add_field(name="New Balance", value=str(new_balance), inline=True)
                await send_log_embed(interaction.guild, embed)
            except:
                pass
        else:
            await interaction.followup.send(f"❌ Error: {data.get('error', 'Unknown Error')}")
    except Exception as e:
        await interaction.followup.send(f"❌ Connection failed: {e}")

@pcredit_group.command(name="buy", description="Redeem 20 PCredits for a License Key")
async def pcredit_buy(interaction: discord.Interaction):
    print(f"DEBUG: /pcredit buy command received")
    
    # ANTI-BOT CHECK
    if interaction.user.bot:
        await interaction.response.send_message("❌ Bots cannot buy keys.", ephemeral=True)
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except:
        return

    COST = 20
    
    # 1. Check Balance
    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "discord_id": str(interaction.user.id)
        }
        status, data = await db_query_fallback("/pcredit/balance", payload)
        if status != 200:
            await interaction.followup.send(f"❌ Error checking balance: {data.get('error')}", ephemeral=True)
            return
            
        balance = data.get("balance", 0)
        
        if balance < COST:
            await interaction.followup.send(f"❌ You need **{COST}** credits to buy a license. You have **{balance}**.", ephemeral=True)
            return
            
        # 2. Deduct Credits
        payload_deduct = {
            "admin_secret": ADMIN_SECRET,
            "action": "remove",
            "discord_id": str(interaction.user.id),
            "amount": COST
        }
        status_d, data_d = await db_query_fallback("/pcredit/manage", payload_deduct)
        
        if status_d != 200:
            await interaction.followup.send(f"❌ Transaction failed: {data_d.get('error')}", ephemeral=True)
            return
            
        new_balance = data_d.get("new_balance")
        
        # 3. Generate Key
        payload_gen = {
            "admin_secret": ADMIN_SECRET,
            "amount": 1,
            "duration_hours": 0, # Lifetime
            "note": f"Purchased with {COST} PCredits by {interaction.user.name}",
            # "discord_id": str(interaction.user.id) # Do not pre-link
        }
        
        status_g, data_g = await db_query_fallback("/generate", payload_gen)
        
        if status_g == 200:
            keys = data_g.get("keys", [])
            if keys:
                key = keys[0]
                
                # Log Purchase
                try:
                    log_embed = discord.Embed(title="🛒 Key Purchased", color=discord.Color.teal())
                    log_embed.add_field(name="👤 User", value=interaction.user.mention, inline=True)
                    log_embed.add_field(name="💰 Cost", value=f"**{COST}** Credits", inline=True)
                    log_embed.add_field(name="🔢 Remaining", value=f"**{new_balance}** Credits", inline=True)
                    log_embed.add_field(name="🔑 Key", value=f"`{key}`", inline=False)
                    log_embed.set_footer(text="Pillow Player Store", icon_url=interaction.user.display_avatar.url)
                    log_embed.timestamp = datetime.datetime.now()
                    await send_log_embed(interaction.guild, log_embed)
                except: pass

                # Try DM
                try:
                    embed = discord.Embed(title="🎉 Purchase Successful!", color=discord.Color.gold())
                    embed.description = f"You have redeemed **{COST}** credits for a license."
                    embed.add_field(name="Your License Key", value=f"```{key}```", inline=False)
                    embed.add_field(name="Instructions", value="Use `/claim` in the server to activate this key.", inline=False)
                    embed.set_footer(text="Thank you for your support!")
                    await interaction.user.send(embed=embed)
                    await interaction.followup.send(f"✅ **Purchase Successful!** I have sent the key to your DMs.\nNew Balance: **{new_balance}**", ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(f"✅ **Purchase Successful!**\n\n**Key:** `{key}`\n\n⚠️ I couldn't DM you, so here it is. Save it immediately!\nNew Balance: **{new_balance}**", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Purchase failed: {e}", ephemeral=True)

@bot.tree.command(name="postredeem", description="Post the Redeem License Panel (Admin Only)")
async def postredeem(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="💎 **Pillow Player Rewards Store**",
        description="Turn your community engagement into rewards. Invite friends to earn credits and redeem them for free licenses.",
        color=discord.Color.gold()
    )
    
    embed.add_field(
        name="💰 **Pricing**",
        value="**20 Credits** = 1 Lifetime License Key",
        inline=False
    )
    embed.add_field(
        name="📈 **Earning Strategy**",
        value="Invite your friends to this server! **1 Valid Invite = 1 Credit**",
        inline=False
    )
    embed.add_field(
        name="⚡ **Instant Delivery**",
        value="Keys are generated and sent to you immediately upon redemption.",
        inline=False
    )
    
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/2331/2331970.png")
    embed.set_footer(text="Automated Reward System • Powered by Pillow Player", icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None)
    embed.timestamp = datetime.datetime.now()
    
    await target_channel.send(embed=embed, view=RedeemSystemView())
    await interaction.response.send_message(f"✅ Redeem panel posted to {target_channel.mention}", ephemeral=True)

bot.tree.add_command(pcredit_group)

# Helper for logging embeds
async def send_log_embed(guild, embed):
    config = load_config()
    channel_id = config.get('log_channel_id')
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            embed.timestamp = datetime.datetime.now()
            await channel.send(embed=embed)

async def send_log(guild, title, description, color=discord.Color.blue()):
    config = load_config()
    channel_id = config.get('log_channel_id')
    if not channel_id:
        return
        
    channel = guild.get_channel(channel_id)
    if channel:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
        await channel.send(embed=embed)

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
