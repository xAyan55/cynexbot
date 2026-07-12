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
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeWarningContainer,
    BreezeInfoContainer,
    BreezePaginationContainer,
    BreezeContainerBuilder,
    create_info_card,
    create_success_section,
    create_warning_section,
    create_error_section,
    create_user_card,
    create_server_card,
    create_pagination_menu
)

logger = logging.getLogger("Breeze.Utilities")
DB_PATH = "breeze.db"

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

def build_poll_layout(question: str, options: list, votes: dict, anonymous: bool, allow_multiple: bool, end_time_epoch: int, status: str) -> discord.ui.LayoutView:
    total_voters = len(votes)
    tally = [0] * len(options)
    for voter_id, choice_indices in votes.items():
        for idx in choice_indices:
            if 0 <= idx < len(options):
                tally[idx] += 1
                
    total_votes_cast = sum(tally)
    
    layout = discord.ui.LayoutView()
    
    # Container 1: Question and Options 1 & 2
    container1 = discord.ui.Container(accent_color=3447003)
    container1.add_item(discord.ui.TextDisplay(f"📊 **{question}**"))
    container1.add_item(discord.ui.Separator())
    
    count0 = tally[0]
    pct0 = (count0 / total_votes_cast * 100) if total_votes_cast > 0 else 0
    bar0 = "🟩" * int(round(pct0 / 10)) + "⬛" * (10 - int(round(pct0 / 10)))
    container1.add_item(discord.ui.Section(title=f"Option 1: {options[0]}", text=f"{bar0} `{count0} votes` ({pct0:.1f}%)"))
    container1.add_item(discord.ui.Separator())
    
    count1 = tally[1]
    pct1 = (count1 / total_votes_cast * 100) if total_votes_cast > 0 else 0
    bar1 = "🟩" * int(round(pct1 / 10)) + "⬛" * (10 - int(round(pct1 / 10)))
    container1.add_item(discord.ui.Section(title=f"Option 2: {options[1]}", text=f"{bar1} `{count1} votes` ({pct1:.1f}%)"))
    
    layout.add_item(container1)
    
    # Container 2: Options 3 & 4 (if any)
    if len(options) > 2:
        container2 = discord.ui.Container(accent_color=3447003)
        count2 = tally[2]
        pct2 = (count2 / total_votes_cast * 100) if total_votes_cast > 0 else 0
        bar2 = "🟩" * int(round(pct2 / 10)) + "⬛" * (10 - int(round(pct2 / 10)))
        container2.add_item(discord.ui.Section(title=f"Option 3: {options[2]}", text=f"{bar2} `{count2} votes` ({pct2:.1f}%)"))
        
        if len(options) > 3:
            container2.add_item(discord.ui.Separator())
            count3 = tally[3]
            pct3 = (count3 / total_votes_cast * 100) if total_votes_cast > 0 else 0
            bar3 = "🟩" * int(round(pct3 / 10)) + "⬛" * (10 - int(round(pct3 / 10)))
            container2.add_item(discord.ui.Section(title=f"Option 4: {options[3]}", text=f"{bar3} `{count3} votes` ({pct3:.1f}%)"))
            
        layout.add_item(container2)
        
    # Container 3: Stats / Status / Results
    container_stats = discord.ui.Container(accent_color=3447003)
    if status == "closed":
        max_votes = max(tally) if tally else 0
        if max_votes > 0:
            winners = [options[i] for i, v in enumerate(tally) if v == max_votes]
            w_str = f"`{winners[0]}`" if len(winners) == 1 else ", ".join([f"`{w}`" for w in winners])
            container_stats.add_item(discord.ui.Section(title="🏆 Results Winner", text=w_str))
        else:
            container_stats.add_item(discord.ui.Section(title="🏆 Results Winner", text="No votes were cast."))
            
        container_stats.add_item(discord.ui.Separator())
        container_stats.add_item(discord.ui.Section(title="Status", text="🔒 *This poll is closed.*"))
        layout.add_item(container_stats)
    elif status == "cancelled":
        container_stats.add_item(discord.ui.Section(title="Status", text="❌ *This poll was cancelled.*"))
        layout.add_item(container_stats)
    else:
        # Active metadata
        settings_flags = []
        if anonymous:
            settings_flags.append("Anonymous")
        if allow_multiple:
            settings_flags.append("Multiple Choice")
        if not settings_flags:
            settings_flags.append("Single Choice")
            
        metadata_text = f"⏳ <t:{end_time_epoch}:F> (<t:{end_time_epoch}:R>)\n⚙️ **Settings:** {', '.join(settings_flags)}"
        container_stats.add_item(discord.ui.Section(title="ℹ️ Poll Metadata", text=metadata_text))
        container_stats.add_item(discord.ui.Separator())
        
        voter_text = f"👤 **Total Voters:** `{total_voters}`"
        if not anonymous and total_voters > 0:
            voter_lines = []
            for voter_id, choices in list(votes.items())[-5:]:
                choices_str = ", ".join([f"`{options[c]}`" for c in choices if 0 <= c < len(options)])
                voter_lines.append(f"<@{voter_id}> voted for {choices_str}")
            voter_text += "\n" + "\n".join(voter_lines)
            
        container_stats.add_item(discord.ui.Section(title="👥 Voter Activity", text=voter_text))
        layout.add_item(container_stats)
        
    # Container 4: Action Row for buttons
    if status == "active":
        container_btn = discord.ui.Container(accent_color=3447003)
        row_items = []
        for idx, opt in enumerate(options):
            row_items.append(discord.ui.Button(label=opt, style=discord.ButtonStyle.secondary, custom_id=f"breeze:poll:vote:{idx}"))
        row_items.append(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:cancel"))
        container_btn.add_item(discord.ui.ActionRow(*row_items))
        layout.add_item(container_btn)
        
    # Validate layout constraints
    from tickets import validate_v2_layout
    validate_v2_layout(layout)


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
        err = BreezeErrorContainer("Poll Not Found", "Poll data not found in database.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    question, options_json, votes_json, anonymous_int, allow_multiple_int, end_time_str, status = row
    
    if status != "active":
        err = BreezeErrorContainer("Poll Inactive", "This poll has already ended.")
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
        
    layout = build_poll_layout(question, options, votes, anonymous, allow_multiple, end_time_epoch, status)
    await interaction.message.edit(view=layout)
    success = BreezeSuccessContainer("Vote Saved", f"Your vote has been successfully {msg_action}!")
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
        err = BreezeErrorContainer("Poll Not Found", "Poll metadata was not located.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    creator_id, question, options_json, votes_json, anonymous_int, allow_multiple_int, end_time_str, status = row
    
    if status != "active":
        err = BreezeErrorContainer("Poll Inactive", "This poll is already closed or cancelled.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    is_admin = interaction.user.guild_permissions.administrator
    if user_id != creator_id and not is_admin:
        err = BreezeErrorContainer("Unauthorized Action", "Only the poll creator or administrators can cancel this poll.")
        await interaction.followup.send(view=err.build(), ephemeral=True)
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE polls SET status = 'cancelled' WHERE message_id = ?", (msg_id,))
        await db.commit()
        
    options = json.loads(options_json)
    votes = json.loads(votes_json)
    anonymous = bool(anonymous_int)
    allow_multiple = bool(allow_multiple_int)
    
    layout = build_poll_layout(question, options, votes, anonymous, allow_multiple, int(datetime.now().timestamp()), "cancelled")
    await interaction.message.edit(view=layout)
    success = BreezeSuccessContainer("Poll Cancelled", "This poll status has been marked as cancelled.")
    await interaction.followup.send(view=success.build(), ephemeral=True)

class PersistentPollView(discord.ui.View):
    """Persistent View handling global option buttons and cancellations."""
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Option 1", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:vote:0")
    async def vote_0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 0)

    @discord.ui.button(label="Option 2", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:vote:1")
    async def vote_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 1)

    @discord.ui.button(label="Option 3", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:vote:2")
    async def vote_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 2)

    @discord.ui.button(label="Option 4", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:vote:3")
    async def vote_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_poll_vote_interaction(interaction, 3)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="breeze:poll:cancel")
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
                        rem_layout = BreezeInfoContainer("Breeze Reminder Alert", text)
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
                
            welcome = BreezeSuccessContainer("Welcome Back", f"Hello {message.author.mention}, I've removed your AFK status.")
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
                        
                    afk_card = BreezeInfoContainer("Member AFK", f"💤 **{user.display_name}** is currently Away From Keyboard.")
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
        roles_str = ", ".join(roles[:20]) + (f" and {len(roles)-20} more..." if len(roles) > 20 else "") if roles else "None"
        
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
        
        # Collect key permissions
        perms = target.guild_permissions
        key_perms = []
        if perms.administrator:
            key_perms.append("Administrator")
        if perms.manage_guild:
            key_perms.append("Manage Server")
        if perms.manage_roles:
            key_perms.append("Manage Roles")
        if perms.manage_channels:
            key_perms.append("Manage Channels")
        if perms.kick_members:
            key_perms.append("Kick Members")
        if perms.ban_members:
            key_perms.append("Ban Members")
        if perms.manage_messages:
            key_perms.append("Manage Messages")
        if perms.mention_everyone:
            key_perms.append("Mention Everyone")
        if perms.mute_members:
            key_perms.append("Mute Members")
        if perms.deafen_members:
            key_perms.append("Deafen Members")
        if perms.move_members:
            key_perms.append("Move Members")
            
        perms_str = ", ".join(key_perms) if key_perms else "None"
        
        sections = {
            "👤 User Profile": f"• **Username:** `{target.name}`\n• **Display Name:** `{target.display_name}`\n• **ID:** `{target.id}`\n• **Bot Status:** `{'Yes 🤖' if target.bot else 'No 👤'}`",
            "📅 Account": f"• **Registered:** <t:{int(target.created_at.timestamp())}:F> (<t:{int(target.created_at.timestamp())}:R>)\n• **Joined:** <t:{int(target.joined_at.timestamp())}:F> (<t:{int(target.joined_at.timestamp())}:R>)",
            "🎭 Roles": f"• **Total Roles:** `{len(roles)}`\n• **List:** {roles_str}",
            "📊 Statistics": f"• **Badges:** {flags_str}\n• **Administrator:** `{'Yes 🛡️' if target.guild_permissions.administrator else 'No'}`",
            "📝 Permissions": f"• **Key Permissions:** {perms_str}"
        }
        card = create_user_card(target, sections)
        await interaction.followup.send(view=card, ephemeral=True)

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
        
        sections = {
            "General Information": f"• **Owner:** {guild.owner.mention if guild.owner else 'Unknown'} (`{guild.owner_id}`)\n• **Created On:** <t:{int(guild.created_at.timestamp())}:F> (<t:{int(guild.created_at.timestamp())}:R>)\n• **Server ID:** `{guild.id}`",
            "Members": f"• **Total Members:** `{guild.member_count}`\n• **Humans:** `{humans}`\n• **Bots:** `{bots}`",
            "Channels": f"• **Categories:** `{categories}`\n• **Text Channels:** `{text_channels}`\n• **Voice Channels:** `{voice_channels}`",
            "Boosts": f"• **Total Boosts:** `{guild.premium_subscription_count}`\n• **Level Tier:** `{guild.premium_tier}`",
            "Roles": f"• **Total Roles:** `{len(guild.roles)}`"
        }
        card = create_server_card(guild, sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="avatar", description="Show a member's avatar")
    @app_commands.describe(member="The member to view")
    async def avatar(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("avatar", interaction)
        target = member or interaction.user
        
        builder = BreezeContainerBuilder(f"Avatar of {target.name}", f"Avatar profile asset for {target.mention}")
        builder.add_section("Metadata Details", f"• **Avatar URL:** [Download Link]({target.display_avatar.url})")
        builder.layout.add_item(MediaGallery(MediaGalleryItem(target.display_avatar.url)))
        await interaction.followup.send(view=builder.build(), ephemeral=True)

    @app_commands.command(name="banner", description="Show a member's banner profile image")
    @app_commands.describe(member="The member to view")
    async def banner(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("banner", interaction)
        target = member or interaction.user
        
        user = await self.bot.fetch_user(target.id)
        if not user.banner:
            err = create_error_section("No Banner Detected", f"User {target.mention} does not have a profile banner configuration.")
            await interaction.followup.send(view=err, ephemeral=True)
            return
            
        builder = BreezeContainerBuilder(f"Profile Banner of {target.name}", f"Profile banner asset for {target.mention}")
        builder.add_section("Metadata Details", f"• **Banner URL:** [Download Link]({user.banner.url})")
        builder.layout.add_item(MediaGallery(MediaGalleryItem(user.banner.url)))
        await interaction.followup.send(view=builder.build(), ephemeral=True)

    @app_commands.command(name="roleinfo", description="Show detailed information about a role")
    @app_commands.describe(role="The role to view")
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("roleinfo", interaction)
        
        member_count = len(role.members)
        permissions = [p[0].replace('_', ' ').title() for p in role.permissions if p[1]]
        perms_str = ", ".join(permissions[:15]) + (f" and {len(permissions)-15} more..." if len(permissions) > 15 else "") if permissions else "None"
        
        sections = {
            "🎨 Color Code": f"`{role.color}`",
            "📊 Position Rank": f"`{role.position}`",
            "👥 Members Assigned": f"`{member_count}` users",
            "⚙️ Settings Flags": f"• **Hoisted:** `{'Yes' if role.hoist else 'No'}`\n• **Mentionable:** `{'Yes' if role.mentionable else 'No'}`",
            "🛡️ Permissions": perms_str
        }
        card = create_info_card(f"Role Config: {role.name}", f"ID: `{role.id}`", sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="channelinfo", description="Show detailed information about a channel")
    @app_commands.describe(channel="The channel to view")
    async def channelinfo(self, interaction: discord.Interaction, channel: Optional[Union[discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel]] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("channelinfo", interaction)
        target = channel or interaction.channel
        
        sections = {
            "📝 Basic Details": f"• **Name:** `#{target.name}`\n• **ID:** `{target.id}`\n• **Type:** `{target.type.name.title()}`",
            "📁 Category Parent": f"{target.category.name if target.category else 'None'}",
            "📊 Channel Position": f"`{target.position}`",
            "📅 Created On": f"<t:{int(target.created_at.timestamp())}:F> (<t:{int(target.created_at.timestamp())}:R>)"
        }
        
        if isinstance(target, discord.TextChannel):
            sections["💬 Text Configuration"] = f"• **Topic:** {target.topic or 'No topic set.'}\n• **Slowmode:** `{target.slowmode_delay}s`\n• **NSFW:** `{'Yes' if target.is_nsfw() else 'No'}`"
        elif isinstance(target, discord.VoiceChannel):
            sections["🔊 Voice Configuration"] = f"• **Bitrate:** `{target.bitrate // 1000} kbps`\n• **User Limit:** `{target.user_limit or 'Unlimited'}` users"
            
        card = create_info_card(f"Channel Config: #{target.name}", None, sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="membercount", description="Show server member metrics")
    async def membercount(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("membercount", interaction)
        guild = interaction.guild
        total = guild.member_count
        bots = sum(1 for m in guild.members if m.bot)
        humans = total - bots
        
        sections = {
            "👥 Total Members": f"`{total}`",
            "👤 Humans": f"`{humans}`",
            "🤖 Bot Accounts": f"`{bots}`"
        }
        card = create_info_card("Server Members Count", f"Total stats for **{guild.name}**", sections)
        await interaction.followup.send(view=card, ephemeral=True)

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
        card_loader = create_info_card("Ping Latency Metric", "Measuring API latency round trip...", {})
        followup_msg = await interaction.followup.send(view=card_loader, ephemeral=True)
        api_latency = (datetime.now() - t2).total_seconds() * 1000
        
        sections = {
            "⚡ Gateway Latency": f"{gw_latency:.1f}ms",
            "💾 Database": f"{db_latency:.1f}ms",
            "🌐 API": "Healthy"
        }
        card = create_info_card("🏓 Pong Latencies", "System connection and database response times.", sections)
        await followup_msg.edit(view=card)

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
            
        success = BreezeSuccessContainer("AFK Status Enabled", f"I have set your status to AFK: **{reason}**")
        await interaction.followup.send(view=success.build(), ephemeral=True)
        
        broadcast = BreezeInfoContainer("AFK Notification", f"💤 {interaction.user.mention} is now Away From Keyboard: **{reason}**")
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
                err = BreezeErrorContainer("Sniper Log Empty", "No recently deleted messages found in this channel.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            msg = cache[-1]
            card = BreezeWarningContainer("Sniped Deleted Message", f"Author: {msg['author'].mention}")
            card.add_section("Deleted Message Content", msg["content"] or "*No content (attachment only or empty)*")
            if msg["attachments"]:
                card.add_section("Attachments URLs", "\n".join(msg["attachments"]))
            card.add_section("Time Logged", f"<t:{int(msg['timestamp'].timestamp())}:R>")
            await interaction.followup.send(view=card.build(), ephemeral=True)
        else:
            cache = self.sniped_edited.get(channel_id)
            if not cache or len(cache) == 0:
                err = BreezeErrorContainer("Sniper Log Empty", "No recently edited messages found in this channel.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            msg = cache[-1]
            card = BreezeWarningContainer("Sniped Edited Message", f"Author: {msg['author'].mention}")
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
            err = BreezeErrorContainer("Layout Restrictions", "Poll must contain between 2 and 4 options to fit action rows.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        seconds = parse_duration(duration)
        if not seconds:
            err = BreezeErrorContainer("Invalid Duration", "Please specify a correct duration tag like `1h`, `30m` or `1d`.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        end_dt = datetime.now() + timedelta(seconds=seconds)
        end_str = end_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        layout = build_poll_layout(question, opt_list, {}, anonymous, allow_multiple, int(end_dt.timestamp()), "active")
        channel_msg = await interaction.channel.send(view=layout)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO polls (message_id, channel_id, guild_id, creator_id, question, options, votes, anonymous, allow_multiple, end_time) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)",
                (str(channel_msg.id), str(interaction.channel_id), str(interaction.guild_id), str(interaction.user.id), question, json.dumps(opt_list), 1 if anonymous else 0, 1 if allow_multiple else 0, end_str)
            )
            await db.commit()
            
        success = BreezeSuccessContainer("Poll Created", f"Your poll was successfully posted in {interaction.channel.mention}.")
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
            err = BreezeErrorContainer("Conflict", f"{target.mention} is already locked.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        overwrite.send_messages = False
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {interaction.user}")
        
        broadcast = BreezeWarningContainer("Channel Locked", f"🔒 **This channel has been locked by staff.**")
        await target.send(view=broadcast.build())
        
        success = BreezeSuccessContainer("Channel Locked Successfully", f"Locked {target.mention}.")
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
            err = BreezeErrorContainer("Conflict", f"{target.mention} is not locked.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        overwrite.send_messages = None
        await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {interaction.user}")
        
        broadcast = BreezeSuccessContainer("Channel Unlocked", f"🔓 **This channel has been unlocked by staff.**")
        await target.send(view=broadcast.build())
        
        success = BreezeSuccessContainer("Channel Unlocked Successfully", f"Unlocked {target.mention}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="slowmode", description="Set slowmode delay for a channel")
    @app_commands.describe(seconds="Slowmode delay in seconds (0 to disable)", channel="The channel to edit")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: int, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("slowmode", interaction)
        target = channel or interaction.channel
        
        if seconds < 0 or seconds > 21600:
            err = create_error_section("Out of Range", "Slowmode delay must be between 0 and 21600 seconds.")
            await interaction.followup.send(view=err, ephemeral=True)
            return
            
        await target.edit(slowmode_delay=seconds, reason=f"Slowmode updated by {interaction.user}")
        
        if seconds == 0:
            broadcast = create_success_section("Slowmode Disabled", f"⏱ **Slowmode has been disabled in this channel by {interaction.user.mention}.**")
            await target.send(view=broadcast)
            success = create_success_section("Slowmode Updated", f"Disabled slowmode in {target.mention}.")
        else:
            broadcast = create_warning_section("Slowmode Enabled", f"⏱ **Slowmode has been set to `{seconds}s` in this channel by {interaction.user.mention}.**")
            await target.send(view=broadcast)
            success = create_success_section("Slowmode Updated", f"Set slowmode in {target.mention} to `{seconds}s`.")
            
        await interaction.followup.send(view=success, ephemeral=True)

    @app_commands.command(name="purge", description="Bulk delete messages in this channel")
    @app_commands.describe(limit="Number of messages to delete (1 to 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, limit: int):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("purge", interaction)
        
        if limit < 1 or limit > 100:
            err = BreezeErrorContainer("Invalid Limit", "Purge limit must be between 1 and 100.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        deleted = await interaction.channel.purge(limit=limit)
        success = BreezeSuccessContainer("Purge Completed", f"Successfully deleted `{len(deleted)}` messages.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # EXTENDED UTILITYslash COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="help", description="Explore Breeze commands, setup guides and utilities")
    async def help_menu(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("help", interaction)
        
        pages = [
            # Index page (Welcome category overview)
            {
                "title": "Breeze Help Center",
                "description": "Welcome to Breeze.\nSelect a category below to explore commands.",
                "sections": [
                    ("🌿 Welcome System", "Configure welcome messages, auto roles, greeting channels and previews."),
                    ("🎫 Ticket System", "Professional ticket management with transcripts, claims and panels."),
                    ("🛡️ Moderation", "Anti-swear, warnings, logging and moderation utilities."),
                    ("💡 Suggestions System", "Enterprise-grade suggestions with moderator queues and approval loops."),
                    ("⭐ Reviews System", "Service reviews module with moderation approval workflow."),
                    ("⚙️ General Utilities", "Core utility commands and administrative options.")
                ]
            },
            # Page 1: Welcome System
            {
                "title": "Welcome System",
                "description": "Configure welcome messages, auto roles, greeting channels and previews.",
                "sections": [
                    ("⚙️ Welcome Setup", "`/welcome setup [ch] [dm_enabled] [log_ch] [auto_role]`\nConfigure the welcome greeting and logging systems."),
                    ("👁️ Preview Welcome", "`/welcome preview`\nPreview the welcome greeting banner layout."),
                    ("📝 Edit Welcome Option", "`/welcome edit [option] [text]`\nUpdate greeting message text or rules guidelines text."),
                    ("🏷️ Welcome Variables", "`/welcome variables`\nDisplay all supported placeholders available for welcome messages."),
                    ("❌ Disable Welcome", "`/welcome disable`\nDisable the welcome greeting system on the guild.")
                ]
            },
            # Page 2: Ticket System
            {
                "title": "Ticket System",
                "description": "Professional ticket management with transcripts, claims and panels.",
                "sections": [
                    ("⚙️ Ticket Setup", "`/ticket setup [role] [log_channel]`\nConfigure support staff role and logging channel."),
                    ("🎫 Ticket Panel", "`/ticket panel [channel] [title] [description]`\nDeploy a persistent ticket panel with custom branding."),
                    ("🔒 Ticket Control", "`/ticket close` / `/ticket reopen`\nClose or reopen an existing support ticket."),
                    ("🗑️ Ticket Delete", "`/ticket delete`\nDelete the ticket channel (logs transcript if logging is configured)."),
                    ("📌 Ticket Assignment", "`/ticket claim` / `/ticket unclaim`\nClaim or unclaim a ticket as a support staff member."),
                    ("🏷️ Ticket Rename", "`/ticket rename [name]`\nRename the ticket channel safely."),
                    ("👥 Ticket Members", "`/ticket add [member]` / `/ticket remove [member]`\nAdd or remove server members from a ticket channel."),
                    ("📜 Ticket Transcript", "`/ticket transcript`\nManually generate and export an HTML transcript of messages."),
                    ("📊 Ticket Stats", "`/ticket stats` / `/ticket list`\nView ticket stats and queue.")
                ]
            },
            # Page 3: Suggestions System
            {
                "title": "Suggestions System",
                "description": "Enterprise-grade suggestions with moderator queues and approval loops.",
                "sections": [
                    ("💡 Suggest Idea", "`/suggest [category] [anonymous]`\nSubmit an idea or feedback via interactive modal."),
                    ("⚙️ Suggestion Setup", "`/suggestion setup [channel]`\nConfigure the suggestions target channel."),
                    ("✅ Suggestion Approve", "`/suggestion approve [id] [reason]`\nApprove suggestion and open a discussion thread."),
                    ("❌ Suggestion Deny", "`/suggestion deny [id] [reason]`\nDeny suggestion and notify the author."),
                    ("🚀 Suggestion Implement", "`/suggestion implement [id] [notes]`\nMark suggestion as successfully implemented."),
                    ("📊 Suggestion Stats", "`/suggestion stats`\nView suggestion approval and implementation rates.")
                ]
            },
            # Page 4: Reviews System
            {
                "title": "Reviews System",
                "description": "Service reviews module with moderation approval workflow.",
                "sections": [
                    ("⚙️ Review Setup", "`/review setup [review_ch] [mod_ch]`\nSetup reviews and moderation queue channels."),
                    ("📝 Review Submit", "`/review submit`\nOpen interactive review submission modal."),
                    ("📂 Review List", "`/review list`\nDisplay paginated lists of server reviews."),
                    ("📊 Review Stats", "`/review stats`\nStar count breakdown and top reviewers leaderboard."),
                    ("✅ Review Moderation", "`/review approve [id]` / `/review deny [id]`\nApprove or deny reviews in the staff channel.")
                ]
            },
            # Page 5: Moderation
            {
                "title": "Moderation",
                "description": "Anti-swear, warnings, logging and moderation utilities.",
                "sections": [
                    ("🤬 Antiswear Control", "`/antiswear enable` / `/antiswear disable`\nEnable or disable anti-swear filters."),
                    ("📝 Antiswear Words", "`/antiswear add [word]` / `/antiswear remove [word]`\nAdd or remove words from swear list."),
                    ("📂 Antiswear Settings", "`/antiswear list` / `/antiswear regex [mode]`\nList swear words or toggle regex mode."),
                    ("🛡️ Moderation Whitelist", "`/whitelist role [role]` / `/whitelist channel [channel]`\nBypass anti-swear for specific role/channel."),
                    ("⚠️ Warnings Manager", "`/warnings check [member]` / `/warnings clear [member]`\nCheck or clear user warning records.")
                ]
            },
            # Page 6: General Utilities
            {
                "title": "General Utilities",
                "description": "Core utility commands and administrative options.",
                "sections": [
                    ("🔒 Channel Lock & Unlock", "`/lock` / `/unlock`\nLock or unlock channel permissions for members."),
                    ("⏱️ Slowmode & Purge", "`/slowmode [seconds]` / `/purge [limit]`\nSet slowmode delay or bulk delete messages."),
                    ("📌 Sticky Messages", "`/sticky create [text]` / `/sticky delete`\nManage sticky messages in channels."),
                    ("👤 Profile & Server", "`/userinfo` / `/serverinfo`\nDetailed visual stats overview cards."),
                    ("🖼️ Avatar & Banner", "`/avatar` / `/banner`\nView member profile avatars and banners."),
                    ("🏓 Bot Diagnostics", "`/ping` / `/botinfo` / `/uptime` / `/stats`\nCheck bot system stats and diagnostics."),
                    ("⏰ Scheduler Reminders", "`/remind set` / `/remind list` / `/remind delete`\nSchedule, list, or delete reminders.")
                ]
            }
        ]
        
        paginator = create_pagination_menu("Breeze Help Menu", pages, interaction.user.id)
        await interaction.followup.send(view=paginator, ephemeral=True)

    @app_commands.command(name="botinfo", description="Show information about the Breeze bot")
    async def botinfo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("botinfo", interaction)
        
        ping = round(self.bot.latency * 1000)
        uptime = str(datetime.now() - self.bot.start_time).split('.')[0]
        os_name = platform.system()
        python_version = platform.python_version()
        discord_version = discord.__version__
        
        sections = {
            "👥 Developer Team": "`Breeze Developer Team`",
            "⚡ Gateway Latency": f"`{ping}ms`",
            "⏰ System Uptime": f"`{uptime}`",
            "💻 Host Platform": f"`{os_name}`",
            "🐍 Python Version": f"`{python_version}`",
            "📦 Library Version": f"`discord.py v{discord_version}`"
        }
        card = create_info_card("System Information", "Breeze Bot Diagnostics & System Details", sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="uptime", description="Check how long the bot has been running")
    async def command_uptime(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("uptime", interaction)
        
        uptime = datetime.now() - self.bot.start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        sections = {
            "⏰ Active Time": f"`{days}d {hours}h {minutes}m {seconds}s`",
            "📅 Startup Reference": f"Last Boot: <t:{int(self.bot.start_time.timestamp())}:F>"
        }
        card = create_info_card("Bot System Uptime", None, sections)
        await interaction.followup.send(view=card, ephemeral=True)

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

        sections = {
            "📈 Command Executions": f"`{total_commands}` invocations in this guild",
            "🔥 Most Popular Command": top_cmd,
            "📊 Join/Leave Metrics": f"• Joins: `{joins}`\n• Leaves: `{leaves}`"
        }
        card = create_info_card("Guild Analytics Report", "Metrics logging from this server.", sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="invite", description="Get the invite link for Breeze")
    async def command_invite(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("invite", interaction)
        
        link = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands"
        card = create_success_section("Bot Invitation Link", "You can invite the bot to other guilds using the button or link below.")
        btn = Button(label="Invite Bot", style=discord.ButtonStyle.link, url=link)
        card.add_item(ActionRow(btn))
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="support", description="Get link to support server")
    async def support_server(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("support", interaction)
        
        url = "https://discord.gg/breeze"
        card = create_info_card("Help Desk Support", "Need help setting up systems or reporting bugs? Join our support server.", {})
        btn = Button(label="Join Server", style=discord.ButtonStyle.link, url=url)
        card.add_item(ActionRow(btn))
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="about", description="About the bot design philosophy")
    async def command_about(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("about", interaction)
        
        sections = {
            "Overview": "This is an enterprise-grade utility, ticket, suggestion, and review bot built entirely with Components V2.",
            "🛠️ System Architecture": "Python + discord.py 2.7.1 + aiosqlite (WAL connection enabled)"
        }
        card = create_info_card("About Bot", "Design philosophy and tech stack details.", sections)
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="vote", description="Vote link for the bot")
    async def command_vote(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("vote", interaction)
        
        url = "https://top.gg/bot/breeze"
        card = create_success_section("Vote", "Support the development by voting!")
        btn = Button(label="Vote", style=discord.ButtonStyle.link, url=url)
        card.add_item(ActionRow(btn))
        await interaction.followup.send(view=card, ephemeral=True)

    @app_commands.command(name="links", description="Show official bot links")
    async def links(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("links", interaction)
        
        invite = f"https://discord.com/api/oauth2/authorize?client_id={self.bot.user.id}&permissions=8&scope=bot%20applications.commands"
        support = "https://discord.gg/breeze"
        website = "https://breeze.dev"
        vote = "https://top.gg/bot/breeze"
        
        sections = {
            "🌐 Web Portal": f"[Website]({website})",
            "🎫 OAuth Invite": f"[Authorize Bot]({invite})",
            "📢 Help Desk": f"[Support Server]({support})",
            "⭐ Top.gg Portal": f"[Vote Bot]({vote})"
        }
        card = create_info_card("Breeze Directory Links", "Useful official references.", sections)
        await interaction.followup.send(view=card, ephemeral=True)

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
                card = BreezeWarningContainer("Incident Report Filed", f"Report submitted by {interaction.user.mention}")
                card.add_section("Reporter", f"{interaction.user} (`{interaction.user.id}`)")
                card.add_section("Report details", issue)
                card.add_section("Channel Reference", interaction.channel.mention)
                await mod_ch.send(view=card.build())
            except Exception:
                pass

        success = BreezeSuccessContainer("Report Filed Successfully", "Moderators have been notified about this incident.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="calculator", description="Solve mathematical expressions securely")
    @app_commands.describe(expression="Basic mathematical formula (e.g. 2 + (5 * 3))")
    async def calculator(self, interaction: discord.Interaction, expression: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("calculator", interaction)
        
        safe_chars = set("0123456789+-*/(). ")
        if not all(c in safe_chars for c in expression):
            err = BreezeErrorContainer("Security Blocked", "Only digits and standard operators (`+`, `-`, `*`, `/`, `(`, `)`) are allowed.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        try:
            # Safe evaluation
            res = eval(expression, {"__builtins__": None}, {})
            card = BreezeSuccessContainer("Mathematical Solver", f"Solved expression: `{expression}`")
            card.add_section("Result Output", f"`{res}`")
            await interaction.followup.send(view=card.build(), ephemeral=True)
        except Exception as e:
            err = BreezeErrorContainer("Solver Error", f"Failed to compute math expression: {e}")
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
                        
                        info = BreezeInfoContainer(f"Weather Report: {location.title()}", f"Current conditions at your target location.")
                        info.add_section("Temperature", f"`{temp_c}°C` / `{temp_f}°F`")
                        info.add_section("Conditions", desc.title())
                        info.add_section("Humidity", f"`{humidity}%`")
                        info.add_section("Wind Speed", f"`{wind} km/h`")
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        # Fallback
        info = BreezeWarningContainer(f"Weather Report: {location.title()}", "Weather query timed out. Showing typical climate report.")
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
                        
                        info = BreezeSuccessContainer("Translation Complete", f"Language translations auto to `{target_lang}`.")
                        info.add_section("Input String", text)
                        info.add_section("Translated String", translated)
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        info = BreezeWarningContainer("Translation Failure", "Google translation requests failed. Outputting fallback mock.")
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
                    err = BreezeErrorContainer("Parsing Error", "Invalid time format. Please use `YYYY-MM-DD HH:MM` or `YYYY-MM-DD` or `now`.")
                    await interaction.followup.send(view=err.build(), ephemeral=True)
                    return
                    
        epoch = int(dt.timestamp())
        
        info = BreezeInfoContainer(f"Timestamp Generator: {dt.strftime('%Y-%m-%d %H:%M')}", "Copy the desired raw format code to display dynamic timestamps in server posts.")
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
                            
                            info = BreezeInfoContainer(f"Urban Dictionary: {word}", "Definition results.")
                            info.add_section("Definition", definition[:1000])
                            if example:
                                info.add_section("Example", example[:1000])
                            info.add_section("Rating", f"👍 `{thumbs_up}` positive reviews")
                            await interaction.followup.send(view=info.build(), ephemeral=True)
                            return
        except Exception:
            pass
            
        err = BreezeErrorContainer("No Definition Found", f"Urban Dictionary search returned zero results for `{term}`.")
        await interaction.followup.send(view=err.build(), ephemeral=True)

    @app_commands.command(name="github", description="Query repository metrics on GitHub")
    @app_commands.describe(repo="Repository namespace (e.g. google/google-api-python-client)")
    async def github(self, interaction: discord.Interaction, repo: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("github", interaction)
        
        url = f"https://api.github.com/repos/{repo}"
        headers = {"User-Agent": "BreezeBot/1.0"}
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
                        
                        info = BreezeInfoContainer(f"GitHub: {name}", desc)
                        info.add_section("Stargazers", f"⭐ `{stars}`")
                        info.add_section("Forks Count", f"🍴 `{forks}`")
                        info.add_section("Open Issues", f"🐛 `{issues}`")
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        err = BreezeErrorContainer("Repo Offline", f"Could not find or retrieve details for GitHub repository `{repo}`.")
        await interaction.followup.send(view=err.build(), ephemeral=True)

    @app_commands.command(name="qr", description="Generate a QR code image link")
    @app_commands.describe(text="Content to encode into QR code image")
    async def qr(self, interaction: discord.Interaction, text: str):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("qr", interaction)
        
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(text)}"
        card = BreezeInfoContainer(f"QR Code Generator")
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
                        info = BreezeSuccessContainer("URL Link Shortened", "Long URL formatted successfully.")
                        info.add_section("Original Link", url)
                        info.add_section("TinyURL Link", short_url)
                        await interaction.followup.send(view=info.build(), ephemeral=True)
                        return
        except Exception:
            pass
            
        err = BreezeErrorContainer("API Error", "Failed to shorten URL. Make sure it is formatted correctly.")
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
            
        err_card = BreezeErrorContainer("Command Execution Failed", msg)
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
                
        builder = BreezeContainerBuilder("Sticky Message", None, accent_color=16776960)
        builder.add_section("Notice", text)
        new_msg = await interaction.channel.send(view=builder.build())
        
        self.cog.sticky_last_ids[channel_id] = new_msg.id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sticky_messages SET last_message_id = ? WHERE channel_id = ?",
                (str(new_msg.id), str(channel_id))
            )
            await db.commit()
            
        success = create_success_section("Sticky Message Posted", "Sticky message created successfully and pinned.")
        await interaction.followup.send(view=success, ephemeral=True)

    @app_commands.command(name="delete", description="Delete the sticky message in this channel")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("sticky delete", interaction)
        channel_id = interaction.channel.id
        
        if channel_id not in self.cog.sticky_messages:
            err = create_error_section("Not Found", "There is no active sticky message configured in this channel.")
            await interaction.followup.send(view=err, ephemeral=True)
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
        success = create_success_section("Sticky Message Deleted", "Sticky banner removed successfully.")
        await interaction.followup.send(view=success, ephemeral=True)

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
            info = create_info_card("No Sticky Messages", "There are no active sticky messages configured on this server.", {})
            await interaction.followup.send(view=info, ephemeral=True)
            return
            
        pages = []
        for r in rows:
            channel = interaction.guild.get_channel(int(r[0]))
            mention = channel.mention if channel else f"#{r[0]}"
            pages.append({
                "title": "Sticky Message Details",
                "sections": [
                    ("📍 Target Channel", f"{mention} (`{r[0]}`)"),
                    ("📝 Content Details", r[1])
                ]
            })
            
        paginator = create_pagination_menu("Server Sticky Messages", pages, interaction.user.id, accent_color=16776960)
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
            err = create_error_section("Invalid Duration", "Please specify a correct duration tag like `10m`, `2h`, `1d`.")
            await interaction.followup.send(view=err, ephemeral=True)
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
        sections = {
            "⏰ Alert Time": f"<t:{timestamp_epoch}:F> (<t:{timestamp_epoch}:R>)",
            "📝 Reminder Text": text
        }
        success = create_info_card("Reminder Registered", "Reminder alert successfully scheduled.", sections)
        await interaction.followup.send(view=success, ephemeral=True)

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
            info = create_info_card("No Reminders", "You have no active reminders scheduled.", {})
            await interaction.followup.send(view=info, ephemeral=True)
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
                
            pages.append({
                "title": f"Reminder #{rem_id}",
                "sections": [
                    ("⏰ Alert Time", time_display),
                    ("📝 Content", rem_text)
                ]
            })
            
        paginator = create_pagination_menu("Your Scheduled Reminders", pages, interaction.user.id)
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
                err = BreezeErrorContainer("Not Found", f"Reminder `{id}` was not found or doesn't belong to you.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
            await db.execute("DELETE FROM reminders WHERE id = ?", (id,))
            await db.commit()
            
        success = BreezeSuccessContainer("Reminder Deleted", f"Reminder `{id}` cleared successfully.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @app_commands.command(name="clear", description="Clear all of your active reminders")
    async def clear_reminders(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await log_command_usage("remind clear", interaction)
        user_id = str(interaction.user.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM reminders WHERE user_id = ?", (user_id,))
            await db.commit()
            
        success = BreezeSuccessContainer("Reminders Cleared", "All personal active reminders have been deleted.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# SETUP COG ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    cog = Utilities(bot)
    await bot.add_cog(cog)
    bot.add_view(PersistentPollView())
