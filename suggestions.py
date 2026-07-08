import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Union

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands
from discord.ui import (
    LayoutView,
    Container,
    TextDisplay,
    Separator,
    Section,
    ActionRow,
    Button
)

import ui
from ui import (
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudWarningContainer,
    CynexCloudInfoContainer,
    CynexCloudPaginationContainer,
    CynexCloudContainerBuilder
)

logger = logging.getLogger("CynexCloud.Suggestions")
DB_PATH = "cynex.db"

# ══════════════════════════════════════════════════════════════════════
# SUGGESTION MODAL FORM
# ══════════════════════════════════════════════════════════════════════

class SuggestionSubmitModal(discord.ui.Modal, title="Submit a Suggestion"):
    s_title = discord.ui.TextInput(
        label="Suggestion Title",
        placeholder="Brief summary of your suggestion...",
        max_length=80,
        required=True
    )
    s_desc = discord.ui.TextInput(
        label="Detailed Description",
        style=discord.TextStyle.paragraph,
        placeholder="Provide explanation, details, and rationale...",
        min_length=10,
        max_length=1500,
        required=True
    )

    def __init__(self, category: str, anonymous: bool, settings: dict):
        super().__init__()
        self.category = category
        self.anonymous = anonymous
        self.settings = settings

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        guild_id = str(interaction.guild.id)
        user_id = str(interaction.user.id)
        title_text = self.s_title.value.strip()
        desc_text = self.s_desc.value.strip()

        # Check for duplicate suggestion title in the last hour to prevent spam
        now = datetime.now()
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT created_at FROM suggestions WHERE user_id = ? AND guild_id = ? AND title = ? ORDER BY created_at DESC LIMIT 1",
                (user_id, guild_id, title_text)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    try:
                        dt = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                        if (now - dt).total_seconds() < 3600:
                            warn = CynexCloudWarningContainer("Duplicate Suggestion Detected", "You have already submitted a suggestion with this title in the last hour.")
                            await interaction.followup.send(view=warn.build(), ephemeral=True)
                            return
                    except Exception:
                        pass

            # Generate Suggestion ID
            async with db.execute("SELECT COUNT(*) FROM suggestions") as cursor:
                count_row = await cursor.fetchone()
                s_count = count_row[0] if count_row else 0
            suggestion_id = f"S-{1001 + s_count}"

            # Save suggestion
            await db.execute(
                "INSERT INTO suggestions (suggestion_id, guild_id, user_id, title, description, category, status, anonymous) VALUES (?, ?, ?, ?, ?, ?, 'approved', ?)",
                (suggestion_id, guild_id, user_id, title_text, desc_text, self.category, 1 if self.anonymous else 0)
            )
            # Log command usage
            await db.execute(
                "INSERT INTO command_usage (command_name, user_id, guild_id) VALUES ('suggest', ?, ?)",
                (user_id, guild_id)
            )
            await db.commit()

        # Post immediately to suggest channel and create thread
        suggest_channel = None
        sug_channel_id = self.settings.get("suggest_channel_id")
        if sug_channel_id:
            try:
                suggest_channel = interaction.guild.get_channel(int(sug_channel_id))
                if not suggest_channel:
                    suggest_channel = await interaction.guild.fetch_channel(int(sug_channel_id))
            except Exception as e:
                logger.warning(f"Could not get/fetch suggestions channel {sug_channel_id}: {e}")

        if suggest_channel:
            try:
                # Build V2 Container (Approved - Green)
                pub_layout = CynexCloudContainerBuilder(f"Suggestion: {title_text}", accent_color=3066993) # Green
                pub_layout.add_section("Category", f"`{self.category}`")
                pub_layout.add_section("Description", desc_text)
                pub_layout.add_section("Status", "🟢 **Approved**")
                pub_layout.add_section("Author", "Anonymous" if self.anonymous else f"{interaction.user.mention}")
                pub_layout.add_section("Suggestion ID", f"`{suggestion_id}`")
                
                # Upvote / Downvote buttons with initial counts 0
                btn_up = Button(label="👍 Upvote (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:up:{suggestion_id}")
                btn_down = Button(label="👎 Downvote (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:down:{suggestion_id}")
                btn_stats = Button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:stats:{suggestion_id}")
                pub_layout.add_buttons(btn_up, btn_down, btn_stats)
                
                msg = await suggest_channel.send(view=pub_layout.build())
                
                # Create discussion thread automatically
                thread = await msg.create_thread(name=f"Discussion: {title_text[:50]}", auto_archive_duration=1440)
                await thread.send(f"🟢 **Discussion thread for suggestion `{suggestion_id}` has been created.**")
                
                # Save thread_id to database
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE suggestions SET thread_id = ? WHERE suggestion_id = ?", (str(thread.id), suggestion_id))
                    await db.commit()
            except Exception as pub_err:
                logger.warning(f"Failed to post suggestion or create thread: {pub_err}")

        success = CynexCloudSuccessContainer("Suggestion Submitted", f"Your suggestion `{suggestion_id}` has been published to {suggest_channel.mention if suggest_channel else 'the suggestion channel'}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# SUGGESTIONS COG EXTENSION
# ══════════════════════════════════════════════════════════════════════

class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.init_db()

    async def init_db(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS suggestions (
                    suggestion_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    notes TEXT,
                    thread_id TEXT,
                    anonymous INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS suggestion_votes (
                    suggestion_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    vote_value INTEGER NOT NULL,
                    PRIMARY KEY (suggestion_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS suggestion_settings (
                    guild_id TEXT PRIMARY KEY,
                    suggest_channel_id TEXT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild_status ON suggestions (guild_id, status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_suggestion_votes_suggestion ON suggestion_votes (suggestion_id)")
            await db.commit()

    async def get_settings(self, guild_id: str) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT suggest_channel_id FROM suggestion_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "suggest_channel_id": row[0]
                    }
        return None

    # ══════════════════════════════════════════════════════════════════════
    # SUGGESTION VOTE & INTERACTION CALLBACK ROUTER
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id") if interaction.data else None
        if not custom_id or not custom_id.startswith("cynexcloud:suggest:"):
            return

        parts = custom_id.split(":")
        action = parts[2]

        # Voting flow
        if action == "vote":
            await interaction.response.defer(ephemeral=True)
            vote_dir = parts[3] # up or down
            suggestion_id = parts[4]
            user_id = str(interaction.user.id)
            val = 1 if vote_dir == "up" else -1

            async with aiosqlite.connect(DB_PATH) as db:
                # Retrieve current status to ensure votes are active
                async with db.execute("SELECT status, user_id FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)) as cursor:
                    status_row = await cursor.fetchone()
                    
                if not status_row:
                    err = CynexCloudErrorContainer("Suggestion Missing", "The suggestion data could not be found.")
                    await interaction.followup.send(view=err.build(), ephemeral=True)
                    return
                    
                status, author_id = status_row
                if status in ("implemented", "denied"):
                    err = CynexCloudErrorContainer("Voting Closed", "This suggestion is already implemented or denied. Voting is disabled.")
                    await interaction.followup.send(view=err.build(), ephemeral=True)
                    return

                # Prevent self-voting
                if author_id == user_id:
                    err = CynexCloudErrorContainer("Self Voting Blocked", "You cannot vote on your own suggestions.")
                    await interaction.followup.send(view=err.build(), ephemeral=True)
                    return

                async with db.execute(
                    "SELECT vote_value FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?",
                    (suggestion_id, user_id)
                ) as cursor:
                    row = await cursor.fetchone()

                if row:
                    current_val = row[0]
                    if current_val == val:
                        # Toggle / Remove vote
                        await db.execute("DELETE FROM suggestion_votes WHERE suggestion_id = ? AND user_id = ?", (suggestion_id, user_id))
                        msg_action = "removed"
                    else:
                        # Update / Change vote
                        await db.execute(
                            "UPDATE suggestion_votes SET vote_value = ? WHERE suggestion_id = ? AND user_id = ?",
                            (val, suggestion_id, user_id)
                        )
                        msg_action = f"updated to {vote_dir}vote"
                else:
                    # New vote
                    await db.execute(
                        "INSERT INTO suggestion_votes (suggestion_id, user_id, vote_value) VALUES (?, ?, ?)",
                        (suggestion_id, user_id, val)
                    )
                    msg_action = f"registered as {vote_dir}vote"
                await db.commit()

                # Get Tallies
                async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = 1", (suggestion_id,)) as cursor:
                    up_row = await cursor.fetchone()
                    upvotes = up_row[0] if up_row else 0
                async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = -1", (suggestion_id,)) as cursor:
                    down_row = await cursor.fetchone()
                    downvotes = down_row[0] if down_row else 0

            # Rebuild and edit layout view in Suggestion Channel
            try:
                message = interaction.message
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT title, description, category, anonymous, user_id, status, notes FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)) as cursor:
                        s_data = await cursor.fetchone()
                        
                if s_data:
                    title, desc, category, anon, author_uid, status_val, notes = s_data
                    
                    # Accent color logic based on status
                    accent_col = 3447003 # Info Blue
                    if status_val == "approved":
                        accent_col = 3066993 # Green
                        badge = "🟢 **Approved**"
                    elif status_val == "implemented":
                        accent_col = 13937975 # Gold
                        badge = "🔵 **Implemented**"
                    elif status_val == "denied":
                        accent_col = 15158332 # Red
                        badge = "🔴 **Denied**"
                    else:
                        badge = "⏳ **Pending Moderation**"
                        
                    pub_layout = CynexCloudContainerBuilder(f"Suggestion: {title}", accent_color=accent_col)
                    pub_layout.add_section("Category", f"`{category}`")
                    pub_layout.add_section("Description", desc)
                    pub_layout.add_section("Status", badge)
                    if notes:
                        pub_layout.add_section("Developer Notes", notes)
                    pub_layout.add_section("Author", "Anonymous" if anon else f"<@{author_uid}>")
                    pub_layout.add_section("Suggestion ID", f"`{suggestion_id}`")
                    
                    btn_up = Button(label=f"👍 Upvote ({upvotes})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:up:{suggestion_id}")
                    btn_down = Button(label=f"👎 Downvote ({downvotes})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:down:{suggestion_id}")
                    btn_stats = Button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:stats:{suggestion_id}")
                    pub_layout.add_buttons(btn_up, btn_down, btn_stats)
                    
                    await message.edit(view=pub_layout.build())
            except Exception as edit_err:
                logger.warning(f"Failed to edit suggestion votes button: {edit_err}")

            success = CynexCloudSuccessContainer("Vote Confirmed", f"Your vote has been successfully {msg_action}.")
            await interaction.followup.send(view=success.build(), ephemeral=True)

        # Stats view flow
        elif action == "stats":
            await interaction.response.defer(ephemeral=True)
            suggestion_id = parts[3]
            
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT COUNT(*), SUM(vote_value) FROM suggestion_votes WHERE suggestion_id = ?", (suggestion_id,)) as cursor:
                    row = await cursor.fetchone()
                    total_votes = row[0] if row else 0
                    net_score = row[1] if row and row[1] is not None else 0
                    
                async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = 1", (suggestion_id,)) as cursor:
                    up_row = await cursor.fetchone()
                    up = up_row[0] if up_row else 0
                    
                async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = -1", (suggestion_id,)) as cursor:
                    down_row = await cursor.fetchone()
                    down = down_row[0] if down_row else 0

            info = CynexCloudInfoContainer(f"Suggestion Statistics - {suggestion_id}", f"Real-time voting analytical tallies.")
            info.add_section("Total Votes Cast", f"`{total_votes}` votes")
            info.add_section("Net Score", f"`{net_score:+d}`")
            info.add_section("Breakdown", f"👍 Upvotes: `{up}`\n👎 Downvotes: `{down}`")
            await interaction.followup.send(view=info.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # SUGGEST SLASH COMMANDS TREE
    # ══════════════════════════════════════════════════════════════════════

    @app_commands.command(name="suggest", description="Submit a suggestion to the server")
    @app_commands.describe(
        category="Category of the suggestion",
        anonymous="Whether to hide your name on submission"
    )
    @app_commands.choices(category=[
        app_commands.Choice(name="Server", value="Server"),
        app_commands.Choice(name="Hosting", value="Hosting"),
        app_commands.Choice(name="Discord Bot", value="Discord Bot"),
        app_commands.Choice(name="Website", value="Website"),
        app_commands.Choice(name="Billing", value="Billing"),
        app_commands.Choice(name="Other", value="Other")
    ])
    async def suggest(self, interaction: discord.Interaction, category: str, anonymous: bool = False):
        guild_id = str(interaction.guild.id)
        settings = await self.get_settings(guild_id)
        if not settings or not settings["suggest_channel_id"]:
            err = CynexCloudErrorContainer("Configuration Error", "Suggestions are not set up on this server yet. Ask an administrator to run `/suggestion setup`.")
            await interaction.response.send_message(view=err.build(), ephemeral=True)
            return

        # Modal must be sent directly
        await interaction.response.send_modal(SuggestionSubmitModal(category, anonymous, settings))

    suggestion_group = app_commands.Group(name="suggestion", description="CynexCloud suggestion management panel")

    @suggestion_group.command(name="setup", description="Configure suggestion systems")
    @app_commands.describe(channel="Target text channel for suggestions posting")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_suggestions(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO suggestion_settings (guild_id, suggest_channel_id) VALUES (?, ?)",
                (guild_id, str(channel.id))
            )
            await db.commit()

        success = CynexCloudSuccessContainer("Suggestions Setup Completed", f"Suggestions will be posted to {channel.mention}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @suggestion_group.command(name="approve", description="Approve a suggestion and create a discussion thread")
    @app_commands.describe(suggestion_id="ID of the suggestion to approve", reason="Approve commentary notes")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def approve_suggestion(self, interaction: discord.Interaction, suggestion_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status, title, description, category, anonymous, user_id FROM suggestions WHERE suggestion_id = ? AND guild_id = ?", (suggestion_id, guild_id)) as cursor:
                row = await cursor.fetchone()

        if not row:
            err = CynexCloudErrorContainer("Suggestion Not Found", f"Suggestion `{suggestion_id}` is missing.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        status, title, desc, category, anon, author_uid = row
        if status != "pending":
            warn = CynexCloudWarningContainer("Already Moderated", f"Suggestion `{suggestion_id}` status is already `{status}`.")
            await interaction.followup.send(view=warn.build(), ephemeral=True)
            return

        # Upvote/Downvote counts
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = 1", (suggestion_id,)) as cursor:
                up_row = await cursor.fetchone()
                up = up_row[0] if up_row else 0
            async with db.execute("SELECT COUNT(*) FROM suggestion_votes WHERE suggestion_id = ? AND vote_value = -1", (suggestion_id,)) as cursor:
                down_row = await cursor.fetchone()
                down = down_row[0] if down_row else 0

        # Update status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE suggestions SET status = 'approved', notes = ? WHERE suggestion_id = ?",
                (reason, suggestion_id)
            )
            await db.commit()

        # Update channel post & create thread
        settings = await self.get_settings(guild_id)
        if settings and settings["suggest_channel_id"]:
            s_channel = None
            try:
                s_channel = interaction.guild.get_channel(int(settings["suggest_channel_id"]))
                if not s_channel:
                    s_channel = await interaction.guild.fetch_channel(int(settings["suggest_channel_id"]))
            except Exception:
                pass
            if s_channel:
                try:
                    # Let's locate the public message in suggest channel
                    # Since we don't have the exact message_id recorded, we search recent messages in suggest channel
                    # This is simple and highly effective!
                    target_msg = None
                    async for msg in s_channel.history(limit=50):
                        if msg.author.id == self.bot.user.id and msg.views:
                            # We can check if the custom ID contains the suggestion_id
                            # Layout views buttons contains suggestion_id
                            for row_item in msg.components:
                                for comp in row_item.children:
                                    if comp.custom_id and comp.custom_id.endswith(suggestion_id):
                                        target_msg = msg
                                        break
                                if target_msg:
                                    break
                        if target_msg:
                            break

                    pub_layout = CynexCloudContainerBuilder(f"Suggestion: {title}", accent_color=3066993) # Green
                    pub_layout.add_section("Category", f"`{category}`")
                    pub_layout.add_section("Description", desc)
                    pub_layout.add_section("Status", "🟢 **Approved**")
                    pub_layout.add_section("Developer Notes", reason)
                    pub_layout.add_section("Author", "Anonymous" if anon else f"<@{author_uid}>")
                    pub_layout.add_section("Suggestion ID", f"`{suggestion_id}`")
                    
                    btn_up = Button(label=f"👍 Upvote ({up})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:up:{suggestion_id}")
                    btn_down = Button(label=f"👎 Downvote ({down})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:vote:down:{suggestion_id}")
                    btn_stats = Button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:stats:{suggestion_id}")
                    pub_layout.add_buttons(btn_up, btn_down, btn_stats)

                    if target_msg:
                        await target_msg.edit(view=pub_layout.build())
                        # Create Discussion Thread
                        thread = await target_msg.create_thread(name=f"Discussion: {title[:50]}", auto_archive_duration=1440)
                        
                        # Save thread_id
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE suggestions SET thread_id = ? WHERE suggestion_id = ?", (str(thread.id), suggestion_id))
                            await db.commit()
                            
                        # Send moderator confirmation
                        await thread.send(f"🟢 **Suggestion approved by {interaction.user.mention}**\n*Reason:* {reason}")
                except Exception as thread_err:
                    logger.warning(f"Failed to create suggestion thread: {thread_err}")

        success = CynexCloudSuccessContainer("Suggestion Approved", f"Suggestion `{suggestion_id}` approved successfully.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @suggestion_group.command(name="deny", description="Deny a pending suggestion")
    @app_commands.describe(suggestion_id="ID of the suggestion to deny", reason="Denial rationale")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def deny_suggestion(self, interaction: discord.Interaction, suggestion_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status, title, description, category, anonymous, user_id FROM suggestions WHERE suggestion_id = ? AND guild_id = ?", (suggestion_id, guild_id)) as cursor:
                row = await cursor.fetchone()

        if not row:
            err = CynexCloudErrorContainer("Suggestion Not Found", f"Suggestion `{suggestion_id}` is missing.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        status, title, desc, category, anon, author_uid = row
        if status != "pending" and status != "approved":
            warn = CynexCloudWarningContainer("Already Moderated", f"Suggestion `{suggestion_id}` status is already `{status}`.")
            await interaction.followup.send(view=warn.build(), ephemeral=True)
            return

        # Update status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE suggestions SET status = 'denied', notes = ? WHERE suggestion_id = ?",
                (reason, suggestion_id)
            )
            await db.commit()

        # Update channel post & remove active upvote/downvote buttons
        settings = await self.get_settings(guild_id)
        if settings and settings["suggest_channel_id"]:
            s_channel = None
            try:
                s_channel = interaction.guild.get_channel(int(settings["suggest_channel_id"]))
                if not s_channel:
                    s_channel = await interaction.guild.fetch_channel(int(settings["suggest_channel_id"]))
            except Exception:
                pass
            if s_channel:
                try:
                    target_msg = None
                    async for msg in s_channel.history(limit=50):
                        if msg.author.id == self.bot.user.id and msg.views:
                            for row_item in msg.components:
                                for comp in row_item.children:
                                    if comp.custom_id and comp.custom_id.endswith(suggestion_id):
                                        target_msg = msg
                                        break
                                if target_msg:
                                    break
                        if target_msg:
                            break

                    pub_layout = CynexCloudContainerBuilder(f"Suggestion: {title}", accent_color=15158332) # Red
                    pub_layout.add_section("Category", f"`{category}`")
                    pub_layout.add_section("Description", desc)
                    pub_layout.add_section("Status", "🔴 **Denied**")
                    pub_layout.add_section("Staff Notes", reason)
                    pub_layout.add_section("Author", "Anonymous" if anon else f"<@{author_uid}>")
                    pub_layout.add_section("Suggestion ID", f"`{suggestion_id}`")
                    
                    # Keep only stats button, remove upvote/downvote
                    btn_stats = Button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:stats:{suggestion_id}")
                    pub_layout.add_buttons(btn_stats)

                    if target_msg:
                        await target_msg.edit(view=pub_layout.build())
                except Exception:
                    pass

        success = CynexCloudSuccessContainer("Suggestion Denied", f"Suggestion `{suggestion_id}` denied successfully.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @suggestion_group.command(name="implement", description="Mark a suggestion as implemented")
    @app_commands.describe(suggestion_id="ID of the suggestion", notes="Developer implementation notes")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def implement_suggestion(self, interaction: discord.Interaction, suggestion_id: str, notes: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status, title, description, category, anonymous, user_id, thread_id FROM suggestions WHERE suggestion_id = ? AND guild_id = ?", (suggestion_id, guild_id)) as cursor:
                row = await cursor.fetchone()

        if not row:
            err = CynexCloudErrorContainer("Suggestion Not Found", f"Suggestion `{suggestion_id}` is missing.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        status, title, desc, category, anon, author_uid, thread_id = row
        if status != "approved":
            warn = CynexCloudWarningContainer("Status Conflict", f"Only approved suggestions can be marked as implemented (Current status: `{status}`).")
            await interaction.followup.send(view=warn.build(), ephemeral=True)
            return

        # Update status
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE suggestions SET status = 'implemented', notes = ? WHERE suggestion_id = ?",
                (notes, suggestion_id)
            )
            await db.commit()

        # Update suggestion channel card and post to discussion thread if exists
        settings = await self.get_settings(guild_id)
        if settings and settings["suggest_channel_id"]:
            s_channel = None
            try:
                s_channel = interaction.guild.get_channel(int(settings["suggest_channel_id"]))
                if not s_channel:
                    s_channel = await interaction.guild.fetch_channel(int(settings["suggest_channel_id"]))
            except Exception:
                pass
            if s_channel:
                try:
                    target_msg = None
                    async for msg in s_channel.history(limit=50):
                        if msg.author.id == self.bot.user.id and msg.views:
                            for row_item in msg.components:
                                for comp in row_item.children:
                                    if comp.custom_id and comp.custom_id.endswith(suggestion_id):
                                        target_msg = msg
                                        break
                                if target_msg:
                                    break
                        if target_msg:
                            break

                    pub_layout = CynexCloudContainerBuilder(f"Suggestion: {title}", accent_color=13937975) # Gold
                    pub_layout.add_section("Category", f"`{category}`")
                    pub_layout.add_section("Description", desc)
                    pub_layout.add_section("Status", "🔵 **Implemented**")
                    pub_layout.add_section("Implementation Notes", notes)
                    pub_layout.add_section("Author", "Anonymous" if anon else f"<@{author_uid}>")
                    pub_layout.add_section("Suggestion ID", f"`{suggestion_id}`")
                    
                    btn_stats = Button(label="📊 Stats", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:suggest:stats:{suggestion_id}")
                    pub_layout.add_buttons(btn_stats)

                    if target_msg:
                        await target_msg.edit(view=pub_layout.build())
                        
                    if thread_id:
                        thread = interaction.guild.get_thread(int(thread_id))
                        if thread:
                            await thread.send(f"🔵 **Suggestion Implemented by {interaction.user.mention}!**\n*Notes:* {notes}")
                            await thread.edit(locked=True, archived=True)
                except Exception:
                    pass

        success = CynexCloudSuccessContainer("Suggestion Implemented", f"Suggestion `{suggestion_id}` marked as implemented.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @suggestion_group.command(name="archive", description="Archive a suggestion post")
    @app_commands.describe(suggestion_id="ID of the suggestion to archive")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def archive_suggestion(self, interaction: discord.Interaction, suggestion_id: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT thread_id FROM suggestions WHERE suggestion_id = ? AND guild_id = ?", (suggestion_id, guild_id)) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            err = CynexCloudErrorContainer("Suggestion Not Found", f"Suggestion `{suggestion_id}` does not exist.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        thread_id = row[0]
        if thread_id:
            try:
                thread = interaction.guild.get_thread(int(thread_id))
                if thread:
                    await thread.edit(archived=True, reason="Suggestion archived by staff command")
            except Exception:
                pass
                
        success = CynexCloudSuccessContainer("Suggestion Archived", f"Suggestion thread for `{suggestion_id}` has been archived.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @suggestion_group.command(name="stats", description="Show suggestion analytics and leaderboards")
    async def suggestion_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                total = row[0] if row else 0
                
            async with db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ? AND status = 'approved'", (guild_id,)) as cursor:
                app_row = await cursor.fetchone()
                approved = app_row[0] if app_row else 0
                
            async with db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ? AND status = 'implemented'", (guild_id,)) as cursor:
                imp_row = await cursor.fetchone()
                implemented = imp_row[0] if imp_row else 0

        approval_rate = (approved / total * 100) if total > 0 else 0.0

        stats_card = CynexCloudInfoContainer("Suggestions Analytics", f"**Server:** {interaction.guild.name}")
        stats_card.add_section("Total Submitted", f"`{total}` suggestions")
        stats_card.add_section("Approved", f"`{approved}` approved")
        stats_card.add_section("Implemented", f"`{implemented}` implemented")
        stats_card.add_section("Approval Rate", f"`{approval_rate:.1f}%` of total submissions")

        await interaction.followup.send(view=stats_card.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
