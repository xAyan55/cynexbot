import asyncio
import json
import logging
from datetime import datetime, timezone
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
    CynexCloudContainerBuilder,
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudWarningContainer,
    CynexCloudInfoContainer
)

logger = logging.getLogger("CynexCloud.Welcome")
DB_PATH = "cynex.db"

# ══════════════════════════════════════════════════════════════════════
# WELCOME VARIABLE HELPER
# ══════════════════════════════════════════════════════════════════════

def translate_welcome_variables(text: str, member: discord.Member) -> str:
    """Translates placeholder brackets with dynamic member details."""
    if not text:
        return ""
    
    created_epoch = int(member.created_at.timestamp())
    created_str = f"<t:{created_epoch}:F> (<t:{created_epoch}:R>)"
    
    joined_at = member.joined_at or datetime.now(timezone.utc)
    joined_epoch = int(joined_at.timestamp())
    joined_str = f"<t:{joined_epoch}:F> (<t:{joined_epoch}:R>)"
    
    replacements = {
        "{user}": str(member),
        "{username}": member.name,
        "{server}": member.guild.name,
        "{membercount}": str(member.guild.member_count),
        "{mention}": member.mention,
        "{created}": created_str,
        "{joined}": joined_str
    }
    
    for key, val in replacements.items():
        text = text.replace(key, val)
    return text

# ══════════════════════════════════════════════════════════════════════
# PUBLIC WELCOME INTERACTION CALLBACK
# ══════════════════════════════════════════════════════════════════════

async def on_interaction(interaction: discord.Interaction):
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    if not custom_id or not custom_id.startswith("cynexcloud:welcome:"):
        return

    action = custom_id.split(":")[2]
    await interaction.response.defer(ephemeral=True)

    if action == "rules":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT rules_text FROM welcome_settings WHERE guild_id = ?", (str(interaction.guild_id),)) as cursor:
                row = await cursor.fetchone()
                
        rules_text = row[0] if row and row[0] else "📜 Please check the server rules channels for details."
        info = CynexCloudInfoContainer("Server Rules", rules_text)
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "support":
        # Check if tickets system is loaded
        tickets_cog = interaction.client.get_cog("TicketGroup") # app commands group is not a Cog, tickets is usually registered
        # We can send information about tickets
        info = CynexCloudInfoContainer(
            "CynexCloud Help Desk Support",
            "🎫 Need assistance? Run the `/ticket panel` or `/ticket setup` command to contact staff."
        )
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "website":
        info = CynexCloudInfoContainer(
            "CynexCloud Web Portal",
            "🌐 Visit our official web site at: **https://cynexcloud.dev**"
        )
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "announce":
        info = CynexCloudInfoContainer(
            "📢 Server Announcements",
            "To stay updated, check our announcement channels and enable notifications!"
        )
        await interaction.followup.send(view=info.build(), ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# WELCOME COG MODULE
# ══════════════════════════════════════════════════════════════════════

class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.init_db()
        # Bind the global listener to on_interaction
        self.bot.add_listener(on_interaction, "on_interaction")

    async def cog_unload(self):
        self.bot.remove_listener(on_interaction, "on_interaction")

    async def init_db(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS welcome_settings (
                    guild_id TEXT PRIMARY KEY,
                    channel_id TEXT,
                    dm_enabled INTEGER DEFAULT 0,
                    join_roles TEXT DEFAULT '[]',
                    log_channel_id TEXT,
                    welcome_message TEXT,
                    rules_text TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS member_history (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    first_joined TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_left TIMESTAMP,
                    rejoin_count INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_member_history_user ON member_history (user_id)")
            await db.commit()

    async def get_settings(self, guild_id: str) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT channel_id, dm_enabled, join_roles, log_channel_id, welcome_message, rules_text FROM welcome_settings WHERE guild_id = ?",
                (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "channel_id": row[0],
                        "dm_enabled": bool(row[1]),
                        "join_roles": json.loads(row[2]),
                        "log_channel_id": row[3],
                        "welcome_message": row[4],
                        "rules_text": row[5]
                    }
        return None

    # ══════════════════════════════════════════════════════════════════════
    # GUILD MEMBER JOIN/LEAVE OBSERVERS
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild_id = str(member.guild.id)
        user_id = str(member.id)
        now = datetime.now(timezone.utc)
        
        # 1. Update Join Analytics
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO guild_analytics (guild_id, event_type, user_id) VALUES (?, 'join', ?)",
                    (guild_id, user_id)
                )
                await db.commit()
        except Exception:
            pass

        settings = await self.get_settings(guild_id)
        if not settings:
            return

        # 2. Rejoin Detection
        rejoin_text = "New Join"
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT rejoin_count FROM member_history WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
                
            if row:
                rejoin_count = row[0] + 1
                await db.execute(
                    "UPDATE member_history SET rejoin_count = ? WHERE guild_id = ? AND user_id = ?",
                    (rejoin_count, guild_id, user_id)
                )
                rejoin_text = f"Rejoined (Rejoin Count: {rejoin_count})"
            else:
                await db.execute(
                    "INSERT INTO member_history (guild_id, user_id, rejoin_count) VALUES (?, ?, 0)",
                    (guild_id, user_id)
                )
            await db.commit()

        # 3. Account Age / Anti-Alt Detection
        account_age_days = (datetime.now(timezone.utc) - member.created_at).days
        alt_alert = ""
        if account_age_days < 7:
            alt_alert = "🚨 **Anti-Alt Alert:** Account was created less than 7 days ago!"

        # 4. Log Join to log channel
        if settings["log_channel_id"]:
            log_ch = None
            try:
                log_ch = member.guild.get_channel(int(settings["log_channel_id"]))
                if not log_ch:
                    log_ch = await member.guild.fetch_channel(int(settings["log_channel_id"]))
            except Exception:
                pass
            if log_ch:
                try:
                    log_layout = CynexCloudInfoContainer(
                        "Member Joined",
                        f"👤 {member.mention} (`{member.name}` / `{member.id}`)\n"
                        f"• Account: <t:{int(member.created_at.timestamp())}:F>\n"
                        f"• Status: {rejoin_text}\n"
                        f"{alt_alert}"
                    )
                    await log_ch.send(view=log_layout.build())
                except Exception:
                    pass

        # 5. Auto Role Assignment
        if settings["join_roles"]:
            for role_id in settings["join_roles"]:
                role = member.guild.get_role(int(role_id))
                if role:
                    try:
                        await member.add_roles(role, reason="CynexCloud Welcome Auto-Role Assignment")
                    except Exception as role_err:
                        logger.warning(f"Failed to assign auto-role {role_id} to {member}: {role_err}")

        # 6. Construct V2 Welcome Container Message
        default_welcome = (
            "👋 **Welcome {mention} to {server}!**\n"
            "We are thrilled to have you join our community.\n\n"
            "💡 **Getting Started Checklist:**\n"
            "• Visit our website to check hosting services.\n"
            "• View guidelines by clicking the **📜 Rules** button below.\n"
            "• Need help? Create a support ticket with **🎫 Support**.\n\n"
            "*We now have {membercount} members!*"
        )
        welcome_txt = settings["welcome_message"] or default_welcome
        translated_msg = translate_welcome_variables(welcome_txt, member)

        welcome_layout = CynexCloudContainerBuilder(f"Welcome to {member.guild.name}!", accent_color=3447003)
        welcome_layout.add_section("Greetings", translated_msg)
        welcome_layout.add_section("Server Information", f"You are member number **{member.guild.member_count}**.")
        welcome_layout.add_section("Rules & Guidelines", "Please click the **Rules** button below or check the server rules channels to ensure guidelines are followed.")
        
        btn_rules = Button(label="📜 Rules", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:rules:{member.id}")
        btn_support = Button(label="🎫 Support", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:support:{member.id}")
        btn_web = Button(label="🌐 Website", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:website:{member.id}")
        btn_announce = Button(label="📢 Announcements", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:announce:{member.id}")
        welcome_layout.add_buttons(btn_rules, btn_support, btn_web, btn_announce)

        # 7. Post Welcome Message
        if settings["channel_id"]:
            welcome_ch = None
            try:
                welcome_ch = member.guild.get_channel(int(settings["channel_id"]))
                if not welcome_ch:
                    welcome_ch = await member.guild.fetch_channel(int(settings["channel_id"]))
            except Exception as e:
                logger.error(f"Failed to resolve welcome channel {settings['channel_id']}: {e}")
            if welcome_ch:
                try:
                    await welcome_ch.send(view=welcome_layout.build())
                except Exception as e:
                    logger.error(f"Failed to send welcome message to channel: {e}", exc_info=True)

        # 8. Send Welcome DM
        if settings["dm_enabled"]:
            try:
                await member.send(view=welcome_layout.build())
            except Exception as e:
                logger.error(f"Failed to send welcome DM to {member}: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild_id = str(member.guild.id)
        user_id = str(member.id)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Update Leave Analytics
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO guild_analytics (guild_id, event_type, user_id) VALUES (?, 'leave', ?)",
                    (guild_id, user_id)
                )
                await db.execute(
                    "UPDATE member_history SET last_left = ? WHERE guild_id = ? AND user_id = ?",
                    (now_str, guild_id, user_id)
                )
                await db.commit()
        except Exception:
            pass

        settings = await self.get_settings(guild_id)
        if not settings:
            return

        # Log Leave
        if settings["log_channel_id"]:
            log_ch = None
            try:
                log_ch = member.guild.get_channel(int(settings["log_channel_id"]))
                if not log_ch:
                    log_ch = await member.guild.fetch_channel(int(settings["log_channel_id"]))
            except Exception:
                pass
            if log_ch:
                try:
                    log_layout = CynexCloudWarningContainer(
                        "Member Left",
                        f"👤 {member.mention} (`{member.name}` / `{member.id}`)\n"
                        f"We now have **{member.guild.member_count}** members."
                    )
                    await log_ch.send(view=log_layout.build())
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════
    # WELCOME SLASH COMMANDS TREE (/welcome)
    # ══════════════════════════════════════════════════════════════════════

    welcome_group = app_commands.Group(name="welcome", description="CynexCloud greeting and auto-roles setup panel")

    @welcome_group.command(name="setup", description="Configure the welcome system")
    @app_commands.describe(
        channel="Channel to post public welcome banners",
        dm_enabled="Whether to welcome users in DMs",
        log_channel="Channel for join/leave logging",
        auto_role="Role to automatically assign on join"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_welcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        dm_enabled: bool,
        log_channel: discord.TextChannel,
        auto_role: Optional[discord.Role] = None
    ):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        roles_list = [str(auto_role.id)] if auto_role else []

        async with aiosqlite.connect(DB_PATH) as db:
            # Check if settings already exist to preserve welcome_message/rules_text
            async with db.execute("SELECT welcome_message, rules_text FROM welcome_settings WHERE guild_id = ?", (guild_id,)) as cursor:
                row = await cursor.fetchone()
                
            default_welcome = (
                "👋 **Welcome {mention} to {server}!**\n"
                "We are thrilled to have you join our community.\n\n"
                "💡 **Getting Started Checklist:**\n"
                "• Visit our website to check hosting services.\n"
                "• View guidelines by clicking the **📜 Rules** button below.\n"
                "• Need help? Create a support ticket with **🎫 Support**.\n\n"
                "*We now have {membercount} members!*"
            )
            welcome_msg = row[0] if (row and row[0]) else default_welcome
            rules_txt = row[1] if row else "📜 Please behave and read server guidelines."

            await db.execute(
                "INSERT OR REPLACE INTO welcome_settings (guild_id, channel_id, dm_enabled, join_roles, log_channel_id, welcome_message, rules_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (guild_id, str(channel.id), 1 if dm_enabled else 0, json.dumps(roles_list), str(log_channel.id), welcome_msg, rules_txt)
            )
            await db.commit()

        success = CynexCloudSuccessContainer(
            "Welcome System Configured",
            f"• Welcome channel: {channel.mention}\n"
            f"• DM system: `{'Enabled' if dm_enabled else 'Disabled'}`\n"
            f"• Join log: {log_channel.mention}\n"
            f"• Auto role: {auto_role.mention if auto_role else 'None'}"
        )
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="preview", description="Preview server welcome banner layout")
    async def preview_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        settings = await self.get_settings(guild_id)
        if not settings:
            err = CynexCloudErrorContainer("Not Configured", "Welcome system is not set up on this server.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        default_welcome = (
            "👋 **Welcome {mention} to {server}!**\n"
            "We are thrilled to have you join our community.\n\n"
            "💡 **Getting Started Checklist:**\n"
            "• Visit our website to check hosting services.\n"
            "• View guidelines by clicking the **📜 Rules** button below.\n"
            "• Need help? Create a support ticket with **🎫 Support**.\n\n"
            "*We now have {membercount} members!*"
        )
        welcome_txt = settings["welcome_message"] or default_welcome
        translated_msg = translate_welcome_variables(welcome_txt, interaction.user)

        welcome_layout = CynexCloudContainerBuilder(f"Welcome to {interaction.guild.name}!", accent_color=3447003)
        welcome_layout.add_section("Greetings", translated_msg)
        welcome_layout.add_section("Server Information", f"You are member number **{interaction.guild.member_count}**.")
        welcome_layout.add_section("Rules & Guidelines", "Please click the **Rules** button below or check the server rules channels to ensure guidelines are followed.")
        
        btn_rules = Button(label="📜 Rules", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:rules:{interaction.user.id}")
        btn_support = Button(label="🎫 Support", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:support:{interaction.user.id}")
        btn_web = Button(label="🌐 Website", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:website:{interaction.user.id}")
        btn_announce = Button(label="📢 Announcements", style=discord.ButtonStyle.secondary, custom_id=f"cynexcloud:welcome:announce:{interaction.user.id}")
        welcome_layout.add_buttons(btn_rules, btn_support, btn_web, btn_announce)

        await interaction.followup.send(view=welcome_layout.build(), ephemeral=True)

    @welcome_group.command(name="test", description="Trigger a test welcome event for yourself")
    async def test_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        settings = await self.get_settings(guild_id)
        if not settings:
            err = CynexCloudErrorContainer("Not Configured", "Welcome system is not set up on this server.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        # Fire member join listener logic on the caller
        await self.on_member_join(interaction.user)
        
        success = CynexCloudSuccessContainer("Test Executed", "A test welcome banner has been generated. Please check DMs and the welcome channels.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="disable", description="Disable welcome system greetings")
    @app_commands.checks.has_permissions(administrator=True)
    async def disable_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM welcome_settings WHERE guild_id = ?", (guild_id,))
            await db.commit()

        success = CynexCloudSuccessContainer("Welcome System Disabled", "The welcome system has been disabled and cleared.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="edit", description="Edit welcome message text or rules guidelines text")
    @app_commands.describe(
        message_type="Type of parameter to update",
        value="New parameter text (Supports brackets: {mention}, {server}, etc.)"
    )
    @app_commands.choices(message_type=[
        app_commands.Choice(name="Welcome Message", value="welcome_message"),
        app_commands.Choice(name="Rules Guidelines", value="rules_text")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def edit_welcome(self, interaction: discord.Interaction, message_type: str, value: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        settings = await self.get_settings(guild_id)
        if not settings:
            err = CynexCloudErrorContainer("Not Configured", "Welcome system is not set up. Run `/welcome setup` first.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            if message_type == "welcome_message":
                await db.execute("UPDATE welcome_settings SET welcome_message = ? WHERE guild_id = ?", (value, guild_id))
            elif message_type == "rules_text":
                await db.execute("UPDATE welcome_settings SET rules_text = ? WHERE guild_id = ?", (value, guild_id))
            await db.commit()

        success = CynexCloudSuccessContainer("Welcome Property Updated", f"Property `{message_type}` set to: **{value}**")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="premade", description="Apply a premade welcome message template")
    @app_commands.describe(
        template="Choose a predefined theme for the welcome message"
    )
    @app_commands.choices(template=[
        app_commands.Choice(name="Community Focus (Friendly checklist)", value="community"),
        app_commands.Choice(name="Enterprise Hosting (Client-centric)", value="enterprise"),
        app_commands.Choice(name="Gaming Guild (Lobby/action-centric)", value="gaming"),
        app_commands.Choice(name="Minimalist (Sleek and clean)", value="minimalist")
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_premade(self, interaction: discord.Interaction, template: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        settings = await self.get_settings(guild_id)
        if not settings:
            err = CynexCloudErrorContainer("Not Configured", "Welcome system is not set up. Run `/welcome setup` first.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        templates = {
            "community": (
                "👋 **Welcome {mention} to {server}!**\n"
                "We are thrilled to have you join our community.\n\n"
                "💡 **Getting Started Checklist:**\n"
                "• Visit our website to check hosting services.\n"
                "• View guidelines by clicking the **📜 Rules** button below.\n"
                "• Need help? Create a support ticket with **🎫 Support**.\n\n"
                "*We now have {membercount} members!*"
            ),
            "enterprise": (
                "💼 **Welcome to the {server} client portal, {mention}!**\n"
                "We are dedicated to providing enterprise-grade hosting services. Follow our quick-start checklist:\n\n"
                "• Check out our billing panel and services list.\n"
                "• Click **📜 Rules** to read our Terms of Service (ToS).\n"
                "• Open a billing or support inquiry via **🎫 Support**.\n\n"
                "Thank you for choosing CynexCloud! *Total clients: {membercount}*"
            ),
            "gaming": (
                "🎮 **Welcome {mention} to the {server} lobby!**\n"
                "Prepare your loadout and join the battle! Make sure to follow the guidelines:\n\n"
                "• Team up and find active groups in our discord channels.\n"
                "• Click **📜 Rules** to avoid getting banned or muted.\n"
                "• Need assistance or want to report a player? Use **🎫 Support**.\n\n"
                "Let the game begin! *Player Count: {membercount}* ⚔️"
            ),
            "minimalist": (
                "👋 **Welcome {mention} to {server}.**\n\n"
                "• Check announcements for latest news & updates.\n"
                "• Click **📜 Rules** to read server guidelines.\n"
                "• Reach out to staff using **🎫 Support**.\n\n"
                "*Member Count: {membercount}*"
            )
        }

        selected_message = templates.get(template)
        if not selected_message:
            err = CynexCloudErrorContainer("Invalid Template", "The requested template could not be found.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE welcome_settings SET welcome_message = ? WHERE guild_id = ?", (selected_message, guild_id))
            await db.commit()

        preview_msg = translate_welcome_variables(selected_message, interaction.user)
        success = CynexCloudSuccessContainer("Premade Welcome Message Applied", f"Theme `{template}` applied. Preview:")
        success.add_section("Greetings Preview", preview_msg)
        await interaction.followup.send(view=success.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
