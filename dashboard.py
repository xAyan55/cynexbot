import asyncio
import logging
from typing import Optional

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands
from discord.ui import Button

import ui
from ui import (
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudWarningContainer,
    CynexCloudInfoContainer
)

logger = logging.getLogger("CynexCloud.Dashboard")
DB_PATH = "cynex.db"

class Dashboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the CynexCloud Server Control Panel Dashboard")
    @app_commands.checks.has_permissions(administrator=True)
    async def open_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        guild_id = str(guild.id)

        # 1. Fetch statistics from SQLite
        async with aiosqlite.connect(DB_PATH) as db:
            # Reviews count
            async with db.execute("SELECT COUNT(*), AVG(rating) FROM reviews WHERE guild_id = ? AND status = 'approved'", (guild_id,)) as cursor:
                rev_row = await cursor.fetchone()
                reviews_count = rev_row[0] if rev_row else 0
                reviews_avg = rev_row[1] if rev_row and rev_row[1] else 0.0

            # Suggestions count
            async with db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ?", (guild_id,)) as cursor:
                sug_row = await cursor.fetchone()
                suggestions_count = sug_row[0] if sug_row else 0

            # Suggestions implemented/approved
            async with db.execute("SELECT COUNT(*) FROM suggestions WHERE guild_id = ? AND status = 'approved'", (guild_id,)) as cursor:
                sug_app_row = await cursor.fetchone()
                suggestions_approved = sug_app_row[0] if sug_app_row else 0

            # Welcome configurations
            async with db.execute("SELECT channel_id FROM welcome_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                wel_row = await cursor.fetchone()
                welcome_active = "Enabled" if wel_row and wel_row[0] else "Disabled"

            # Ticket configurations
            async with db.execute("SELECT log_channel_id FROM ticket_configs WHERE guild_id = ?", (guild_id,)) as cursor:
                tick_row = await cursor.fetchone()
                tickets_active = "Enabled" if tick_row and tick_row[0] else "Disabled"

            # Saved V2 container builds
            async with db.execute("SELECT COUNT(*) FROM builders WHERE user_id = ?", (str(interaction.user.id),)) as cursor:
                builder_row = await cursor.fetchone()
                saved_containers = builder_row[0] if builder_row else 0

        # Calculations
        sug_rate = (suggestions_approved / suggestions_count * 100) if suggestions_count > 0 else 0.0

        # 2. Build Dashboard Layout
        dash = CynexCloudInfoContainer(
            "CynexCloud Management Dashboard",
            f"💻 **Administrative control panel for server {guild.name}**"
        )
        
        # General Stats Section
        dash.add_section(
            "📊 Server Metrics Overview",
            f"• **Server Members:** `{guild.member_count}`\n"
            f"• **Saved Builders:** `{saved_containers}` templates"
        )
        
        # System States Section
        dash.add_section(
            "⚙️ Core Modules Configuration Status",
            f"• **Welcome Greeting System:** `{welcome_active}`\n"
            f"• **Support Ticket System:** `{tickets_active}`"
        )
        
        # Reviews Section
        dash.add_section(
            "⭐ Customer Reviews Metrics",
            f"• **Approved Reviews Count:** `{reviews_count}` reviews\n"
            f"• **Average Service Rating:** `{reviews_avg:.2f}` / 5.0 {'⭐' * int(round(reviews_avg))}"
        )
        
        # Suggestions Section
        dash.add_section(
            "💡 Suggestions Analytics Tracker",
            f"• **Suggestions Submitted:** `{suggestions_count}` ideas\n"
            f"• **Approval/Discussion Rate:** `{sug_rate:.1f}%` ({suggestions_approved} approved)"
        )

        # Quick Links/Settings Help Buttons
        btn_rev = Button(label="Reviews Info", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:dash:help:reviews")
        btn_sug = Button(label="Suggestions Info", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:dash:help:suggestions")
        btn_wel = Button(label="Welcome Info", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:dash:help:welcome")
        
        async def help_callback(help_interaction: discord.Interaction):
            await help_interaction.response.defer(ephemeral=True)
            custom_id = help_interaction.data.get("custom_id")
            module = custom_id.split(":")[3]
            
            if module == "reviews":
                msg = (
                    "⭐ **Reviews Setup & Usage Guide:**\n\n"
                    "1. Setup review channels: `/review setup [review_channel] [mod_channel]`\n"
                    "2. Customers submit reviews: `/review submit`\n"
                    "3. Staff manages approvals via mod queue or commands: `/review approve [review_id]` / `/review deny [review_id]`\n"
                    "4. View leaderboard and stars count: `/review stats`"
                )
            elif module == "suggestions":
                msg = (
                    "💡 **Suggestions Setup & Usage Guide:**\n\n"
                    "1. Setup suggestions posting channel: `/suggestion setup [channel]`\n"
                    "2. Users submit ideas: `/suggest [category] [anonymous]`\n"
                    "3. Staff reviews and updates suggestion workflow: `/suggestion approve [suggestion_id] [notes]` / `/suggestion deny [suggestion_id]` / `/suggestion implement [suggestion_id]`"
                )
            elif module == "welcome":
                msg = (
                    "👋 **Welcome Setup & Usage Guide:**\n\n"
                    "1. Setup greetings and logs channel: `/welcome setup [channel] [dm_enabled] [log_channel] [auto_role]`\n"
                    "2. Customize text blocks: `/welcome edit [Welcome Message/Rules Guidelines] [value]`\n"
                    "3. Generate preview or test joins: `/welcome preview` / `/welcome test`"
                )
            else:
                msg = "No help details found."
                
            info = CynexCloudInfoContainer(f"{module.title()} Setup Help", msg)
            await help_interaction.followup.send(view=info.build(), ephemeral=True)
            
        btn_rev.callback = help_callback
        btn_sug.callback = help_callback
        btn_wel.callback = help_callback
        
        dash.add_buttons(btn_rev, btn_sug, btn_wel)
        await interaction.followup.send(view=dash.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Dashboard(bot))
