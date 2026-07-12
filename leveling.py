import re
import random
import logging
from datetime import datetime
from typing import Optional, List, Dict, Tuple

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks

from ui import (
    BreezeContainerBuilder,
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeWarningContainer,
    BreezeInfoContainer
)

logger = logging.getLogger("Breeze.Leveling")
DB_PATH = "fb.db"

class Leveling(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # Memory caches
        self.settings_cache: Dict[str, dict] = {}      # guild_id -> settings dict
        self.cooldown_cache: Dict[Tuple[str, str], datetime] = {}  # (guild_id, user_id) -> datetime
        self.user_cache: Dict[Tuple[str, str], dict] = {}          # (guild_id, user_id) -> data dict
        self.dirty_users: set = set()                  # set of (guild_id, user_id) to flush

    async def cog_load(self):
        # Initialize database tables
        await self.init_db()
        # Start periodic flushing loop
        self.flush_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        logger.info("[Leveling] Cog unloading. Flushing final buffered leveling updates...")
        await self.do_flush()

    async def init_db(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            
            # Create levels table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS levels (
                    guild_id TEXT,
                    user_id TEXT,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 0,
                    total_messages INTEGER DEFAULT 0,
                    last_message_at TEXT,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            
            # Create level_settings table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS level_settings (
                    guild_id TEXT PRIMARY KEY,
                    enabled INTEGER DEFAULT 1,
                    announcement_channel_id TEXT,
                    xp_min INTEGER DEFAULT 15,
                    xp_max INTEGER DEFAULT 25,
                    cooldown_seconds INTEGER DEFAULT 60
                )
            """)
            
            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_levels_leaderboard ON levels (guild_id, level DESC, xp DESC)")
            
            await db.commit()
        logger.info("[Leveling] Database tables and indexes checked/initialized in fb.db.")

    async def get_settings(self, guild_id: str) -> dict:
        if guild_id in self.settings_cache:
            return self.settings_cache[guild_id]
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT enabled, announcement_channel_id, xp_min, xp_max, cooldown_seconds 
                FROM level_settings 
                WHERE guild_id = ?
            """, (guild_id,)) as c:
                row = await c.fetchone()
                if row:
                    settings = {
                        "enabled": bool(row[0]),
                        "announcement_channel_id": row[1],
                        "xp_min": row[2],
                        "xp_max": row[3],
                        "cooldown_seconds": row[4]
                    }
                else:
                    # Default settings
                    settings = {
                        "enabled": True,
                        "announcement_channel_id": None,
                        "xp_min": 15,
                        "xp_max": 25,
                        "cooldown_seconds": 60
                    }
                    await db.execute("""
                        INSERT OR IGNORE INTO level_settings (guild_id, enabled, announcement_channel_id, xp_min, xp_max, cooldown_seconds)
                        VALUES (?, 1, NULL, 15, 25, 60)
                    """, (guild_id,))
                    await db.commit()
        
        self.settings_cache[guild_id] = settings
        return settings

    async def get_user_data(self, guild_id: str, user_id: str) -> dict:
        key = (guild_id, user_id)
        if key in self.user_cache:
            return self.user_cache[key]
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT xp, level, total_messages 
                FROM levels 
                WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id)) as c:
                row = await c.fetchone()
                if row:
                    data = {
                        "xp": row[0],
                        "level": row[1],
                        "total_messages": row[2]
                    }
                else:
                    data = {
                        "xp": 0,
                        "level": 0,
                        "total_messages": 0
                    }
        self.user_cache[key] = data
        return data

    @tasks.loop(seconds=30)
    async def flush_loop(self):
        try:
            await self.do_flush()
        except Exception as e:
            logger.error(f"[Leveling] Exception during database flush: {e}")

    async def do_flush(self):
        dirty_keys = list(self.dirty_users)
        self.dirty_users.clear()
        
        if not dirty_keys:
            return
            
        logger.debug(f"[Leveling] Flushing {len(dirty_keys)} user levels records...")
        
        async with aiosqlite.connect(DB_PATH) as db:
            for key in dirty_keys:
                data = self.user_cache.get(key)
                if not data:
                    continue
                guild_id, user_id = key
                await db.execute("""
                    INSERT INTO levels (guild_id, user_id, xp, level, total_messages, last_message_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    xp = excluded.xp,
                    level = excluded.level,
                    total_messages = excluded.total_messages,
                    last_message_at = excluded.last_message_at
                """, (
                    guild_id, 
                    user_id, 
                    data["xp"], 
                    data["level"], 
                    data["total_messages"], 
                    datetime.utcnow().isoformat()
                ))
            await db.commit()
        logger.info(f"[Leveling] Committed {len(dirty_keys)} buffered leveling logs to SQLite.")

    # ══════════════════════════════════════════════════════════════════════
    # ON MESSAGE EVENT LISTENER (XP GAIN)
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DM messages
        if not message.guild:
            return
            
        # Ignore Bots
        if message.author.bot:
            return
            
        # Ignore Webhooks
        if message.webhook_id is not None:
            return
            
        # Ignore System Messages
        if message.system or message.type != discord.MessageType.default:
            return

        guild_id = str(message.guild.id)
        user_id = str(message.author.id)

        # Get settings
        settings = await self.get_settings(guild_id)
        if not settings["enabled"]:
            return

        # Cooldown check
        now = datetime.utcnow()
        cooldown_key = (guild_id, user_id)
        if cooldown_key in self.cooldown_cache:
            elapsed = (now - self.cooldown_cache[cooldown_key]).total_seconds()
            if elapsed < settings["cooldown_seconds"]:
                return

        # Update cooldown cache
        self.cooldown_cache[cooldown_key] = now

        # Retrieve/update user data
        cache_data = await self.get_user_data(guild_id, user_id)
        xp_gained = random.randint(settings["xp_min"], settings["xp_max"])
        
        cache_data["xp"] += xp_gained
        cache_data["total_messages"] += 1
        
        old_level = cache_data["level"]
        new_level = old_level
        temp_xp = cache_data["xp"]

        # Level formula: 5 * level^2 + 50 * level + 100
        while True:
            needed = 5 * new_level**2 + 50 * new_level + 100
            if temp_xp >= needed:
                temp_xp -= needed
                new_level += 1
            else:
                break

        cache_data["xp"] = temp_xp
        cache_data["level"] = new_level
        self.dirty_users.add(cooldown_key)

        if new_level > old_level:
            # Commit instantly to database on level-up for data safety and correct leaderboard display
            await self.do_flush()
            # Trigger Level Up Announcement
            await self.handle_level_up(message, new_level, settings)

    async def handle_level_up(self, message: discord.Message, new_level: int, settings: dict):
        ann_channel_id = settings.get("announcement_channel_id")
        if not ann_channel_id:
            return

        channel = message.guild.get_channel(int(ann_channel_id))
        if not channel:
            try:
                channel = await message.guild.fetch_channel(int(ann_channel_id))
            except Exception:
                return  # Could not fetch/find channel, bot has no access or it was deleted

        # Components V2 Announcement message
        builder = BreezeContainerBuilder(
            title="🎉 Level Up!",
            description=f"Congratulations {message.author.mention}!",
            accent_color=3066993,
            thumbnail_url=message.author.display_avatar.url if message.author.display_avatar else None
        )
        builder.add_section("Rank Progress", f"You reached **Level {new_level}**.")
        builder.add_separator()
        builder.add_text("Keep chatting to earn more XP!")

        try:
            await channel.send(view=builder.build())
        except Exception as e:
            logger.error(f"[Leveling] Failed to send level up message in channel {ann_channel_id}: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # COG APP COMMAND ERROR HANDLER
    # ══════════════════════════════════════════════════════════════════════

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.exception(f"Error in slash command inside Leveling: {error}")
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
    # USER SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="rank", description="Shows the rank status and leveling details of a member")
    @app_commands.describe(member="The member to view")
    async def rank(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        
        target = member or interaction.user
        if target.bot:
            err = BreezeErrorContainer("Invalid User", "Bots do not earn XP or have ranks.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        user_id = str(target.id)

        # Flush dirty state if this specific user has pending changes
        if (guild_id, user_id) in self.dirty_users:
            await self.do_flush()

        data = await self.get_user_data(guild_id, user_id)
        
        xp = data["xp"]
        level = data["level"]
        total_messages = data["total_messages"]
        xp_needed = 5 * level**2 + 50 * level + 100

        builder = BreezeContainerBuilder(
            title="⭐ Rank Details",
            description=f"Leveling statistics for {target.mention}",
            accent_color=3447003,
            thumbnail_url=target.display_avatar.url if target.display_avatar else None
        )
        builder.add_section("Level Status", f"Level **{level}**")
        builder.add_separator()
        builder.add_section("XP Progress", f"**{xp} / {xp_needed} XP**")
        builder.add_separator()
        builder.add_section("Total Messages", f"**{total_messages}** messages")

        await interaction.followup.send(view=builder.build(), ephemeral=True)

    @app_commands.command(name="leaderboard", description="Shows the top 10 highest-level users in the server")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        # Flush all pending updates so leaderboard displays up-to-date values
        await self.do_flush()

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id, level, xp, total_messages 
                FROM levels 
                WHERE guild_id = ? 
                ORDER BY level DESC, xp DESC 
                LIMIT 10
            """, (guild_id,)) as c:
                rows = await c.fetchall()

        if not rows:
            info = BreezeInfoContainer("Leaderboard", "No users have earned XP yet.")
            await interaction.followup.send(view=info.build(), ephemeral=True)
            return

        leaderboard_lines = []
        for rank, row in enumerate(rows, 1):
            user_id_str, level, xp, total_messages = row
            member = interaction.guild.get_member(int(user_id_str))
            
            if member:
                user_mention = member.mention
            else:
                try:
                    user = await self.bot.fetch_user(int(user_id_str))
                    user_mention = user.mention
                except Exception:
                    user_mention = f"User ID: {user_id_str}"
            
            leaderboard_lines.append(f"**#{rank}** {user_mention} — **Level {level}** ({xp} XP) — `{total_messages}` msgs")

        leaderboard_text = "\n".join(leaderboard_lines)

        builder = BreezeContainerBuilder(
            title="🏆 Server Leaderboard",
            description=f"Top 10 highest-level users in **{interaction.guild.name}**",
            accent_color=3447003
        )
        builder.add_section("Rankings", leaderboard_text)

        await interaction.followup.send(view=builder.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # ADMIN CONFIG SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    level = app_commands.Group(name="level", description="Breeze Leveling configuration and settings")

    @level.command(name="setup", description="Sets the level-up announcement channel")
    @app_commands.describe(channel="The channel where level-up notifications will be announced")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_setup(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        channel_id = str(channel.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO level_settings (guild_id, announcement_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET announcement_channel_id = ?
            """, (guild_id, channel_id, channel_id))
            await db.commit()

        # Evict cache
        self.settings_cache.pop(guild_id, None)

        card = BreezeSuccessContainer(
            title="Setup Completed",
            description=f"Level-up announcements have been set to {channel.mention}."
        )
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @level.command(name="disable", description="Disables level-up announcements")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO level_settings (guild_id, announcement_channel_id)
                VALUES (?, NULL)
                ON CONFLICT(guild_id) DO UPDATE SET announcement_channel_id = NULL
            """, (guild_id,))
            await db.commit()

        # Evict cache
        self.settings_cache.pop(guild_id, None)

        card = BreezeSuccessContainer(
            title="Announcements Disabled",
            description="Level-up announcements have been disabled."
        )
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @level.command(name="enable", description="Enables the leveling system")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_enable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO level_settings (guild_id, enabled)
                VALUES (?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET enabled = 1
            """, (guild_id,))
            await db.commit()

        # Evict cache
        self.settings_cache.pop(guild_id, None)

        card = BreezeSuccessContainer(
            title="System Enabled",
            description="The leveling system has been enabled for this server."
        )
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @level.command(name="settings", description="Displays current leveling and settings configuration")
    @app_commands.checks.has_permissions(administrator=True)
    async def level_settings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        settings = await self.get_settings(guild_id)
        
        enabled_str = "🟢 Enabled" if settings["enabled"] else "🔴 Disabled"
        xp_range = f"**{settings['xp_min']} - {settings['xp_max']}** XP per message"
        cooldown = f"**{settings['cooldown_seconds']} seconds**"
        
        ann_channel_id = settings["announcement_channel_id"]
        announcement_channel = f"<#{ann_channel_id}>" if ann_channel_id else "❌ Disabled"

        builder = BreezeContainerBuilder(
            title="⚙️ Leveling Settings",
            description=f"Configuration for **{interaction.guild.name}**",
            accent_color=3447003
        )
        builder.add_section("Enabled Status", enabled_str)
        builder.add_separator()
        builder.add_section("XP Range", xp_range)
        builder.add_separator()
        builder.add_section("Cooldown Duration", cooldown)
        builder.add_separator()
        builder.add_section("Announcement Channel", announcement_channel)

        await interaction.followup.send(view=builder.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Leveling(bot))
