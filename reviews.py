import asyncio
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

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

logger = logging.getLogger("CynexCloud.Reviews")
DB_PATH = "fb.db"

# ══════════════════════════════════════════════════════════════════════
# REVIEW SUBMISSION MODALS
# ══════════════════════════════════════════════════════════════════════

class ReviewSubmitModal(discord.ui.Modal, title="Submit a Service Review"):
    rating = discord.ui.TextInput(
        label="Rating (1 to 5 Stars)",
        placeholder="Enter 1, 2, 3, 4, or 5",
        min_length=1,
        max_length=1,
        required=True
    )
    service = discord.ui.TextInput(
        label="Service Used",
        placeholder="e.g. Hosting, Bot Setup, Development",
        max_length=100,
        required=True
    )
    message = discord.ui.TextInput(
        label="Review Message",
        style=discord.TextStyle.paragraph,
        placeholder="Detailed feedback about your experience...",
        min_length=10,
        max_length=1000,
        required=True
    )
    screenshot = discord.ui.TextInput(
        label="Optional Screenshot URL",
        placeholder="https://i.imgur.com/... (optional)",
        required=False
    )

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # Validate rating
        try:
            val = int(self.rating.value.strip())
            if val < 1 or val > 5:
                raise ValueError()
        except ValueError:
            err = CynexCloudErrorContainer("Invalid Rating", "Please enter an integer between 1 and 5 for your rating.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        screenshot_url = self.screenshot.value.strip()
        if screenshot_url and not (screenshot_url.startswith("http://") or screenshot_url.startswith("https://")):
            err = CynexCloudErrorContainer("Invalid Screenshot URL", "Please enter a valid HTTP/HTTPS image URL.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild.id)
        service_text = self.service.value.strip()
        message_text = self.message.value.strip()

        async with aiosqlite.connect(DB_PATH) as db:
            # Generate Review ID
            async with db.execute("SELECT COUNT(*) FROM reviews") as cursor:
                count_row = await cursor.fetchone()
                review_count = count_row[0] if count_row else 0
            review_id = f"R-{1001 + review_count}"

            # Save review
            await db.execute(
                "INSERT INTO reviews (review_id, guild_id, user_id, rating, service, message, screenshot_url, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'approved')",
                (review_id, guild_id, user_id, val, service_text, message_text, screenshot_url or None)
            )
            # Log command usage
            await db.execute(
                "INSERT INTO command_usage (command_name, user_id, guild_id) VALUES ('review submit', ?, ?)",
                (user_id, guild_id)
            )
            await db.commit()

        # Publish to public reviews channel immediately
        pub_channel = None
        channel_id = self.settings.get("review_channel_id")
        if channel_id:
            try:
                pub_channel = interaction.guild.get_channel(int(channel_id))
                if not pub_channel:
                    pub_channel = await interaction.guild.fetch_channel(int(channel_id))
            except Exception as e:
                logger.warning(f"Could not get/fetch review channel {channel_id}: {e}")

        if pub_channel:
            try:
                pub_layout = CynexCloudContainerBuilder(f"Review: {service_text}", accent_color=13937975) # Gold
                meta_content = f"**Rating:** {'⭐' * val}\n**Submitted By:** {interaction.user.mention}"
                if screenshot_url:
                    meta_content += f"\n**Screenshot Reference:** {screenshot_url}"
                
                pub_layout.add_section("Customer Review", message_text)
                pub_layout.add_section("Metadata", meta_content)
                
                btn_helpful = Button(label="Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:helpful:{review_id}")
                btn_unhelpful = Button(label="Not Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:unhelpful:{review_id}")
                btn_report = Button(label="Report Review", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:report:{review_id}")
                pub_layout.add_buttons(btn_helpful, btn_unhelpful, btn_report)
                
                await pub_channel.send(view=pub_layout.build())
            except Exception as pub_err:
                logger.warning(f"Failed to publish review message: {pub_err}")

        success = CynexCloudSuccessContainer("Review Submitted", f"Thank you! Your review `{review_id}` has been published to {pub_channel.mention if pub_channel else 'the reviews channel'}.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

class ReviewDenyModal(discord.ui.Modal, title="Deny Review Confirmation"):
    reason = discord.ui.TextInput(
        label="Denial Reason",
        placeholder="Provide the reason for denying this review...",
        min_length=5,
        max_length=200,
        required=True
    )

    def __init__(self, review_id: str, message: discord.Message):
        super().__init__()
        self.review_id = review_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason_text = self.reason.value.strip()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE reviews SET status = 'denied' WHERE review_id = ?",
                (self.review_id,)
            )
            await db.commit()

        # Update mod channel message
        try:
            denied_layout = CynexCloudErrorContainer("Review Denied", f"Review `{self.review_id}` has been denied.")
            denied_layout.add_section("Moderator", interaction.user.mention)
            denied_layout.add_section("Reason", reason_text)
            await self.message.edit(view=denied_layout.build())
        except Exception:
            pass

        success = CynexCloudSuccessContainer("Review Denied Successfully", f"Review `{self.review_id}` status has been set to denied.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# REVIEWS COG MODULE
# ══════════════════════════════════════════════════════════════════════

class Reviews(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.init_db()

    async def init_db(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    service TEXT NOT NULL,
                    message TEXT NOT NULL,
                    screenshot_url TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS review_votes (
                    review_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    vote_type TEXT NOT NULL,
                    PRIMARY KEY (review_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS review_settings (
                    guild_id TEXT PRIMARY KEY,
                    review_channel_id TEXT,
                    mod_channel_id TEXT
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_reviews_guild_status ON reviews (guild_id, status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_review_votes_review ON review_votes (review_id)")
            await db.commit()

    async def get_settings(self, guild_id: str) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT review_channel_id, mod_channel_id FROM review_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "review_channel_id": row[0],
                        "mod_channel_id": row[1]
                    }
        return None

    # ══════════════════════════════════════════════════════════════════════
    # INTERACTION LISTENER (MODERATION/VOTING ROUTER)
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        custom_id = interaction.data.get("custom_id") if interaction.data else None
        if not custom_id or not custom_id.startswith("cynexcloud:review:"):
            return

        parts = custom_id.split(":")
        action = parts[2]
        
        # Approve flow
        if action == "approve":
            await interaction.response.defer(ephemeral=True)
            review_id = parts[3]
            
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE reviews SET status = 'approved' WHERE review_id = ?", (review_id,))
                await db.commit()
                
                async with db.execute("SELECT user_id, rating, service, message, screenshot_url FROM reviews WHERE review_id = ?", (review_id,)) as cursor:
                    review = await cursor.fetchone()
            
            if not review:
                err = CynexCloudErrorContainer("Review Not Found", f"Metadata for `{review_id}` is missing.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return

            author_id, rating, service, msg_content, screenshot_url = review
            
            # Edit Mod Message
            try:
                mod_ok = CynexCloudSuccessContainer("Review Approved", f"Review `{review_id}` has been approved.")
                mod_ok.add_section("Moderator", interaction.user.mention)
                await interaction.message.edit(view=mod_ok.build())
            except Exception:
                pass

            # Publish to Reviews Channel
            settings = await self.get_settings(str(interaction.guild_id))
            if settings and settings["review_channel_id"]:
                pub_channel = interaction.guild.get_channel(int(settings["review_channel_id"]))
                if pub_channel:
                    try:
                        pub_layout = CynexCloudContainerBuilder(f"Review: {service}", accent_color=13937975) # Gold
                        pub_layout.add_section("Rating", "⭐" * rating)
                        pub_layout.add_section("Customer Review", msg_content)
                        pub_layout.add_section("Submitted By", f"<@{author_id}>")
                        if screenshot_url:
                            pub_layout.add_section("Screenshot Reference", screenshot_url)
                        
                        btn_helpful = Button(label="Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:helpful:{review_id}")
                        btn_unhelpful = Button(label="Not Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:unhelpful:{review_id}")
                        btn_report = Button(label="Report Review", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:report:{review_id}")
                        pub_layout.add_buttons(btn_helpful, btn_unhelpful, btn_report)
                        
                        await pub_channel.send(view=pub_layout.build())
                    except Exception as pub_err:
                        logger.warning(f"Failed to publish review message: {pub_err}")

            success = CynexCloudSuccessContainer("Review Approved Successfully", f"Review `{review_id}` is published.")
            await interaction.followup.send(view=success.build(), ephemeral=True)

        # Deny flow
        elif action == "deny":
            review_id = parts[3]
            # Send denial modal to ask for the reason
            await interaction.response.send_modal(ReviewDenyModal(review_id, interaction.message))

        # Vote helpful/unhelpful flow
        elif action == "vote":
            await interaction.response.defer(ephemeral=True)
            vote_type = parts[3] # helpful or unhelpful
            review_id = parts[4]
            user_id = str(interaction.user.id)
            
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT vote_type FROM review_votes WHERE review_id = ? AND user_id = ?",
                    (review_id, user_id)
                ) as cursor:
                    row = await cursor.fetchone()
                
                if row:
                    current_vote = row[0]
                    if current_vote == vote_type:
                        # Toggle / Remove vote
                        await db.execute("DELETE FROM review_votes WHERE review_id = ? AND user_id = ?", (review_id, user_id))
                        msg_action = "removed"
                    else:
                        # Change vote
                        await db.execute(
                            "UPDATE review_votes SET vote_type = ? WHERE review_id = ? AND user_id = ?",
                            (vote_type, review_id, user_id)
                        )
                        msg_action = f"updated to {vote_type.replace('_', ' ')}"
                else:
                    # New vote
                    await db.execute(
                        "INSERT INTO review_votes (review_id, user_id, vote_type) VALUES (?, ?, ?)",
                        (review_id, user_id, vote_type)
                    )
                    msg_action = f"registered as {vote_type.replace('_', ' ')}"
                await db.commit()

                # Get Tallies
                async with db.execute("SELECT COUNT(*) FROM review_votes WHERE review_id = ? AND vote_type = 'helpful'", (review_id,)) as cursor:
                    help_row = await cursor.fetchone()
                    help_count = help_row[0] if help_row else 0
                async with db.execute("SELECT COUNT(*) FROM review_votes WHERE review_id = ? AND vote_type = 'unhelpful'", (review_id,)) as cursor:
                    unhelp_row = await cursor.fetchone()
                    unhelp_count = unhelp_row[0] if unhelp_row else 0

            # Update Buttons in Public Review Message
            try:
                # Re-fetch view components from public message and update labels
                message = interaction.message
                action_row = message.components[0] if message.components else None
                if action_row:
                    new_layout = LayoutView()
                    new_container = Container(accent_color=13937975) # Gold
                    new_layout.add_item(new_container)
                    
                    # Extract contents from existing sections
                    for item in message.components:
                        pass # Layout view components edit requires rebuilding
                    
                    # Instead, we rebuild the layout with updated button counts
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute("SELECT user_id, rating, service, message, screenshot_url FROM reviews WHERE review_id = ?", (review_id,)) as cursor:
                            rev_row = await cursor.fetchone()
                            
                    if rev_row:
                        author_id, rating, service, msg_content, screenshot_url = rev_row
                        pub_layout = CynexCloudContainerBuilder(f"Review: {service}", accent_color=13937975)
                        meta_content = f"**Rating:** {'⭐' * rating}\n**Submitted By:** <@{author_id}>"
                        if screenshot_url:
                            meta_content += f"\n**Screenshot Reference:** {screenshot_url}"
                        
                        pub_layout.add_section("Customer Review", msg_content)
                        pub_layout.add_section("Metadata", meta_content)
                        
                        btn_helpful = Button(label=f"Helpful ({help_count})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:helpful:{review_id}")
                        btn_unhelpful = Button(label=f"Not Helpful ({unhelp_count})", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:unhelpful:{review_id}")
                        btn_report = Button(label="Report Review", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:report:{review_id}")
                        pub_layout.add_buttons(btn_helpful, btn_unhelpful, btn_report)
                        
                        await message.edit(view=pub_layout.build())
            except Exception as edit_err:
                logger.warning(f"Failed to edit public review votes button: {edit_err}")

            success = CynexCloudSuccessContainer("Vote Logged", f"Your helpfulness vote has been successfully {msg_action}.")
            await interaction.followup.send(view=success.build(), ephemeral=True)

        # Report flow
        elif action == "report":
            await interaction.response.defer(ephemeral=True)
            review_id = parts[3]
            
            settings = await self.get_settings(str(interaction.guild_id))
            if settings and settings["mod_channel_id"]:
                mod_channel = interaction.guild.get_channel(int(settings["mod_channel_id"]))
                if mod_channel:
                    try:
                        rep_card = CynexCloudWarningContainer("Review Reported Alert", f"Review `{review_id}` was reported by a customer.")
                        rep_card.add_section("Reporter", interaction.user.mention)
                        rep_card.add_section("Review Link/Message", f"[Jump to original Review]({interaction.message.jump_url})")
                        
                        del_btn = Button(label="Force Delete Review", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:forcedelete:{review_id}")
                        rep_card.add_buttons(del_btn)
                        
                        await mod_channel.send(view=rep_card.build())
                    except Exception:
                        pass
            
            success = CynexCloudSuccessContainer("Report Submitted", "Thank you for reporting. This review has been submitted to staff for inspection.")
            await interaction.followup.send(view=success.build(), ephemeral=True)

        # Force delete review (moderator action)
        elif action == "forcedelete":
            await interaction.response.defer(ephemeral=True)
            review_id = parts[3]
            
            is_admin = interaction.user.guild_permissions.administrator
            if not is_admin:
                err = CynexCloudErrorContainer("Permission Denied", "Only administrators can force-delete reviews.")
                await interaction.followup.send(view=err.build(), ephemeral=True)
                return
                
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM reviews WHERE review_id = ?", (review_id,))
                await db.execute("DELETE FROM review_votes WHERE review_id = ?", (review_id,))
                await db.commit()
                
            # Update mod report card
            try:
                done = CynexCloudSuccessContainer("Review Force Deleted", f"Review `{review_id}` has been deleted from database by {interaction.user.mention}.")
                await interaction.message.edit(view=done.build())
            except Exception:
                pass
                
            success = CynexCloudSuccessContainer("Review Deleted", f"Review `{review_id}` deleted successfully.")
            await interaction.followup.send(view=success.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # REVIEWS SLASH COMMAND TREE (/review)
    # ══════════════════════════════════════════════════════════════════════

    review_group = app_commands.Group(name="review", description="CynexCloud reviews management system")

    @review_group.command(name="setup", description="Configure reviews channels")
    @app_commands.describe(review_channel="Channel for approved public reviews", mod_channel="Moderation review queue channel")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_reviews(self, interaction: discord.Interaction, review_channel: discord.TextChannel, mod_channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO review_settings (guild_id, review_channel_id, mod_channel_id) VALUES (?, ?, ?)",
                (guild_id, str(review_channel.id), str(mod_channel.id))
            )
            await db.commit()

        success = CynexCloudSuccessContainer(
            "Reviews Setup Completed",
            f"• Public Review Channel: {review_channel.mention}\n• Review Mod Channel: {mod_channel.mention}"
        )
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @review_group.command(name="submit", description="Submit a new service review")
    async def submit_review(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild.id)
        settings = await self.get_settings(guild_id)
        if not settings:
            err = CynexCloudErrorContainer("Configuration Error", "Reviews system is not set up on this server. Ask an administrator to run `/review setup`.")
            await interaction.response.send_message(view=err.build(), ephemeral=True)
            return

        # Modals cannot be deferred first, so we open directly!
        await interaction.response.send_modal(ReviewSubmitModal(settings))

    @review_group.command(name="approve", description="Approve a pending review")
    @app_commands.describe(review_id="ID of the review to approve")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def approve_review(self, interaction: discord.Interaction, review_id: str):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status, user_id, rating, service, message, screenshot_url FROM reviews WHERE review_id = ?", (review_id,)) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            err = CynexCloudErrorContainer("Review Not Found", f"Review `{review_id}` does not exist.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        status, author_id, rating, service, msg_content, screenshot_url = row
        if status != "pending":
            warn = CynexCloudWarningContainer("Already Moderated", f"Review `{review_id}` is already `{status}`.")
            await interaction.followup.send(view=warn.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE reviews SET status = 'approved' WHERE review_id = ?", (review_id,))
            await db.commit()

        # Publish to public reviews channel
        settings = await self.get_settings(str(interaction.guild_id))
        if settings and settings["review_channel_id"]:
            pub_channel = None
            channel_id = settings["review_channel_id"]
            try:
                pub_channel = interaction.guild.get_channel(int(channel_id))
                if not pub_channel:
                    pub_channel = await interaction.guild.fetch_channel(int(channel_id))
            except Exception as e:
                logger.warning(f"Could not get/fetch review channel {channel_id}: {e}")
            
            if pub_channel:
                try:
                    pub_layout = CynexCloudContainerBuilder(f"Review: {service}", accent_color=13937975)
                    meta_content = f"**Rating:** {'⭐' * rating}\n**Submitted By:** <@{author_id}>"
                    if screenshot_url:
                        meta_content += f"\n**Screenshot Reference:** {screenshot_url}"
                    
                    pub_layout.add_section("Customer Review", msg_content)
                    pub_layout.add_section("Metadata", meta_content)
                    
                    btn_helpful = Button(label="Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:helpful:{review_id}")
                    btn_unhelpful = Button(label="Not Helpful (0)", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:vote:unhelpful:{review_id}")
                    btn_report = Button(label="Report Review", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:review:report:{review_id}")
                    pub_layout.add_buttons(btn_helpful, btn_unhelpful, btn_report)
                    
                    await pub_channel.send(view=pub_layout.build())
                except Exception as pub_err:
                    logger.warning(f"Failed to publish review message: {pub_err}")

        success = CynexCloudSuccessContainer("Review Approved", f"Review `{review_id}` approved and posted.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @review_group.command(name="deny", description="Deny a pending review")
    @app_commands.describe(review_id="ID of the review to deny", reason="Reason for denial")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def deny_review(self, interaction: discord.Interaction, review_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT status FROM reviews WHERE review_id = ?", (review_id,)) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            err = CynexCloudErrorContainer("Review Not Found", f"Review `{review_id}` does not exist.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return
            
        status = row[0]
        if status != "pending":
            warn = CynexCloudWarningContainer("Already Moderated", f"Review `{review_id}` is already `{status}`.")
            await interaction.followup.send(view=warn.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE reviews SET status = 'denied' WHERE review_id = ?", (review_id,))
            await db.commit()

        success = CynexCloudSuccessContainer("Review Denied", f"Review `{review_id}` has been denied. Reason: **{reason}**")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @review_group.command(name="delete", description="Delete an existing review")
    @app_commands.describe(review_id="ID of the review to delete")
    @app_commands.checks.has_permissions(administrator=True)
    async def delete_review(self, interaction: discord.Interaction, review_id: str):
        await interaction.response.defer(ephemeral=True)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            err = CynexCloudErrorContainer("Review Not Found", f"Review `{review_id}` does not exist.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM reviews WHERE review_id = ?", (review_id,))
            await db.execute("DELETE FROM review_votes WHERE review_id = ?", (review_id,))
            await db.commit()

        success = CynexCloudSuccessContainer("Review Deleted", f"Review `{review_id}` has been deleted from the database.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @review_group.command(name="list", description="List approved reviews")
    async def list_reviews(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT review_id, user_id, rating, service, message FROM reviews WHERE guild_id = ? AND status = 'approved' ORDER BY created_at DESC",
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            info = CynexCloudInfoContainer("No Reviews", "There are no approved reviews on this server yet.")
            await interaction.followup.send(view=info.build(), ephemeral=True)
            return

        pages = []
        page_size = 3
        for i in range(0, len(rows), page_size):
            chunk = rows[i:i + page_size]
            page_text = ""
            for r in chunk:
                r_id, u_id, rating, svc, msg = r
                page_text += (
                    f"**ID:** `{r_id}` | **Author:** <@{u_id}>\n"
                    f"**Rating:** {'⭐' * rating} | **Service:** `{svc}`\n"
                    f"**Message:** {msg}\n"
                    f"─────────────────────\n"
                )
            pages.append(page_text)

        paginator = CynexCloudPaginationContainer("Server Reviews List", pages, interaction.user.id, accent_color=13937975)
        await interaction.followup.send(view=paginator, ephemeral=True)

    @review_group.command(name="stats", description="Show reviews statistics and analytics")
    async def reviews_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*), AVG(rating) FROM reviews WHERE guild_id = ? AND status = 'approved'", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                total = row[0] if row else 0
                avg = row[1] if row and row[1] else 0.0

            # Leaderboard
            async with db.execute(
                "SELECT user_id, COUNT(*) as cnt FROM reviews WHERE guild_id = ? AND status = 'approved' GROUP BY user_id ORDER BY cnt DESC LIMIT 5",
                (guild_id,)
            ) as cursor:
                leaderboard = await cursor.fetchall()

        stats_card = CynexCloudInfoContainer("Reviews Analytics", f"**Server:** {interaction.guild.name}")
        stats_card.add_section("Total Reviews", f"`{total}` approved reviews")
        stats_card.add_section("Average Rating", f"`{avg:.2f}` / 5.0 {'⭐' * int(round(avg))}")
        
        # Format leaderboard
        lb_text = ""
        if leaderboard:
            for idx, user_row in enumerate(leaderboard, 1):
                lb_text += f"`{idx}.` <@{user_row[0]}> — `{user_row[1]}` reviews\n"
        else:
            lb_text = "No reviewers leaderboard available."
        stats_card.add_section("🏆 Top Reviewers", lb_text)

        await interaction.followup.send(view=stats_card.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Reviews(bot))
