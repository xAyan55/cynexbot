import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands, tasks

from ui import (
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeInfoContainer,
    BreezePaginationContainer
)

logger = logging.getLogger("Breeze.MessageTracker")
DB_PATH = "breeze.db"

# ══════════════════════════════════════════════════════════════════════
# MEMORY BUFFER FOR WRITES BATCHING
# ══════════════════════════════════════════════════════════════════════

class MessageTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings_cache = {}  # guild_id -> dict
        
        # Buffer structures
        self.activity_buffer = {}  # (guild_id, user_id) -> dict
        self.daily_buffer = {}     # (guild_id, user_id, date_str) -> count

    async def cog_load(self):
        self.flush_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        logger.info("[MessageTracker] Cog unloading. Flushing final buffered message logs...")
        await self.do_flush()

    async def get_settings(self, guild_id: str) -> dict:
        if guild_id in self.settings_cache:
            return self.settings_cache[guild_id]
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT ignore_bots FROM message_settings WHERE guild_id = ?", (guild_id,)) as c:
                row = await c.fetchone()
                if row:
                    settings = {"ignore_bots": row[0]}
                else:
                    settings = {"ignore_bots": 1}
                    await db.execute("INSERT OR IGNORE INTO message_settings (guild_id) VALUES (?)", (guild_id,))
                    await db.commit()
        
        self.settings_cache[guild_id] = settings
        return settings

    # ══════════════════════════════════════════════════════════════════════
    # FLUSH LOGIC
    # ══════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=30)
    async def flush_loop(self):
        try:
            await self.do_flush()
        except Exception as e:
            logger.error(f"[MessageTracker] Exception during database flush: {e}")

    async def do_flush(self):
        # Swap buffers to avoid thread-safety/concurrency issues during SQL tasks
        act_flush = self.activity_buffer
        self.activity_buffer = {}

        daily_flush = self.daily_buffer
        self.daily_buffer = {}

        if not act_flush and not daily_flush:
            return

        logger.debug(f"[MessageTracker] Flushing {len(act_flush)} activity and {len(daily_flush)} daily log records...")

        async with aiosqlite.connect(DB_PATH) as db:
            # Flush activity buffer
            for (guild_id, user_id), data in act_flush.items():
                date_str = datetime.utcnow().strftime("%Y-%m-%d")
                await db.execute("""
                    INSERT INTO message_activity (
                        guild_id, user_id, total_messages, attachments_count, 
                        images_count, links_count, voice_messages_count, last_active_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    total_messages = total_messages + excluded.total_messages,
                    attachments_count = attachments_count + excluded.attachments_count,
                    images_count = images_count + excluded.images_count,
                    links_count = links_count + excluded.links_count,
                    voice_messages_count = voice_messages_count + excluded.voice_messages_count,
                    last_active_date = excluded.last_active_date
                """, (
                    guild_id, user_id, data["messages"], data["attachments"],
                    data["images"], data["links"], data["voice"], date_str
                ))

            # Flush daily stats buffer
            for (guild_id, user_id, date_str), count in daily_flush.items():
                await db.execute("""
                    INSERT INTO message_daily_stats (guild_id, user_id, date, count)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id, date) DO UPDATE SET
                    count = count + excluded.count
                """, (guild_id, user_id, date_str, count))

            await db.commit()

        logger.info(f"[MessageTracker] Successfully committed batched message statistics to SQLite.")

    # ══════════════════════════════════════════════════════════════════════
    # ON MESSAGE EVENT LISTENER
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild:
            return

        guild_id = str(message.guild.id)
        user_id = str(message.author.id)

        # Check bot ignore config
        if message.author.bot:
            settings = await self.get_settings(guild_id)
            if settings["ignore_bots"]:
                return

        # Calculate metrics
        msg_count = 1
        attachments = len(message.attachments)
        images = 0
        links = 0
        voice = 0

        # Check images in attachments
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                images += 1
            elif att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".tiff")):
                images += 1

        # Check links
        link_matches = re.findall(r"https?://[^\s]+", message.content)
        links = len(link_matches)

        # Check voice messages
        if hasattr(message.flags, "voice") and message.flags.voice:
            voice = 1
        elif message.flags.value & 8192:
            voice = 1

        # 1. Update activity buffer
        act_key = (guild_id, user_id)
        if act_key not in self.activity_buffer:
            self.activity_buffer[act_key] = {"messages": 0, "attachments": 0, "images": 0, "links": 0, "voice": 0}
        
        self.activity_buffer[act_key]["messages"] += msg_count
        self.activity_buffer[act_key]["attachments"] += attachments
        self.activity_buffer[act_key]["images"] += images
        self.activity_buffer[act_key]["links"] += links
        self.activity_buffer[act_key]["voice"] += voice

        # 2. Update daily statistics buffer
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        daily_key = (guild_id, user_id, date_str)
        self.daily_buffer[daily_key] = self.daily_buffer.get(daily_key, 0) + 1

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    messages = app_commands.Group(name="messages", description="Breeze Server Activity tracking settings and statistics")

    @messages.command(name="stats", description="View your server message activity statistics")
    async def messages_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)

        # First flush buffers to make sure stats are up to date when viewing
        await self.do_flush()

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        week_str = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        month_str = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT total_messages, attachments_count, images_count, links_count, voice_messages_count, last_active_date 
                FROM message_activity WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id)) as c:
                row = await c.fetchone()

            # Dynamic date range queries
            async with db.execute("SELECT COUNT(*) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND count > 0", (guild_id, user_id)) as c:
                active_days = await c.fetchone()[0]

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date = ?", (guild_id, user_id, today_str)) as c:
                today_row = await c.fetchone()
                today_count = today_row[0] if today_row and today_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date >= ?", (guild_id, user_id, week_str)) as c:
                week_row = await c.fetchone()
                week_count = week_row[0] if week_row and week_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date >= ?", (guild_id, user_id, month_str)) as c:
                month_row = await c.fetchone()
                month_count = month_row[0] if month_row and month_row[0] else 0

        if row:
            total, atts, imgs, lnks, vcs, last_active = row
        else:
            total, atts, imgs, lnks, vcs, last_active = 0, 0, 0, 0, 0, "Never"

        desc = (
            f"**📊 Message Breakdown:**\n"
            f"• **Total Sent**: `{total}` messages\n"
            f"• **Attachments**: `{atts}` files\n"
            f"• **Images**: `{imgs}` pictures\n"
            f"• **Links**: `{lnks}` links\n"
            f"• **Voice Messages**: `{vcs}` voice notes\n\n"
            f"**🕒 Activity Timeline:**\n"
            f"• **Today**: `{today_count}` messages\n"
            f"• **Past 7 Days**: `{week_count}` messages\n"
            f"• **Past 30 Days**: `{month_count}` messages\n"
            f"• **Active Chatting Days**: `{active_days}` days\n"
            f"• **Last Active**: `{last_active}`"
        )
        card = BreezeInfoContainer(f"Activity Statistics for {interaction.user.name}", desc)
        await interaction.followup.send(view=card.build())

    @messages.command(name="user", description="View message activity statistics for a specific member")
    async def messages_user(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        await self.do_flush()

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        week_str = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        month_str = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT total_messages, attachments_count, images_count, links_count, voice_messages_count, last_active_date 
                FROM message_activity WHERE guild_id = ? AND user_id = ?
            """, (guild_id, user_id)) as c:
                row = await c.fetchone()

            async with db.execute("SELECT COUNT(*) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND count > 0", (guild_id, user_id)) as c:
                active_days = await c.fetchone()[0]

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date = ?", (guild_id, user_id, today_str)) as c:
                today_row = await c.fetchone()
                today_count = today_row[0] if today_row and today_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date >= ?", (guild_id, user_id, week_str)) as c:
                week_row = await c.fetchone()
                week_count = week_row[0] if week_row and week_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND user_id = ? AND date >= ?", (guild_id, user_id, month_str)) as c:
                month_row = await c.fetchone()
                month_count = month_row[0] if month_row and month_row[0] else 0

        if row:
            total, atts, imgs, lnks, vcs, last_active = row
        else:
            total, atts, imgs, lnks, vcs, last_active = 0, 0, 0, 0, 0, "Never"

        desc = (
            f"**📊 Message Breakdown:**\n"
            f"• **Total Sent**: `{total}` messages\n"
            f"• **Attachments**: `{atts}` files\n"
            f"• **Images**: `{imgs}` pictures\n"
            f"• **Links**: `{lnks}` links\n"
            f"• **Voice Messages**: `{vcs}` voice notes\n\n"
            f"**🕒 Activity Timeline:**\n"
            f"• **Today**: `{today_count}` messages\n"
            f"• **Past 7 Days**: `{week_count}` messages\n"
            f"• **Past 30 Days**: `{month_count}` messages\n"
            f"• **Active Chatting Days**: `{active_days}` days\n"
            f"• **Last Active**: `{last_active}`"
        )
        card = BreezeInfoContainer(f"Activity Statistics for {member.name}", desc)
        await interaction.followup.send(view=card.build())

    @messages.command(name="leaderboard", description="Display the server's most active members")
    async def messages_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)

        await self.do_flush()

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id, total_messages, attachments_count, images_count 
                FROM message_activity 
                WHERE guild_id = ? 
                ORDER BY total_messages DESC
            """, (guild_id,)) as c:
                rows = await c.fetchall()

        if not rows:
            card = BreezeInfoContainer("Chat Activity Leaderboard", "*No activity statistics found on this server.*")
            await interaction.followup.send(view=card.build())
            return

        pages = []
        page_size = 4
        for i in range(0, len(rows), page_size):
            chunk = rows[i:i + page_size]
            page_sections = []
            for offset, row in enumerate(chunk):
                idx = i + offset + 1
                user_id, total, atts, imgs = row
                member_obj = interaction.guild.get_member(int(user_id))
                user_str = member_obj.mention if member_obj else f"User ID: {user_id}"
                
                sec_title = f"💬 Chat Rank #{idx}: {member_obj.display_name if member_obj else user_id}"
                sec_desc = f"• **Member:** {user_str}\n• **Total Messages:** `{total}`\n• **Details:** `{atts}` files, `{imgs}` pictures"
                page_sections.append((sec_title, sec_desc))
            pages.append({
                "title": "Server Chat Activity Leaderboard",
                "description": f"Page {i//page_size + 1} of {(len(rows) - 1)//page_size + 1}",
                "sections": page_sections
            })

        paginator = BreezePaginationContainer("Server Chat Activity Leaderboard", pages, interaction.user.id)
        await interaction.followup.send(view=paginator)

    @messages.command(name="activity", description="Display aggregated message counts sent on the server recently")
    async def messages_activity_agg(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)

        await self.do_flush()

        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        week_str = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        month_str = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

        async with aiosqlite.connect(DB_PATH) as db:
            # Query sums across all users
            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND date = ?", (guild_id, today_str)) as c:
                today_row = await c.fetchone()
                today_sum = today_row[0] if today_row and today_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND date >= ?", (guild_id, week_str)) as c:
                week_row = await c.fetchone()
                week_sum = week_row[0] if week_row and week_row[0] else 0

            async with db.execute("SELECT SUM(count) FROM message_daily_stats WHERE guild_id = ? AND date >= ?", (guild_id, month_str)) as c:
                month_row = await c.fetchone()
                month_sum = month_row[0] if month_row and month_row[0] else 0

            async with db.execute("SELECT SUM(total_messages) FROM message_activity WHERE guild_id = ?", (guild_id,)) as c:
                total_row = await c.fetchone()
                total_sum = total_row[0] if total_row and total_row[0] else 0

        desc = (
            f"📈 **Server Chat Volume Overview:**\n"
            f"• **Today's Messages**: `{today_sum}` messages\n"
            f"• **This Week (Past 7 Days)**: `{week_sum}` messages\n"
            f"• **This Month (Past 30 Days)**: `{month_sum}` messages\n"
            f"• **Historical Total messages**: `{total_sum}` messages"
        )
        card = BreezeInfoContainer(f"Server Aggregate Activity Stats", desc)
        await interaction.followup.send(view=card.build())

    @messages.command(name="botignore", description="Configure whether bot messages are ignored in statistics")
    @app_commands.checks.has_permissions(administrator=True)
    async def messages_botignore(self, interaction: discord.Interaction, ignore: bool):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        val = 1 if ignore else 0

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO message_settings (guild_id, ignore_bots) VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET ignore_bots = ?
            """, (guild_id, val, val))
            await db.commit()

        self.settings_cache.pop(guild_id, None)

        status_str = "ignored" if ignore else "counted"
        card = BreezeSuccessContainer("Settings Updated", f"Bot messages will now be **{status_str}** in chat activity statistics.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @messages.command(name="reset", description="Reset chat statistics for a specific member")
    @app_commands.checks.has_permissions(administrator=True)
    async def messages_reset(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        # Clear buffers
        self.activity_buffer.pop((guild_id, user_id), None)
        # remove daily buffer matches
        to_del = [k for k in self.daily_buffer if k[0] == guild_id and k[1] == user_id]
        for k in to_del:
            self.daily_buffer.pop(k, None)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM message_activity WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            await db.execute("DELETE FROM message_daily_stats WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            await db.commit()

        card = BreezeSuccessContainer("User Reset Successful", f"Cleared all chat activity history records for {member.mention}.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @messages.command(name="resetall", description="Reset all chat statistics for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def messages_resetall(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        # Clear buffers
        self.activity_buffer = {k: v for k, v in self.activity_buffer.items() if k[0] != guild_id}
        self.daily_buffer = {k: v for k, v in self.daily_buffer.items() if k[0] != guild_id}

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM message_activity WHERE guild_id = ?", (guild_id,))
            await db.execute("DELETE FROM message_daily_stats WHERE guild_id = ?", (guild_id,))
            await db.commit()

        card = BreezeSuccessContainer("Server Reset Successful", "🗑️ Successfully cleared all server chat activity statistics and historical charts.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(MessageTracker(bot))
