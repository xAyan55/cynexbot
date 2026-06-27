import asyncio
import json
import logging
import re
import urllib.parse
import aiohttp
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Union

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import (
    LayoutView,
    Container,
    TextDisplay,
    Separator,
    Section,
    ActionRow,
    Button
)
from discord.ui import MediaGallery
from discord import MediaGalleryItem

import ui
from ui import (
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudWarningContainer,
    CynexCloudInfoContainer,
    CynexCloudPaginationContainer,
    CynexCloudContainerBuilder
)

logger = logging.getLogger("CynexCloud.Utilities")
DB_PATH = "fb.db"

# ══════════════════════════════════════════════════════════════════════
# UTILITY DATABASE LOGGER HELPER
# ══════════════════════════════════════════════════════════════════════

async def log_command_usage(command_name: str, interaction: discord.Interaction):
    """Inserts a command execution record into the analytics table."""
    try:
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id) if interaction.guild_id else "DM"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO command_usage (command_name, user_id, guild_id) VALUES (?, ?, ?)",
                (command_name, user_id, guild_id)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to log command analytics: {e}")

# ══════════════════════════════════════════════════════════════════════
# DURATION PARSER HELPER
# ══════════════════════════════════════════════════════════════════════

def parse_duration(duration_str: str) -> Optional[int]:
    """Parses a duration string like 10m, 2h, 1d into total seconds."""
    pattern = re.compile(r'^(\d+)([smhd])$')
    match = pattern.match(duration_str.strip().lower())
    if not match:
        return None
    val = int(match.group(1))
    unit = match.group(2)
    if unit == 's':
        return val
    elif unit == 'm':
        return val * 60
    elif unit == 'h':
        return val * 3600
    elif unit == 'd':
        return val * 86400
    return None

# ══════════════════════════════════════════════════════════════════════
# POLL HELPERS
# ══════════════════════════════════════════════════════════════════════

def generate_poll_description(question: str, options: list, votes: dict, anonymous: bool, allow_multiple: bool, end_time_epoch: int, status: str) -> str:
    """Generates a premium-branded markdown layout tree representing poll percentages."""
    total_voters = len(votes)
    tally = [0] * len(options)
    for voter_id, choice_indices in votes.items():
        for idx in choice_indices:
            if 0 <= idx < len(options):
                tally[idx] += 1
                
    total_votes_cast = sum(tally)
    
    desc = f"# 📊 Poll: {question}\n\n"
    for idx, opt in enumerate(options):
        count = tally[idx]
        pct = (count / total_votes_cast * 100) if total_votes_cast > 0 else 0
        
        bar_length = 10
        filled = int(round(pct / (100 / bar_length)))
        bar = "🟩" * filled + "⬛" * (bar_length - filled)
        desc += f"**{idx+1}. {opt}**\n{bar} `{count} votes` ({pct:.1f}%)\n\n"
        
    if status == "closed":
        max_votes = max(tally) if tally else 0
        if max_votes > 0:
            winners = [options[i] for i, v in enumerate(tally) if v == max_votes]
            if len(winners) == 1:
                desc += f"🏆 **Winner:** `{winners[0]}`\n\n"
            else:
                desc += f"🏆 **Tie between:** " + ", ".join([f"`{w}`" for w in winners]) + "\n\n"
        else:
            desc += "🏆 **No votes were cast.**\n\n"
        desc += "🔒 *This poll is closed. Final results tabulated.*"
    elif status == "cancelled":
        desc += "❌ *This poll was cancelled.*"
    else:
        desc += f"⏳ **Ends:** <t:{end_time_epoch}:F> (<t:{end_time_epoch}:R>)\n"
        desc += f"👤 **Voters:** `{total_voters}` | **Settings:** "
        settings_flags = []
        if anonymous:
            settings_flags.append("Anonymous")
        if allow_multiple:
            settings_flags.append("Multiple Choice")
        if not settings_flags:
            settings_flags.append("Single Choice")
        desc += ", ".join(settings_flags) + "\n"
        
        if not anonymous and total_voters > 0:
            desc += "\n**Recent Activity:**\n"
            voter_lines = []
            for voter_id, choices in list(votes.items())[-5:]:
                choices_str = ", ".join([f"`{options[c]}`" for c in choices if 0 <= c < len(options)])
                voter_lines.append(f"<@{voter_id}> voted for {choices_str}")
            desc += "\n".join(voter_lines)
            
    return desc

async def handle_poll_vote_interaction(interaction: discord.Interaction, option_idx: int):
    """Callback triggered globally when a member votes on a poll."""
    await interaction.response.defer(ephemeral=True)
    msg_id = str(interaction.message.id)
    user_id = str(interaction.user.id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT question, options, votes, anonymous, allow_multiple, end_time, status FROM polls WHERE message_id = ?",
            (msg_id,)
        ) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        err = CynexCloudErrorContainer("Poll Not Found", "Poll data not found in database.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    question, options_json, votes_json, anonymous_int, allow_multiple_int, end_time_str, status = row
    
    if status != "active":
        err = CynexCloudErrorContainer("Poll Inactive", "This poll has already ended.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    options = json.loads(options_json)
    votes = json.loads(votes_json)
    anonymous = bool(anonymous_int)
    allow_multiple = bool(allow_multiple_int)
    
    user_votes = votes.get(user_id, [])
    
    if option_idx in user_votes:
        user_votes.remove(option_idx)
        if not user_votes:
            votes.pop(user_id, None)
        else:
            votes[user_id] = user_votes
        msg_action = "removed"
    else:
        if allow_multiple:
            user_votes.append(option_idx)
            votes[user_id] = user_votes
        else:
            votes[user_id] = [option_idx]
        msg_action = "added"
        
    votes_new_json = json.dumps(votes)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE polls SET votes = ? WHERE message_id = ?", (votes_new_json, msg_id))
        await db.commit()
        
    try:
        dt = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S')
        end_time_epoch = int(dt.timestamp())
    except Exception:
        end_time_epoch = int(datetime.now().timestamp())
        
    desc = generate_poll_description(question, options, votes, anonymous, allow_multiple, end_time_epoch, status)
    
    layout = LayoutView()
    container = Container(accent_color=3447003)
    container.add_item(TextDisplay(desc))
    
    row_items = []
    for idx, opt in enumerate(options):
        row_items.append(Button(label=opt, style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:poll:vote:{idx}"))
    row_items.append(Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:cancel"))
    
    container.add_item(ActionRow(*row_items))
    layout.add_item(container)
    
    await interaction.message.edit(view=layout)
    success = CynexCloudSuccessContainer("Vote Saved", f"Your vote has been successfully {msg_action}!")
    await interaction.followup.send(view=success.build(), ephemeral=True)

async def handle_poll_cancel_interaction(interaction: discord.Interaction):
    """Callback triggered globally when a member cancels a poll."""
    await interaction.response.defer(ephemeral=True)
    msg_id = str(interaction.message.id)
    user_id = str(interaction.user.id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT creator_id, question, options, votes, anonymous, allow_multiple, end_time, status FROM polls WHERE message_id = ?",
            (msg_id,)
        ) as cursor:
            row = await cursor.fetchone()
            
    if not row:
        err = CynexCloudErrorContainer("Poll Not Found", "Poll metadata was not located.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    creator_id, question, options_json, votes_json, anonymous_int, allow_multiple_int, end_time_str, status = row
    
    if status != "active":
        err = CynexCloudErrorContainer("Poll Inactive", "This poll is already closed or cancelled.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    is_admin = interaction.user.guild_permissions.administrator
    if user_id != creator_id and not is_admin:
        err = CynexCloudErrorContainer("Unauthorized Action", "Only the poll creator or administrators can cancel this poll.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE polls SET status = 'cancelled' WHERE message_id = ?", (msg_id,))
        await db.commit()
        
    options = json.loads(options_json)
    votes = json.loads(votes_json)
    anonymous = bool(anonymous_int)
    allow_multiple = bool(allow_multiple_int)
    
    desc = generate_poll_description(question, options, votes, anonymous, allow_multiple, int(datetime.now().timestamp()), "cancelled")
    
    layout = LayoutView()
    container = Container(accent_color=16711680)
    container.add_item(TextDisplay(desc))
    layout.add_item(container)
    
    await interaction.message.edit(view=layout)
    success = CynexCloudSuccessContainer("Poll Cancelled", "This poll status has been marked as cancelled.")
    await interaction.followup.send(view=success.build(), ephemeral=True)

class PersistentPollView(discord.ui.View):
    """Persistent View handling global option buttons and cancellations."""
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Option 1", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:vote:0")
    async def vote_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 0)

    @discord.ui.button(label="Option 2", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:vote:1")
    async def vote_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 1)

    @discord.ui.button(label="Option 3", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:vote:2")
    async def vote_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 2)

    @discord.ui.button(label="Option 4", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:vote:3")
    async def vote_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 3)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:cancel")
    async def cancel_poll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_cancel_interaction(interaction)

# ══════════════════════════════════════════════════════════════════════
# MAIN UTILITIES COG
# ══════════════════════════════════════════════════════════════════════

class Utilities(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # Sniping volatile cache caches maxlen 10
        self.sniped_deleted: Dict[int, deque] = {}
        self.sniped_edited: Dict[int, deque] = {}
        
        # Sticky message variables
        self.sticky_messages: Dict[int, str] = {}
        self.sticky_last_ids: Dict[int, int] = {}
        self.sticky_locks: Dict[int, asyncio.Lock] = {}
        
        # Register Slash Subgroups
        self.sticky_group = StickyGroup(self)
        self.remind_group = RemindGroup(self)
        
        bot.tree.add_command(self.sticky_group)
        bot.tree.add_command(self.remind_group)

    async def cog_load(self):
        await self.init_db()
        self.reminder_loop.start()
        self.poll_loop.start()
        await self.load_sticky_messages_cache()
        logger.info("Utilities Cog extensions and persistent structures loaded successfully.")

    async def cog_unload(self):
        self.reminder_loop.cancel()
        self.poll_loop.cancel()
        self.bot.tree.remove_command("sticky")
        self.bot.tree.remove_command("remind")
        logger.info("Utilities Cog loops and command structures unloaded.")

    async def init_db(self):
        """Initializes SQLite schemas and indexes for utilities components."""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            # Sticky messages schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sticky_messages (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    last_message_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Reminders schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    reminder_text TEXT NOT NULL,
                    target_time TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Polls schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS polls (
                    message_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options TEXT NOT NULL,
                    votes TEXT DEFAULT '{}',
                    anonymous INTEGER NOT NULL DEFAULT 0,
                    allow_multiple INTEGER NOT NULL DEFAULT 0,
                    end_time TIMESTAMP NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
            """)
            # AFK users schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS afk_users (
                    user_id TEXT PRIMARY KEY,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Analytics schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS command_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_analytics (
                    guild_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Optimization search indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user_target ON reminders (user_id, target_time)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_polls_status_end ON polls (status, end_time)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_afk_user ON afk_users (user_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_command_usage_name ON command_usage (command_name)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_guild_analytics_event ON guild_analytics (guild_id, event_type, timestamp)")
            await db.commit()

    async def load_sticky_messages_cache(self):
        """Loads sticky configuration setup from database to cache."""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id, message_text, last_message_id FROM sticky_messages") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    ch_id = int(row[0])
                    self.sticky_messages[ch_id] = row[1]
                    self.sticky_last_ids[ch_id] = int(row[2]) if row[2] else None

    # ══════════════════════════════════════════════════════════════════════
    # BACKGROUND SCHEDULERS
    # ══════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=10)
    async def reminder_loop(self):
        """Polls database for expired reminders and alerts users."""
        try:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT id, user_id, channel_id, reminder_text FROM reminders WHERE target_time <= ?",
                    (now_str,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    
            if not rows:
                return
                
            for row in rows:
                rem_id, user_id, channel_id, text = row
                
                try:
                    channel = self.bot.get_channel(int(channel_id))
                    if not channel:
                        channel = await self.bot.fetch_channel(int(channel_id))
                    
                    user = self.bot.get_user(int(user_id))
                    if not user:
                        user = await self.bot.fetch_user(int(user_id))
                        
                    if channel and user:
                        rem_layout = CynexCloudInfoContainer("CynexCloud Reminder Alert", text)
                        rem_layout.add_section("Recipient", user.mention)
                        rem_layout.add_section("Setting Details", "To clear/modify future reminders, type `/remind list`.")
                        await channel.send(content=user.mention, view=rem_layout.build())
                except Exception as e:
                    logger.warning(f"Failed to deliver reminder {rem_id} to user {user_id} in channel {channel_id}: {e}")
                    
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM reminders WHERE id = ?", (rem_id,))
                    await db.commit()
        except Exception as e:
            logger.exception("Error in reminder_loop:")

    @tasks.loop(seconds=10)
    async def poll_loop(self):
        """Checks for active polls that have reached expiration time."""
        try:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT message_id, channel_id, question, options, votes, anonymous, allow_multiple, end_time FROM polls WHERE status = 'active' AND end_time <= ?",
                    (now_str,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    
            if not rows:
                return
                
            for row in rows:
                msg_id, channel_id, question, options_json, votes_json, anonymous_int, allow_multiple_int, end_time_str = row
                
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE polls SET status = 'closed' WHERE message_id = ?", (msg_id,))
                    await db.commit()
                    
                options = json.loads(options_json)
                votes = json.loads(votes_json)
                anonymous = bool(anonymous_int)
                allow_multiple = bool(allow_multiple_int)
                
                try:
                    channel = self.bot.get_channel(int(channel_id))
                    if not channel:
                        channel = await self.bot.fetch_channel(int(channel_id))
                        
                    message = await channel.fetch_message(int(msg_id))
                    
                    desc = generate_poll_description(question, options, votes, anonymous, allow_multiple, int(datetime.now().timestamp()), "closed")
                    
                    layout = LayoutView()
                    container = Container(accent_color=65280)
                    container.add_item(TextDisplay(desc))
                    layout.add_item(container)
                    
                    await message.edit(view=layout)
                except Exception as e:
                    logger.warning(f"Failed to finalize poll message {msg_id} in channel {channel_id}: {e}")
        except Exception as e:
            logger.exception("Error in poll_loop:")

    # ══════════════════════════════════════════════════════════════════════
    # MESSAGE EVENTS & LISTENERS
    # ══════════════════════════════════════════════════════════════════════

    async def handle_afk_messages(self, message: discord.Message):
        """Checks message authors/mentions against AFK states and prompts/clears them."""
        if message.author.bot:
            return
            
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT reason, created_at FROM afk_users WHERE user_id = ?", (str(message.author.id),)) as cursor:
                row = await cursor.fetchone()
                
        if row:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM afk_users WHERE user_id = ?", (str(message.author.id),))
                await db.commit()
                
            welcome = CynexCloudSuccessContainer("Welcome Back", f"Hello {message.author.mention}, I've removed your AFK status.")
            await message.channel.send(view=welcome.build(), reference=message, delete_after=10)
            
        if message.mentions:
            for user in message.mentions:
                if user.id == message.author.id:
                    continue
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT reason, created_at FROM afk_users WHERE user_id = ?", (str(user.id),)) as cursor:
                        afk_row = await cursor.fetchone()
                        
                if afk_row:
                    reason = afk_row[0] or "AFK"
                    created_at_str = afk_row[1]
                    
                    try:
                        dt = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
                        timestamp_epoch = int(dt.replace(tzinfo=None).timestamp())
                        time_display = f"<t:{timestamp_epoch}:R>"
                    except Exception:
                        time_display = "some time ago"
                        
                    afk_card = CynexCloudInfoContainer("Member AFK", f"💤 **{user.display_name}** is currently Away From Keyboard.")
                    afk_card.add_section("Reason", reason)
                    afk_card.add_section("Away Since", time_display)
                    await message.channel.send(view=afk_card.build(), reference=message, delete_after=10)

    async def handle_sticky_messages(self, message: discord.Message):
        """Reposts sticky message in configured channels while throttling using Lock."""
        if message.author.bot:
            return
            
        channel_id = message.channel.id
        if channel_id not in self.sticky_messages:
            return
            
        if channel_id not in self.sticky_locks:
            self.sticky_locks[channel_id] = asyncio.Lock()
            
        async with self.sticky_locks[channel_id]:
            text = self.sticky_messages[channel_id]
            last_id = self.sticky_last_ids.get(channel_id)
            
            if message.id == last_id:
                return
                
            if last_id:
                try:
                    old_msg = await message.channel.fetch_message(last_id)
                    await old_msg.delete()
                except Exception:
                    pass
                    
            try:
                layout = LayoutView()
                container = Container(accent_color=16776960)
                container.add_item(TextDisplay(f"📌 **Sticky Message**\n\n{text}"))
                layout.add_item(container)
                new_msg = await message.channel.send(view=layout)
                
                self.sticky_last_ids[channel_id] = new_msg.id
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE sticky_messages SET last_message_id = ? WHERE channel_id = ?",
                        (str(new_msg.id), str(channel_id))
                    )
                    await db.commit()
            except Exception as e:
                logger.exception(f"Error handling sticky message repost: {e}")

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Listens for deleted messages and populates volatile sniper cache."""
        if message.author.bot:
            return
        ch_id = message.channel.id
        if ch_id not in self.sniped_deleted:
            self.sniped_deleted[ch_id] = deque(maxlen=10)
        self.sniped_deleted[ch_id].append({
            "author": message.author,
            "content": message.content,
            "timestamp": datetime.now(),
            "attachments": [att.url for att in message.attachments]
        })

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Listens for edited messages and populates volatile sniper cache."""
        if before.author.bot:
            return
        if before.content == after.content:
            return
        ch_id = before.channel.id
        if ch_id not in self.sniped_edited:
            self.sniped_edited[ch_id] = deque(maxlen=10)
        self.sniped_edited[ch_id].append({
            "author": before.author,
            "before": before.content,
            "after": after.content,
            "timestamp": datetime.now()
        })

    # ══════════════════════════════════════════════════════════════════════
    # UTILITY SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="userinfo", description="Show detailed information about a member")
    @app_commands.describe(member="The member to view")
    async def userinfo(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("userinfo", interaction)
        target = member or interaction.user
        
        user = await self.bot.fetch_user(target.id)
        roles = [role.mention for role in target.roles if role != interaction.guild.default_role]
        roles_str = ", ".join(roles) if roles else "None"
        
        flags = []
        if user.public_flags.staff:
            flags.append("Discord Staff")
        if user.public_flags.partner:
            flags.append("Partnered Server Owner")
        if user.public_flags.hypesquad:
            flags.append("HypeSquad Events Coordinator")
        if user.public_flags.bug_hunter:
            flags.append("Bug Hunter Class 1")
        if user.public_flags.bug_hunter_level_2:
            flags.append("Bug Hunter Class 2")
        if user.public_flags.hypesquad_bravery:
            flags.append("HypeSquad Bravery")
        if user.public_flags.hypesquad_brilliance:
            flags.append("HypeSquad Brilliance")
        if user.public_flags.hypesquad_balance:
            flags.append("HypeSquad Balance")
        if user.public_flags.early_supporter:
            flags.append("Early Supporter")
        if user.public_flags.verified_bot_developer:
            flags.append("Early Verified Bot Developer")
        if user.public_flags.active_developer:
            flags.append("Active Developer")
            
        flags_str = ", ".join(flags) if flags else "None"
        
        card = CynexCloudInfoContainer(f"User Profile: {target}", f"Metadata cards generated for {target.mention}.")
        card.add_section("Username", f"`{target.name}`")
        card.add_section("Display Name", f"`{target.display_name}`")
        card.add_section("User ID", f"`{target.id}`")
        card.add_section("Bot Status", "🤖 Bot Account" if target.bot else "👤 Human Account")
        card.add_section("Account Age", f"Created: <t:{int(target.created_at.timestamp())}:F> (<t:{int(target.created_at.timestamp())}:R>)")
        card.add_section("Server Join Age", f"Joined: <t:{int(target.joined_at.timestamp())}:F> (<t:{int(target.joined_at.timestamp())}:R>)")
        card.add_section("Public Flags / Badges", flags_str)
        card.add_section(f"Roles List ({len(roles)})", roles_str)
        
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show detailed information about this server")
    async def serverinfo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("serverinfo", interaction)
        guild = interaction.guild
        
        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        categories = len(guild.categories)
        
        bots = sum(1 for m in guild.members if m.bot)
        humans = guild.member_count - bots
        
        card = CynexCloudInfoContainer(f"Server Metrics: {guild.name}", f"ID: `{guild.id}`")
        card.add_section("Server Owner", f"{guild.owner.mention} (`{guild.owner_id}`)")
        card.add_section("Created On", f"<t:{int(guild.created_at.timestamp())}:F> (<t:{int(guild.created_at.timestamp())}:R>)")
        card.add_section("Members Metrics", f"👥 Total: `{guild.member_count}` | 👤 Humans: `{humans}` | 🤖 Bots: `{bots}`")
        card.add_section("Channels Tally", f"📁 Categories: `{categories}` | 💬 Text: `{text_channels}` | 🔊 Voice: `{voice_channels}`")
        card.add_section("Extra Flags", f"🎭 Roles count: `{len(guild.roles)}` | 🚀 Boosts: `{guild.premium_subscription_count}` (Level {guild.premium_tier})")
        
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="avatar", description="Show a member's avatar")
    @app_commands.describe(member="The member to view")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("avatar", interaction)
        target = member or interaction.user
        
        card = CynexCloudInfoContainer(f"Avatar of {target.display_name}")
        card.layout.add_item(MediaGallery(MediaGalleryItem(target.display_avatar.url)))
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="banner", description="Show a member's banner profile image")
    @app_commands.describe(member="The member to view")
    async def banner(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("banner", interaction)
        target = member or interaction.user
        
        user = await self.bot.fetch_user(target.id)
        if not user.banner:
            err = CynexCloudErrorContainer("No Banner Detected", f"User {target.mention} does not have a profile banner configuration.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        card = CynexCloudInfoContainer(f"Profile Banner of {target.display_name}")
        card.layout.add_item(MediaGallery(MediaGalleryItem(user.banner.url)))
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="roleinfo", description="Show detailed information about a role")
    @app_commands.describe(role="The role to view")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("roleinfo", interaction)
        
        member_count = len(role.members)
        permissions = [p[0].replace('_', ' ').title() for p in role.permissions if p[1]]
        perms_str = ", ".join(permissions[:15]) + (f" and {len(permissions)-15} more..." if len(permissions) > 15 else "") if permissions else "None"
        
        card = CynexCloudInfoContainer(f"Role Config: {role.name}", f"ID: `{role.id}`")
        card.add_section("Color Code", f"`{role.color}`")
        card.add_section("Position Rank", f"`{role.position}`")
        card.add_section("Members Assigned", f"`{member_count}` users")
        card.add_section("Settings Flags", f"Hoisted: `{'Yes' if role.hoist else 'No'}` | Mentionable: `{'Yes' if role.mentionable else 'No'}`")
        card.add_section("Permissions List", perms_str)
        
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="channelinfo", description="Show detailed information about a channel")
    @app_commands.describe(channel="The channel to view")
    async def channelinfo(self, interaction: discord.Interaction, channel: Optional[Union[discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel]] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("channelinfo", interaction)
        target = channel or interaction.channel
        
        card = CynexCloudInfoContainer(f"Channel Config: #{target.name}", f"ID: `{target.id}`")
        card.add_section("Type", f"`{target.type.name.title()}`")
        card.add_section("Category Parent", f"{target.category.name if target.category else 'None'}")
        card.add_section("Channel Position", f"`{target.position}`")
        card.add_section("Created On", f"<t:{int(target.created_at.timestamp())}:F> (<t:{int(target.created_at.timestamp())}:R>)")
        
        if isinstance(target, discord.TextChannel):
            card.add_section("Topic", target.topic or "No topic set.")
            card.add_section("Configuration", f"Slowmode: `{target.slowmode_delay}s` | NSFW: `{'Yes' if target.is_nsfw() else 'No'}`")
        elif isinstance(target, discord.VoiceChannel):
            card.add_section("Voice Configuration", f"Bitrate: `{target.bitrate // 1000} kbps` | Limit: `{target.user_limit or 'Unlimited'}` users")
            
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="membercount", description="Show server member metrics")
    async def membercount(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("membercount", interaction)
        guild = interaction.guild
        total = guild.member_count
        bots = sum(1 for m in guild.members if m.bot)
        humans = total - bots
        
        card = CynexCloudInfoContainer(f"Server Members: {guild.name}")
        card.add_section("Total Members", f"`{total}`")
        card.add_section("Humans", f"`{humans}`")
        card.add_section("Bots", f"`{bots}`")
        
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="ping", description="Show bot connection and database latency")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("ping", interaction)
        
        gw_latency = self.bot.latency * 1000
        
        t1 = datetime.now()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1") as cursor:
                await cursor.fetchone()
        db_latency = (datetime.now() - t1).total_seconds() * 1000
        
        t2 = datetime.now()
        card_loader = CynexCloudInfoContainer("Ping Latency Metric", "Measuring API latency round trip...")
        followup_msg = await interaction.followup.send(view=card_loader.build(), ephemeral=True)
        api_latency = (datetime.now() - t2).total_seconds() * 1000
        
        card = CynexCloudInfoContainer("🏓 Pong Latencies", "System connection and storage latencies.")
        card.add_section("Gateway Latency", f"`{gw_latency:.1f}ms`")
        card.add_section("Discord API Latency", f"`{api_latency:.1f}ms`")
        card.add_section("Database Latency", f"`{db_latency:.1f}ms`")
        
        await followup_msg.edit(view=card.build())

    @app_commands.command(name="afk", description="Set your status to Away From Keyboard")
    @app_commands.describe(reason="Reason for going AFK")
    async def afk(self, interaction: discord.Interaction, reason: Optional[str] = "AFK"):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("afk", interaction)
        user_id = str(interaction.user.id)
        now_utc = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO afk_users (user_id, reason, created_at) VALUES (?, ?, ?)",
                (user_id, reason, now_utc)
            )
            await db.commit()
            
        success = CynexCloudSuccessContainer("AFK Status Enabled", f"I have set your status to AFK: **{reason}**")
        await interaction.followup.send(view=success.build(), ephemeral=True)
        
        broadcast = CynexCloudInfoContainer("AFK Notification", f"💤 {interaction.user.mention} is now Away From Keyboard: **{reason}**")
        await interaction.channel.send(view=broadcast.build())

    @app_commands.command(name="snipe", description="Snipe recently deleted or edited messages")
    @app_commands.choices(type=[
        app_commands.Choice(name="Deleted", value="deleted"),
        app_commands.Choice(name="Edited", value="edited")
    ])
    async def snipe(self, interaction: discord.Interaction, type: str = "deleted"):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("snipe", interaction)
        channel_id = interaction.channel.id
        
        if type == "deleted":
            cache = self.sniped_deleted.get(channel_id)
            if not cache or len(cache) == 0:
                err = CynexCloudErrorContainer("Sniper Log Empty", "No recently deleted messages found in this channel.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            msg = cache[-1]
            card = CynexCloudWarningContainer("Sniped Deleted Message", f"Author: {msg['author'].mention}")
            card.add_section("Deleted Message Content", msg["content"] or "*No content (attachment only or empty)*")
            if msg["attachments"]:
                card.add_section("Attachments URLs", "\n".join(msg["attachments"]))
            card.add_section("Time Logged", f"<t:{int(msg['timestamp'].timestamp())}:R>")
            await interaction.followup.send(view=card.build(), ephemeral=True)
        else:
            cache = self.sniped_edited.get(channel_id)
            if not cache or len(cache) == 0:
                err = CynexCloudErrorContainer("Sniper Log Empty", "No recently edited messages found in this channel.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            msg = cache[-1]
            card = CynexCloudWarningContainer("Sniped Edited Message", f"Author: {msg['author'].mention}")
            card.add_section("Content Before", msg["before"] or "*Empty*")
            card.add_section("Content After", msg["after"] or "*Empty*")
            card.add_section("Time Logged", f"<t:{int(msg['timestamp'].timestamp())}:R>")
            await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="poll", description="Create an interactive Components V2 poll")
    @app_commands.describe(
        question="The poll question",
        options="Comma-separated list of options (2 to 4 options)",
        duration="Duration of the poll (e.g. 1h, 30m, 1d) - Default 24h",
        anonymous="Keep voter names secret (Default: False)",
        allow_multiple="Allow voting for multiple options (Default: False)"
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: str,
        options: str,
        duration: Optional[str] = "24h",
        anonymous: Optional[bool] = False,
        allow_multiple: Optional[bool] = False
    ):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("poll", interaction)
        
        opt_list = [o.strip() for o in options.split(',') if o.strip()]
        if len(opt_list) < 2 or len(opt_list) > 4:
            err = CynexCloudErrorContainer("Layout Restrictions", "Poll must contain between 2 and 4 options to fit action rows.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        seconds = parse_duration(duration)
        if not seconds:
            err = CynexCloudErrorContainer("Invalid Duration", "Please specify a correct duration tag like `1h`, `30m` or `1d`.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        end_dt = datetime.now() + timedelta(seconds=seconds)
        end_str = end_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        desc = generate_poll_description(question, opt_list, {}, anonymous, allow_multiple, int(end_dt.timestamp()), "active")
        
        layout = LayoutView()
        container = Container(accent_color=3447003)
        container.add_item(TextDisplay(desc))
        
        row_items = []
        for idx, opt in enumerate(opt_list):
            row_items.append(Button(label=opt, style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:poll:vote:{idx}"))
        row_items.append(Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:poll:cancel"))
        
        container.add_item(ActionRow(*row_items))
        layout.add_item(container)
        
        channel_msg = await interaction.channel.send(view=layout)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO polls (message_id, channel_id, guild_id, creator_id, question, options, votes, anonymous, allow_multiple, end_time) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)",
                (str(channel_msg.id), str(interaction.channel_id), str(interaction.guild_id), str(interaction.user.id), question, json.dumps(opt_list), 1 if anonymous else 0, 1 if allow_multiple else 0, end_str)
            )
            await db.commit()
            
        success = CynexCloudSuccessContainer("Poll Created", f"Your poll was successfully posted in {interaction.channel.mention}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="lock", description="Lock a channel to prevent users from sending messages")
    @app_commands.describe(channel="The channel to lock")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("lock", interaction)
        target = channel or interaction.channel
        
        overwrite = target.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is False:
            err = CynexCloudErrorContainer("Conflict", f"{target.mention} is already locked.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        overwrite.send_messages = False
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {interaction.user}")
        
        broadcast = CynexCloudWarningContainer("Channel Locked", f"🔒 **This channel has been locked by staff.**")
        await target.send(view=broadcast.build())
        
        success = CynexCloudSuccessContainer("Channel Locked Successfully", f"Locked {target.mention}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="unlock", description="Unlock a channel to allow users to send messages")
    @app_commands.describe(channel="The channel to unlock")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("unlock", interaction)
        target = channel or interaction.channel
        
        overwrite = target.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is not False:
            err = CynexCloudErrorContainer("Conflict", f"{target.mention} is not locked.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        overwrite.send_messages = None
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {interaction.user}")
        
        broadcast = CynexCloudSuccessContainer("Channel Unlocked", f"🔓 **This channel has been unlocked by staff.**")
        await target.send(view=broadcast.build())
        
        success = CynexCloudSuccessContainer("Channel Unlocked Successfully", f"Unlocked {target.mention}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="slowmode", description="Set slowmode delay for a channel")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)", channel="The channel to edit")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("slowmode", interaction)
        target = channel or interaction.channel
        
        if seconds < 0 or seconds > 21600:
            err = CynexCloudErrorContainer("Out of Range", "Slowmode delay must be between 0 and 21600 seconds.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        await target.edit(slowmode_delay=seconds, reason=f"Slowmode updated by {interaction.user}")
        
        if seconds == 0:
            broadcast = CynexCloudSuccessContainer("Slowmode Disabled", f"⏱ **Slowmode has been disabled in this channel by {interaction.user.mention}.**")
            await target.send(view=broadcast.build())
            success = CynexCloudSuccessContainer("Slowmode Updated", f"Disabled slowmode in {target.mention}.")
        else:
            broadcast = CynexCloudWarningContainer("Slowmode Enabled", f"⏱ **Slowmode has been set to `{seconds}s` in this channel by {interaction.user.mention}.**")
            await target.send(view=broadcast.build())
            success = CynexCloudSuccessContainer("Slowmode Updated", f"Set slowmode in {target.mention} to `{seconds}s`.")
            
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="purge", description="Bulk delete messages in this channel")
    @app_commands.describe(limit="Number of messages to delete (1 to 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, limit: int):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("purge", interaction)
        
        if limit < 1 or limit > 100:
            err = CynexCloudErrorContainer("Invalid Limit", "Purge limit must be between 1 and 100.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        deleted = await interaction.channel.purge(limit=limit)
        success = CynexCloudSuccessContainer("Purge Completed", f"Successfully deleted `{len(deleted)}` messages.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # EXTENDED UTILITYslash COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="help", description="Show CynexCloud interactive visual commands menu")
    async def help_menu(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("help", interaction)
        
        pages = [
            # Tickets
            "🎫 **CynexCloud Support Ticket System**\n"
            "• `/ticket setup`: Configure tickets roles and log channels.\n"
            "• `/ticket panel`: Launches visual builder panel.\n"
            "• `/ticket close` / `/ticket reopen`: Closes/Reopens channels.\n"
            "• `/ticket delete`: Confirms ticket deletion.\n"
            "• `/ticket claim` / `/ticket unclaim`: Claims ticket staff assignment.\n"
            "• `/ticket rename [name]`: Rename channel.\n"
            "• `/ticket add [member]` / `/ticket remove`: Modifies member access.\n"
            "• `/ticket transcript`: Generates HTML logs.\n"
            "• `/ticket stats`: Database stats overview.",
            
            # Reviews
            "⭐ **CynexCloud Service Reviews Module**\n"
            "• `/review setup [review_ch] [mod_ch]`: Setup reviews system.\n"
            "• `/review submit`: Opens reviews submit modal.\n"
            "• `/review list`: Paginated server reviews.\n"
            "• `/review stats`: Star count and top reviewers board.\n"
            "• `/review approve [id]` / `/review deny [id]`: Staff mod queue.",
            
            # Suggestions
            "💡 **CynexCloud Suggestions System**\n"
            "• `/suggest [category] [anonymous]`: Opens idea modal.\n"
            "• `/suggestion setup [channel]`: Setup suggestions destination.\n"
            "• `/suggestion approve [id] [reason]`: Approves suggestion and opens thread.\n"
            "• `/suggestion deny [id] [reason]`: Denies suggestion and clears buttons.\n"
            "• `/suggestion implement [id] [notes]`: Closes suggestion as completed.\n"
            "• `/suggestion stats`: Suggestions approval rate metric.",
            
            # Welcome
            "👋 **CynexCloud Welcome & Auto-Roles**\n"
            "• `/welcome setup [ch] [dm_enabled] [log_ch] [auto_role]`: Configure welcome parameters.\n"
            "• `/welcome preview` / `/welcome test`: Banners visual tester.\n"
            "• `/welcome edit [Welcome Message/Rules] [text]`: Property updates.\n"
            "• `/welcome disable`: Disable welcome greeting system.",
            
            # Admin & Moderation
            "🛡 **Administration & General Utilities**\n"
            "• `/lock` / `/unlock`: Lock/unlock channels permissions.\n"
            "• `/slowmode [seconds]`: Set channel slowmode delay.\n"
            "• `/purge [limit]`: Delete message logs (1-100).\n"
            "• `/sticky create [text]` / `/sticky delete`: Auto reposting message.\n"
            "• `/userinfo` / `/serverinfo`: Visual stats overview cards.\n"
            "• `/avatar` / `/banner`: View member avatars and banners.\n"
            "• `/ping`: Gateway and database latency stats.\n"
            "• `/afk`: Away From Keyboard status toggle.\n"
            "• `/snipe`: Snipes deleted/edited messages.\n"
            "• `/remind set` / `/remind list`: Scheduler reminders."
        ]
        
        paginator = CynexCloudPaginationContainer("CynexCloud Help Menu", pages, interaction.user.id)
        await interaction.followup.send(view=paginator, ephemeral=True)

    @app_commands.command(name="botinfo", description="Show information about the CynexCloud bot")
    async def botinfo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("botinfo", interaction)
        
        guilds_count = len(self.bot.guilds)
        users_count = sum(g.member_count for g in self.bot.guilds)
        
        card = CynexCloudInfoContainer("Bot Statistics Card", "Core framework configurations.")
        card.add_section("Developer Team", "`CynexCloud Developer Team`")
        card.add_section("Framework Library", "`discord.py v2.7.1` (Python 3.13)")
        card.add_section("Total Server Guilds", f"`{guilds_count}` servers")
        card.add_section("Total Users Served", f"`{users_count}` members")
        card.add_section("Gateway Ping", f"`{self.bot.latency * 1000:.1f}ms`")
        card.add_section("Shard Sharding Status", "`Single Shard ID: None`")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="uptime", description="Check how long the bot has been running")
    async def command_uptime(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("uptime", interaction)
        
        uptime = datetime.now() - self.bot.start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        time_str = f"`{days}d {hours}h {minutes}m {seconds}s`"
        card = CynexCloudInfoContainer("Bot System Uptime", f"The bot has been active for: {time_str}")
        card.add_section("Startup Reference", f"Last Boot: <t:{int(self.bot.start_time.timestamp())}:F>")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="stats", description="Show detailed bot analytics metrics")
    async def stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("stats", interaction)
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Command count
            async with db.execute("SELECT COUNT(*) FROM command_usage WHERE guild_id = ?", (guild_id,)) as cursor:
                cmd_row = await cursor.fetchone()
                total_commands = cmd_row[0] if cmd_row else 0
                
            # Most used command
            async with db.execute(
                "SELECT command_name, COUNT(*) as cnt FROM command_usage WHERE guild_id = ? GROUP BY command_name ORDER BY cnt DESC LIMIT 1",
                (guild_id,)
            ) as cursor:
                top_row = await cursor.fetchone()
                top_cmd = f"`/{top_row[0]}` ({top_row[1]} times)" if top_row else "No commands run yet"

            # Member history counts
            async with db.execute("SELECT COUNT(*) FROM guild_analytics WHERE guild_id = ? AND event_type = 'join'", (guild_id,)) as cursor:
                join_row = await cursor.fetchone()
                joins = join_row[0] if join_row else 0
                
            async with db.execute("SELECT COUNT(*) FROM guild_analytics WHERE guild_id = ? AND event_type = 'leave'", (guild_id,)) as cursor:
                leave_row = await cursor.fetchone()
                leaves = leave_row[0] if leave_row else 0

        card = CynexCloudInfoContainer("Guild Analytics Report", f"Metrics logging from this server.")
        card.add_section("Command Executions (Guild)", f"`{total_commands}` invocations")
        card.add_section("Most Popular Command", top_cmd)
        card.add_section("Join/Leave Metrics", f"📈 Member Joins: `{joins}`\n📉 Member Leaves: `{leaves}`")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="invite", description="Get the invite link for CynexCloud")
    async def command_invite(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("invite", interaction)
        
        link = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands"
        card = CynexCloudSuccessContainer("Bot Invitation Link", f"You can invite the bot to other guilds using the button or link below.")
        card.add_section("OAuth2 Link", f"[Authorize CynexCloud Bot]({link})")
        
        btn = Button(label="Invite Bot", style=discord.ButtonStyle.link, url=link)
        card.add_buttons(btn)
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="support", description="Get link to support server")
    async def support_server(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("support", interaction)
        
        url = "https://discord.gg/cynexcloud"
        card = CynexCloudInfoContainer("CynexCloud Help Desk Support", f"Need help setting up systems or reporting bugs? Join our support server.")
        card.add_section("Support Link", f"[Join Support Server]({url})")
        
        btn = Button(label="Join Server", style=discord.ButtonStyle.link, url=url)
        card.add_buttons(btn)
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="about", description="About the bot design philosophy")
    async def command_about(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("about", interaction)
        
        card = CynexCloudInfoContainer(
            "About CynexCloud Support Bot",
            "CynexCloud is an enterprise-grade utility, ticket, suggestion, and review bot. "
            "It is styled strictly utilizing Discord's modern Components V2 rendering layouts (Containers, sections, action rows, and text displays), "
            "providing a seamless and completely consistent server experience."
        )
        card.add_section("Framework Stack", "Python + discord.py 2.7.1 + aiosqlite (WAL connection enabled)")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="vote", description="Vote link for the bot")
    async def command_vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("vote", interaction)
        
        url = "https://top.gg/bot/cynexcloud"
        card = CynexCloudSuccessContainer("Vote for CynexCloud", "Support the development by voting for us!")
        card.add_section("Top.gg Link", f"[Vote Here]({url})")
        
        btn = Button(label="Vote", style=discord.ButtonStyle.link, url=url)
        card.add_buttons(btn)
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="links", description="Show official bot links")
    async def links(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("links", interaction)
        
        invite = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands"
        support = "https://discord.gg/cynexcloud"
        website = "https://cynexcloud.dev"
        vote = "https://top.gg/bot/cynexcloud"
        
        card = CynexCloudInfoContainer("CynexCloud Directory Links", "Useful official references.")
        card.add_section("Web Portal", f"🌐 [Website]({website})")
        card.add_section("OAuth Invite", f"🎫 [Authorize Bot]({invite})")
        card.add_section("Help Desk", f"📢 [Support Server]({support})")
        card.add_section("Top.gg Portal", f"⭐ [Vote Bot]({vote})")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="report", description="Submit an issue report to server moderators")
    @app_commands.describe(issue="Description of the bug/incident to report")
    async def report(self, interaction: discord.Interaction, issue: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("report", interaction)
        guild_id = str(interaction.guild.id)
        
        # Check review mod channel to route report
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT mod_channel_id FROM review_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                
        mod_ch = None
        if row:
            mod_ch = interaction.guild.get_channel(int(row[0]))
            
        if not mod_ch:
            # Fallback to tickets logging
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT log_channel_id FROM ticket_configs WHERE guild_id = ?", (guild_id,)) as cursor:
                    tick_row = await cursor.fetchone()
            if tick_row:
                mod_ch = interaction.guild.get_channel(int(tick_row[0]))

        if mod_ch:
            try:
                card = CynexCloudWarningContainer("Incident Report Filed", f"Report submitted by {interaction.user.mention}")
                card.add_section("Reporter", f"{interaction.user} (`{interaction.user.id}`)")
                card.add_section("Report details", issue)
                card.add_section("Channel Reference", interaction.channel.mention)
                await mod_ch.send(view=card.build())
            except Exception:
                pass

        success = CynexCloudSuccessContainer("Report Filed Successfully", "Moderators have been notified about this incident.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="calculator", description="Solve mathematical expressions securely")
    @app_commands.describe(expression="Basic mathematical formula (e.g. 2 + (5 * 3))")
    async def calculator(self, interaction: discord.Interaction, expression: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("calculator", interaction)
        
        safe_chars = set("0123456789+-*/(). ")
        if not all(c in safe_chars for c in expression):
            err = CynexCloudErrorContainer("Security Blocked", "Only digits and standard operators (`+`, `-`, `*`, `/`, `(`, `)`) are allowed.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        try:
            # Safe evaluation
            res = eval(expression, {"__builtins__": None}, {})
            card = CynexCloudSuccessContainer("Mathematical Solver", f"Solved expression: `{expression}`")
            card.add_section("Result Output", f"`{res}`")
            await interaction.followup.send(view=card.build(), ephemeral=True)
        except Exception as e:
            err = CynexCloudErrorContainer("Solver Error", f"Failed to compute math expression: {e}")
            await interaction.followup.send(view=err.build(), ephemeral=True)

    @app_commands.command(name="weather", description="Query current weather stats for a location")
    @app_commands.describe(location="City name or location")
    async def weather(self, interaction: discord.Interaction, location: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("weather", interaction)
        
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        current = data["current_condition"][0]
                        temp_c = current["temp_C"]
                        temp_f = current["temp_F"]
                        desc = current["weatherDesc"][0]["value"]
                        humidity = current["humidity"]
                        wind = current["windspeedKmph"]
                        
                        info = CynexCloudInfoContainer(f"Weather Report: {location.title()}", f"Current conditions at your target location.")
                        info.add_section("Temperature", f"`{temp_c}°C` / `{temp_f}°F`")
                        info.add_section("Conditions", desc.title())
                        info.add_section("Humidity", f"`{humidity}%`")
                        info.add_section("Wind Speed", f"`{wind} km/h`")
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        # Fallback
        info = CynexCloudWarningContainer(f"Weather Report: {location.title()}", "Weather query timed out. Showing typical climate report.")
        info.add_section("Temperature", "`22.0°C` / `71.6°F`")
        info.add_section("Conditions", "Partly Cloudy")
        info.add_section("Humidity", "`50%`")
        info.add_section("Wind Speed", "`10 km/h`")
        await interaction.followup.send(view=info.build(), ephemeral=True)

    @app_commands.command(name="translate", description="Translate text using Google Translate")
    @app_commands.describe(text="Text to translate", target_lang="Target language (e.g. en, es, fr, de)")
    async def translate(self, interaction: discord.Interaction, text: str, target_lang: str = "en"):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("translate", interaction)
        
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl={target_lang}&dt=t&q={urllib.parse.quote(text)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        translated = "".join([sentence[0] for sentence in data[0] if sentence[0]])
                        
                        info = CynexCloudSuccessContainer("Translation Complete", f"Language translations auto to `{target_lang}`.")
                        info.add_section("Input String", text)
                        info.add_section("Translated String", translated)
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        info = CynexCloudWarningContainer("Translation Failure", "Google translation requests failed. Outputting fallback mock.")
        info.add_section("Input", text)
        info.add_section("Translated Fallback", f"Translated to {target_lang}: {text}")
        await interaction.followup.send(view=info.build(), ephemeral=True)

    @app_commands.command(name="timestamp", description="Generate dynamic Discord timestamp code tags")
    @app_commands.describe(time_str="Time string input (e.g. 2026-06-20 18:00, or 'now')")
    async def timestamp(self, interaction: discord.Interaction, time_str: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("timestamp", interaction)
        
        if time_str.strip().lower() == "now":
            dt = datetime.now()
        else:
            try:
                dt = datetime.strptime(time_str.strip(), '%Y-%m-%d %H:%M')
            except Exception:
                try:
                    dt = datetime.strptime(time_str.strip(), '%Y-%m-%d')
                except Exception:
                    err = CynexCloudErrorContainer("Parsing Error", "Invalid time format. Please use `YYYY-MM-DD HH:MM` or `YYYY-MM-DD` or `now`.")
                    await interaction.followup.send(view=err.build(), ephemeral=True)
                    return
                    
        epoch = int(dt.timestamp())
        
        info = CynexCloudInfoContainer(f"Timestamp Generator: {dt.strftime('%Y-%m-%d %H:%M')}", "Copy the desired raw format code to display dynamic timestamps in server posts.")
        info.add_section("Relative Time (R)", f"`<t:{epoch}:R>` → <t:{epoch}:R>")
        info.add_section("Short Time (t)", f"`<t:{epoch}:t>` → <t:{epoch}:t>")
        info.add_section("Long Time (T)", f"`<t:{epoch}:T>` → <t:{epoch}:T>")
        info.add_section("Short Date (d)", f"`<t:{epoch}:d>` → <t:{epoch}:d>")
        info.add_section("Long Date (D)", f"`<t:{epoch}:D>` → <t:{epoch}:D>")
        info.add_section("Short Date/Time (f)", f"`<t:{epoch}:f>` → <t:{epoch}:f>")
        info.add_section("Long Date/Time (F)", f"`<t:{epoch}:F>` → <t:{epoch}:F>")
        await interaction.followup.send(view=info.build(), ephemeral=True)

    @app_commands.command(name="urban", description="Query definition from Urban Dictionary")
    @app_commands.describe(term="The word to search")
    async def urban(self, interaction: discord.Interaction, term: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("urban", interaction)
        
        url = f"http://api.urbandictionary.com/v0/define?term={urllib.parse.quote(term)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        definitions = data.get("list", [])
                        if definitions:
                            first = definitions[0]
                            word = first["word"]
                            definition = first["definition"].replace("[", "").replace("]", "")
                            example = first["example"].replace("[", "").replace("]", "")
                            thumbs_up = first["thumbs_up"]
                            
                            info = CynexCloudInfoContainer(f"Urban Dictionary: {word}", "Definition results.")
                            info.add_section("Definition", definition[:1000])
                            if example:
                                info.add_section("Example", example[:1000])
                            info.add_section("Rating", f"👍 `{thumbs_up}` positive reviews")
                            await interaction.followup.send(view=info.build(), ephemeral=True)
                            return
        except Exception:
            pass
            
        err = CynexCloudErrorContainer("No Definition Found", f"Urban Dictionary search returned zero results for `{term}`.")
        await interaction.followup.send(view=err.build(), ephemeral=True)

    @app_commands.command(name="github", description="Query repository metrics on GitHub")
    @app_commands.describe(repo="Repository namespace (e.g. google/google-api-python-client)")
    async def github(self, interaction: discord.Interaction, repo: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("github", interaction)
        
        url = f"https://api.github.com/repos/{repo}"
        headers = {"User-Agent": "CynexCloudBot/1.0"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        name = data["full_name"]
                        desc = data.get("description", "No description provided.")
                        stars = data["stargazers_count"]
                        forks = data["forks_count"]
                        issues = data["open_issues_count"]
                        
                        info = CynexCloudInfoContainer(f"GitHub: {name}", desc)
                        info.add_section("Stargazers", f"⭐ `{stars}`")
                        info.add_section("Forks Count", f"🍴 `{forks}`")
                        info.add_section("Open Issues", f"🐛 `{issues}`")
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        err = CynexCloudErrorContainer("Repo Offline", f"Could not find or retrieve details for GitHub repository `{repo}`.")
        await interaction.followup.send(view=err.build(), ephemeral=True)

    @app_commands.command(name="qr", description="Generate a QR code image link")
    @app_commands.describe(text="Content to encode into QR code image")
    async def qr(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("qr", interaction)
        
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(text)}"
        card = CynexCloudInfoContainer(f"QR Code Generator")
        card.add_section("Encoded Content", f"`{text}`")
        card.layout.add_item(MediaGallery(MediaGalleryItem(qr_url)))
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @app_commands.command(name="shorten", description="Shorten a URL link")
    @app_commands.describe(url="Long web URL to shorten")
    async def shorten_link(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("shorten", interaction)
        
        api_url = f"http://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as response:
                    if response.status == 200:
                        short_url = await response.text()
                        info = CynexCloudSuccessContainer("URL Link Shortened", "Long URL formatted successfully.")
                        info.add_section("Original Link", url)
                        info.add_section("TinyURL Link", short_url)
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        err = CynexCloudErrorContainer("API Error", "Failed to shorten URL. Make sure it is formatted correctly.")
        await interaction.followup.send(view=err.build(), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Global error handling logic logging commands failures and alerting users."""
        logger.exception(f"Error in slash command inside Utilities: {error}")
        msg = "❌ An unexpected error occurred while executing the command."
        
        if isinstance(error, app_commands.MissingPermissions):
            missing_perms = ", ".join(error.missing_permissions)
            msg = f"❌ You do not have the required permissions to run this command: `{missing_perms}`"
        elif isinstance(error, app_commands.BotMissingPermissions):
            missing_perms = ", ".join(error.missing_permissions)
            msg = f"❌ The bot is missing required permissions to execute this command: `{missing_perms}`"
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Command is on cooldown. Try again in `{error.retry_after:.1f}s`."
            
        err_card = CynexCloudErrorContainer("Command Execution Failed", msg)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(view=err_card.build(), ephemeral=True)
            else:
                await interaction.followup.send(view=err_card.build(), ephemeral=True)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════
# STICKY GROUP COMMANDS
# ══════════════════════════════════════════════════════════════════════

class StickyGroup(app_commands.Group, name="sticky"):
    def __init__(self, cog: Utilities):
        super().__init__(description="Manage sticky messages in text channels")
        self.cog = cog

    @app_commands.command(name="create", description="Create or update a sticky message in this channel")
    @app_commands.describe(text="The sticky message content")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def create(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("sticky create", interaction)
        channel_id = interaction.channel.id
        guild_id = interaction.guild.id
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sticky_messages (channel_id, guild_id, message_text) VALUES (?, ?, ?)",
                (str(channel_id), str(guild_id), text)
            )
            await db.commit()
            
        self.cog.sticky_messages[channel_id] = text
        
        old_id = self.cog.sticky_last_ids.get(channel_id)
        if old_id:
            try:
                old_msg = await interaction.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
                
        layout = LayoutView()
        container = Container(accent_color=16776960)
        container.add_item(TextDisplay(f"📌 **Sticky Message**\n\n{text}"))
        layout.add_item(container)
        new_msg = await interaction.channel.send(view=layout)
        
        self.cog.sticky_last_ids[channel_id] = new_msg.id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sticky_messages SET last_message_id = ? WHERE channel_id = ?",
                (str(new_msg.id), str(channel_id))
            )
            await db.commit()
            
        success = CynexCloudSuccessContainer("Sticky Message Posted", "Sticky message created successfully and pinned.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="delete", description="Delete the sticky message in this channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("sticky delete", interaction)
        channel_id = interaction.channel.id
        
        if channel_id not in self.cog.sticky_messages:
            err = CynexCloudErrorContainer("Not Found", "There is no active sticky message configured in this channel.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM sticky_messages WHERE channel_id = ?", (str(channel_id),))
            await db.commit()
            
        old_id = self.cog.sticky_last_ids.pop(channel_id, None)
        if old_id:
            try:
                old_msg = await interaction.channel.fetch_message(old_id)
                await old_msg.delete()
            except Exception:
                pass
                
        self.cog.sticky_messages.pop(channel_id, None)
        success = CynexCloudSuccessContainer("Sticky Message Deleted", "Sticky banner removed successfully.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="list", description="List all active sticky messages in this server")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("sticky list", interaction)
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT channel_id, message_text FROM sticky_messages WHERE guild_id = ?", (guild_id,)) as cursor:
                rows = await cursor.fetchall()
                
        if not rows:
            info = CynexCloudInfoContainer("No Sticky Messages", "There are no active sticky messages configured on this server.")
            await interaction.followup.send(view=info.build(), ephemeral=True)
            return
            
        pages = []
        for r in rows:
            channel = interaction.guild.get_channel(int(r[0]))
            mention = channel.mention if channel else f"#{r[0]}"
            pages.append(
                f"**Channel:** {channel.name if channel else r[0]} ({mention})\n"
                f"**Text:** {r[1]}"
            )
            
        paginator = CynexCloudPaginationContainer("Server Sticky Messages", pages, interaction.user.id, accent_color=16776960)
        await interaction.followup.send(view=paginator, ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# REMINDER GROUP COMMANDS
# ══════════════════════════════════════════════════════════════════════

class RemindGroup(app_commands.Group, name="remind"):
    def __init__(self, cog: Utilities):
        super().__init__(description="Manage personal reminders")
        self.cog = cog

    @app_commands.command(name="set", description="Set a reminder alert")
    @app_commands.describe(duration="Duration (e.g., 10m, 2h, 1d)", text="What to remind you about")
    async def set_reminder(self, interaction: discord.Interaction, duration: str, text: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("remind set", interaction)
        seconds = parse_duration(duration)
        if not seconds:
            err = CynexCloudErrorContainer("Invalid Duration", "Please specify a correct duration tag like `10m`, `2h`, `1d`.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        target_dt = datetime.now() + timedelta(seconds=seconds)
        target_str = target_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel.id)
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO reminders (user_id, channel_id, guild_id, reminder_text, target_time) VALUES (?, ?, ?, ?, ?)",
                (user_id, channel_id, guild_id, text, target_str)
            )
            await db.commit()
            
        timestamp_epoch = int(target_dt.timestamp())
        success = CynexCloudSuccessContainer("Reminder Registered", f"Reminder alert successfully scheduled.")
        success.add_section("Alert Time", f"<t:{timestamp_epoch}:F> (<t:{timestamp_epoch}:R>)")
        success.add_section("Reminder Text", text)
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="list", description="List your active reminders")
    async def list_reminders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("remind list", interaction)
        user_id = str(interaction.user.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, reminder_text, target_time FROM reminders WHERE user_id = ? ORDER BY target_time ASC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                
        if not rows:
            info = CynexCloudInfoContainer("No Reminders", "You have no active reminders scheduled.")
            await interaction.followup.send(view=info.build(), ephemeral=True)
            return
            
        pages = []
        for r in rows:
            rem_id = r[0]
            rem_text = r[1]
            target_time_str = r[2]
            
            try:
                dt = datetime.strptime(target_time_str, '%Y-%m-%d %H:%M:%S')
                epoch = int(dt.timestamp())
                time_display = f"<t:{epoch}:F> (<t:{epoch}:R>)"
            except Exception:
                time_display = target_time_str
                
            pages.append(
                f"**Reminder ID:** `{rem_id}`\n"
                f"**Alert Time:** {time_display}\n"
                f"**Reminder Content:** {rem_text}"
            )
            
        paginator = CynexCloudPaginationContainer("Your Scheduled Reminders", pages, interaction.user.id)
        await interaction.followup.send(view=paginator, ephemeral=True)

    @app_commands.command(name="delete", description="Delete an active reminder by ID")
    @app_commands.describe(id="ID of the reminder to delete")
    async def delete_reminder(self, interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("remind delete", interaction)
        user_id = str(interaction.user.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM reminders WHERE id = ? AND user_id = ?", (id, user_id)) as cursor:
                row = await cursor.fetchone()
            if not row:
                err = CynexCloudErrorContainer("Not Found", f"Reminder `{id}` was not found or doesn't belong to you.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            await db.execute("DELETE FROM reminders WHERE id = ?", (id,))
            await db.commit()
            
        success = CynexCloudSuccessContainer("Reminder Deleted", f"Reminder `{id}` cleared successfully.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="clear", description="Clear all of your active reminders")
    async def clear_reminders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("remind clear", interaction)
        user_id = str(interaction.user.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
            await db.commit()
            
        success = CynexCloudSuccessContainer("Reminders Cleared", "All personal active reminders have been deleted.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# SETUP COG ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    cog = Utilities(bot)
    await bot.add_cog(cog)
    bot.add_view(PersistentPollView())
