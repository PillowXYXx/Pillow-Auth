import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select, Button
import requests
import json
import io
import os
import datetime
from user_utils import resolve_users_map

# CONFIGURATION
# GET TOKEN FROM ENVIRONMENT VARIABLE (Security Best Practice)
BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
# Use localhost if running locally, or find a way to communicate if on cloud (usually localhost works if same container)
API_URL = "http://127.0.0.1:5000" 
ADMIN_SECRET = "CHANGE_THIS_TO_A_SECRET_PASSWORD" # Must match server.py
CONFIG_FILE = "bot_config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# Setup Bot
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Register persistent views here so buttons work after restart
        self.add_view(UserDashboardView())
        print("Bot setup complete. Run '!sync' in your server to enable slash commands.")

bot = MyBot()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')

@bot.tree.command(name="panel", description="Send the User Dashboard Panel (Admin Only)")
async def panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    
    embed = discord.Embed(
        title="🛡️ Pillow Player License Manager",
        description=(
            "Welcome! Use the buttons below to manage your license key.\n\n"
            "• **Claim Key:** Link your license key to your Discord account.\n"
            "• **My Subscription:** Check your key status, expiry, and HWID.\n"
            "• **Help:** Learn how to get started."
        ),
        color=discord.Color.brand_green()
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/9322/9322127.png") # Optional: Add a nice icon
    embed.set_footer(text="Secure Auth System • Powered by Pillow Player")
    
    await target_channel.send(embed=embed, view=UserDashboardView())
    await interaction.response.send_message(f"✅ Dashboard sent to {target_channel.mention}", ephemeral=True)

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
            response = requests.post(f"{API_URL}/link_discord", json=payload)
            
            if response.status_code == 200:
                await interaction.followup.send(f"✅ Success! Key `{self.key_input.value}` is now linked to your Discord account.", ephemeral=True)
            else:
                error_msg = response.json().get('error', 'Unknown Error')
                await interaction.followup.send(f"❌ Failed: {error_msg}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

class UserDashboardView(View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent view

    @discord.ui.button(label="🔗 Claim Key", style=discord.ButtonStyle.success, custom_id="dashboard_claim")
    async def claim_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ClaimKeyModal())

    @discord.ui.button(label="📊 My Subscription", style=discord.ButtonStyle.primary, custom_id="dashboard_status")
    async def status_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        try:
            payload = {
                "admin_secret": ADMIN_SECRET,
                "discord_id": str(interaction.user.id)
            }
            response = requests.post(f"{API_URL}/get_user_keys", json=payload)
            
            if response.status_code == 200:
                keys = response.json().get("keys", [])
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
                await interaction.followup.send(f"❌ Failed to fetch info: {response.text}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="dashboard_help")
    async def help_button(self, interaction: discord.Interaction, button: Button):
        msg = (
            "**How to use:**\n"
            "1. Purchase or obtain a key from the admin.\n"
            "2. Click **'🔗 Claim Key'** and paste your key to link it to your Discord account.\n"
            "3. Download the software and use the key to login.\n"
            "4. Click **'📊 My Subscription'** to check your key status and expiry."
        )
        await interaction.response.send_message(msg, ephemeral=True)

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
        try:
            response = requests.post(f"{API_URL}/reset_batch", json={"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if response.status_code == 200:
                count = len(self.keys)
                await interaction.response.send_message(f"✅ {count} keys have been reset.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.response.send_message(f"❌ Error: {response.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Recover (Unban)", style=discord.ButtonStyle.success, emoji="🚑")
    async def recover_button(self, interaction: discord.Interaction, button: Button):
        try:
            response = requests.post(f"{API_URL}/recover_key", json={"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if response.status_code == 200:
                count = len(self.keys)
                await interaction.response.send_message(f"✅ {count} keys have been recovered/unbanned.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.response.send_message(f"❌ Error: {response.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

    @discord.ui.button(label="Delete Selected", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        try:
            response = requests.post(f"{API_URL}/delete_batch", json={"admin_secret": ADMIN_SECRET, "keys": self.keys})
            if response.status_code == 200:
                count = len(self.keys)
                await interaction.response.send_message(f"🗑️ {count} keys deleted.", ephemeral=True)
                # Refresh list
                await self.main_view.refresh(interaction)
            else:
                await interaction.response.send_message(f"❌ Error: {response.text}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)

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
            response = requests.post(f"{API_URL}/list", json={"admin_secret": ADMIN_SECRET})
            if response.status_code == 200:
                new_keys = response.json().get("keys", [])
                new_user_map = await resolve_users_map(interaction, new_keys)
                new_view = KeyManagementView(new_keys, new_user_map)
                await interaction.message.edit(embed=new_view.main_embed, view=new_view)
            else:
                await interaction.followup.send("Failed to refresh list.", ephemeral=True)
        except:
             pass

# --- SLASH COMMANDS ---

@bot.tree.command(name="blacklist", description="Manage HWID Blacklist (Admin Only)")
@app_commands.describe(action="Action to perform", hwid="Target HWID (optional if Key provided)", key="Target Key (to auto-find HWID)", reason="Reason for blacklisting (optional)")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove"),
    app_commands.Choice(name="List", value="list")
])
async def blacklist(interaction: discord.Interaction, action: app_commands.Choice[str], hwid: str = None, key: str = None, reason: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        # Resolve HWID from Key if needed
        target_hwid = hwid
        
        if action.value in ['add', 'remove']:
            if not target_hwid and key:
                # Fetch key info to find HWID
                try:
                    res = requests.post(f"{API_URL}/list", json={"admin_secret": ADMIN_SECRET})
                    if res.status_code == 200:
                        all_keys = res.json().get("keys", [])
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
        response = requests.post(f"{API_URL}/blacklist/manage", json=payload)
        
        if response.status_code == 200:
            data = response.json()
            
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
            await interaction.followup.send(f"❌ Server Error: {response.text}")

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to server: {e}")

@bot.tree.command(name="grant", description="Generate and send a key to a specific user (Admin Only)")
@app_commands.describe(user="The user to grant the key to", duration="Duration in hours (0 for lifetime)", note="Optional note")
async def grant(interaction: discord.Interaction, user: discord.Member, duration: int = 0, note: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "amount": 1,
            "duration_hours": duration,
            "note": note or f"Granted to {user.name}",
            "discord_id": str(user.id)
        }
        
        response = requests.post(f"{API_URL}/generate", json=payload)
        
        if response.status_code == 200:
            keys = response.json().get("keys", [])
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
            await interaction.followup.send(f"❌ Server Error: {response.text}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="claim", description="Link your existing license key to your Discord account")
@app_commands.describe(key="The license key to claim")
async def claim(interaction: discord.Interaction, key: str):
    await interaction.response.defer(ephemeral=True)
    
    try:
        payload = {
            "admin_secret": ADMIN_SECRET,
            "key": key,
            "discord_id": str(interaction.user.id)
        }
        response = requests.post(f"{API_URL}/link_discord", json=payload)
        
        if response.status_code == 200:
            msg = f"✅ Success! Key `{key}` is now linked to your Discord account."
            
            # Auto-assign Role
            config = load_config()
            role_id = config.get('customer_role_id')
            if role_id:
                try:
                    role = interaction.guild.get_role(role_id)
                    if role:
                        await interaction.user.add_roles(role)
                        msg += f"\n🎉 You have been given the **{role.name}** role!"
                        
                        # Log Role Assignment
                        await send_log(interaction.guild, "🎭 Role Assigned", f"User {interaction.user.mention} claimed a key and received {role.mention}.", discord.Color.green())
                except Exception as e:
                    print(f"Failed to assign role: {e}")
                    # Don't fail the whole interaction if role fails
            
            await interaction.followup.send(msg)
            
            # Log Claim
            await send_log(interaction.guild, "🔗 Key Claimed", f"User: {interaction.user.mention} (`{interaction.user.id}`)\nKey: `{key}`", discord.Color.blue())
            
        else:
            await interaction.followup.send(f"❌ Failed: {response.json().get('error', 'Unknown Error')}")
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="banuser", description="Ban all keys linked to a Discord user (Admin Only)")
@app_commands.describe(user="The user to ban", reason="Reason for ban")
async def banuser(interaction: discord.Interaction, user: discord.Member, reason: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer()
    
    try:
        # 1. Get all keys
        res = requests.post(f"{API_URL}/list", json={"admin_secret": ADMIN_SECRET})
        if res.status_code != 200:
            await interaction.followup.send("❌ Failed to fetch keys.")
            return
            
        keys = res.json().get("keys", [])
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
                requests.post(f"{API_URL}/blacklist/manage", json=bl_payload)
                hwids_banned.add(hwid)
        
        # Call server to set status='banned' for all keys
        if keys_to_ban:
            ban_payload = {
                "admin_secret": ADMIN_SECRET,
                "keys": keys_to_ban,
                "reason": reason or "Banned via Discord Command"
            }
            requests.post(f"{API_URL}/ban_key", json=ban_payload)
            
        await interaction.followup.send(f"🚫 Banned {user.mention}.\n• Revoked {len(keys_to_ban)} keys.\n• Blacklisted {len(hwids_banned)} Unique HWIDs.")
        
        # Log Ban
        await send_log(interaction.guild, "🚫 User Banned", f"Admin: {interaction.user.mention}\nTarget: {user.mention} (`{user.id}`)\nReason: {reason or 'No reason'}\nKeys Revoked: {len(keys_to_ban)}\nHWIDs Blacklisted: {len(hwids_banned)}", discord.Color.red())
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.tree.command(name="lookup", description="Lookup key or user details (Admin Only)")
@app_commands.describe(query="Key or Device Name to search for")
async def lookup(interaction: discord.Interaction, query: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    await interaction.response.defer()
    
    try:
        response = requests.post(f"{API_URL}/list", json={"admin_secret": ADMIN_SECRET})
        if response.status_code == 200:
            keys = response.json().get("keys", [])
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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer() # Not ephemeral -> Visible to everyone in channel

    try:
        payload = {"admin_secret": ADMIN_SECRET, "amount": amount, "duration_hours": duration, "note": note}
        response = requests.post(f"{API_URL}/generate", json=payload)
        
        if response.status_code == 200:
            data = response.json()
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
            await interaction.followup.send(f"❌ Error generating keys: {response.text}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.tree.command(name="managekeys", description="Open Key Management Dashboard (Admin Only)")
async def managekeys(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        # Fetch keys
        response = requests.post(f"{API_URL}/list", json={"admin_secret": ADMIN_SECRET})
        if response.status_code == 200:
            keys = response.json().get("keys", [])
            user_map = await resolve_users_map(interaction, keys)
            view = KeyManagementView(keys, user_map)
            await interaction.followup.send(embed=view.main_embed, view=view)
        else:
            await interaction.followup.send(f"❌ Error fetching keys: {response.text}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.tree.command(name="keystatus", description="View key statistics (Admin Only)")
async def keystatus(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        payload = {"admin_secret": ADMIN_SECRET}
        response = requests.post(f"{API_URL}/stats", json=payload)
        
        if response.status_code == 200:
            data = response.json()
            
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
            await interaction.followup.send(f"❌ Error fetching stats: {response.text}")

    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect to key server: {e}")

@bot.tree.command(name="help", description="Show all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Pillow Player Bot Help", description="Here are the available commands:", color=discord.Color.gold())
    
    # User Commands
    embed.add_field(name="� User Commands", value=(
        "`/claim [key]` - Link your license key to your Discord account.\n"
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
            "`/setlog [channel]` - Set channel for real-time Webhook logs."
        ), inline=False)
    
    embed.set_footer(text="Pillow Player Authentication System")
    await interaction.response.send_message(embed=embed)

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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You do not have permission.", ephemeral=True)
        return

    # Create Webhook
    webhook = await channel.create_webhook(name="Pillow Logger")

    config = load_config()
    config['log_channel_id'] = channel.id
    config['webhook_url'] = webhook.url
    save_config(config)
    
    await interaction.response.send_message(f"✅ Audit Log channel set to {channel.mention}. Webhook created.")

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
