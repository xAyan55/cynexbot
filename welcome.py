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
    BreezeContainerBuilder,
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeWarningContainer,
    BreezeInfoContainer
)

logger = logging.getLogger("Breeze.Welcome")
DB_PATH = "breeze.db"

# ══════════════════════════════════════════════════════════════════════
# WELCOME VARIABLE HELPER
# ══════════════════════════════════════════════════════════════════════

async def translate_welcome_variables(text: str, member: discord.Member) -> str:
    """Translates placeholder brackets with dynamic member details."""
    if not text:
        return ""
    
    created_epoch = int(member.created_at.timestamp())
    created_str = f"<t:{created_epoch}:F> (<t:{created_epoch}:R>)"
    
    joined_at = member.joined_at or datetime.now(timezone.utc)
    joined_epoch = int(joined_at.timestamp())
    joined_str = f"<t:{joined_epoch}:F> (<t:{joined_epoch}:R>)"
    
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    
    # Calculate stats
    humans_count = sum(1 for m in member.guild.members if not m.bot)
    bots_count = sum(1 for m in member.guild.members if m.bot)
    
    # Try to fetch invite data from DB
    inviter_name = "Unknown"
    inviter_mention = "Unknown"
    invite_code = "Unknown"
    invite_uses = "0"
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT inviter_user_id, invite_code FROM invited_by WHERE guild_id = ? AND invited_user_id = ?", (str(member.guild.id), str(member.id))) as cursor:
            row = await cursor.fetchone()
            if row:
                inv_id, invite_code = row
                if inv_id == "Vanity URL":
                    inviter_name = "Vanity URL"
                    inviter_mention = "Vanity URL"
                elif inv_id != "Unknown":
                    # Try to fetch inviter object
                    inv_user = member.guild.get_member(int(inv_id))
                    if not inv_user:
                        try:
                            inv_user = await member.guild.fetch_member(int(inv_id))
                        except Exception:
                            pass
                    if inv_user:
                        inviter_name = str(inv_user)
                        inviter_mention = inv_user.mention
                    else:
                        inviter_name = f"User ID: {inv_id}"
                        inviter_mention = f"<@{inv_id}>"

        # Fetch invite uses if invite code is known
        if invite_code != "Unknown":
            if inviter_name == "Vanity URL":
                try:
                    vanity = await member.guild.vanity_invite()
                    invite_uses = str(vanity.uses)
                except Exception:
                    pass
            else:
                async with db.execute("SELECT COUNT(*) FROM invited_by WHERE guild_id = ? AND invite_code = ?", (str(member.guild.id), invite_code)) as c:
                    uses_row = await c.fetchone()
                    invite_uses = str(uses_row[0]) if uses_row else "0"
                    
    replacements = {
        # Member placeholders
        "{user}": str(member),
        "{user.name}": member.name,
        "{user.mention}": member.mention,
        "{user.id}": str(member.id),
        "{user.avatar}": member.display_avatar.url if member.display_avatar else "",
        "{user.created_at}": f"<t:{created_epoch}:F>",
        "{username}": member.name,
        "{mention}": member.mention,
        "{created}": created_str,
        
        # Server placeholders
        "{server}": member.guild.name,
        "{server.name}": member.guild.name,
        "{server.id}": str(member.guild.id),
        "{server.member_count}": str(member.guild.member_count),
        "{server.boosts}": str(member.guild.premium_subscription_count),
        "{server.boost_level}": str(member.guild.premium_tier),
        
        # Invite placeholders
        "{inviter}": inviter_name,
        "{inviter.mention}": inviter_mention,
        "{invite.code}": invite_code,
        "{invite.uses}": invite_uses,
        
        # Time placeholders
        "{time}": f"<t:{now_epoch}:T>",
        "{date}": f"<t:{now_epoch}:d>",
        "{joined_at}": f"<t:{joined_epoch}:F>",
        "{joined}": joined_str,
        
        # Statistics placeholders
        "{membercount}": str(member.guild.member_count),
        "{humans}": str(humans_count),
        "{bots}": str(bots_count)
    }
    
    for key, val in replacements.items():
        text = text.replace(key, val)
    return text


# ══════════════════════════════════════════════════════════════════════
# PUBLIC WELCOME INTERACTION CALLBACK
# ══════════════════════════════════════════════════════════════════════

async def on_interaction(interaction: discord.Interaction):
    custom_id = interaction.data.get("custom_id") if interaction.data else None
    if not custom_id or not custom_id.startswith("breeze:welcome:"):
        return

    action = custom_id.split(":")[2]
    await interaction.response.defer(ephemeral=True)

    if action == "rules":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT rules_text FROM welcome_settings WHERE guild_id = ?", (str(interaction.guild_id),)) as cursor:
                row = await cursor.fetchone()
                
        rules_text = row[0] if row and row[0] else "📜 Please check the server rules channels for details."
        info = BreezeInfoContainer("Server Rules", rules_text)
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "support":
        # Check if tickets system is loaded
        tickets_cog = interaction.client.get_cog("TicketGroup") # app commands group is not a Cog, tickets is usually registered
        # We can send information about tickets
        info = BreezeInfoContainer(
            "Breeze Help Desk Support",
            "🎫 Need assistance? Run the `/ticket panel` or `/ticket setup` command to contact staff."
        )
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "website":
        info = BreezeInfoContainer(
            "Breeze Web Portal",
            "🌐 Visit our official web site at: **https://breeze.dev**"
        )
        await interaction.followup.send(view=info.build(), ephemeral=True)

    elif action == "announce":
        info = BreezeInfoContainer(
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
                    log_layout = BreezeInfoContainer(
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
                        await member.add_roles(role, reason="Breeze Welcome Auto-Role Assignment")
                    except Exception as role_err:
                        logger.warning(f"Failed to assign auto-role {role_id} to {member}: {role_err}")

        # 6. Wait for InviteTracker to write invite details to DB
        for _ in range(4):
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT 1 FROM invited_by WHERE guild_id = ? AND invited_user_id = ?", (guild_id, user_id)) as cursor:
                    if await cursor.fetchone():
                        break
            await asyncio.sleep(0.5)

        # 7. Construct V2 Welcome Container Message
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
        translated_msg = await translate_welcome_variables(welcome_txt, member)

        # Build V2 Welcome Layout View using BreezeContainerBuilder
        builder = BreezeContainerBuilder(
            title=f"Welcome to {member.guild.name}",
            description="Fast. Reliable. Always Online.",
            accent_color=3447003,
            thumbnail_url=member.display_avatar.url if member.display_avatar else None
        )
        builder.add_section("🌿 Greeting Section", translated_msg)
        builder.add_section("👤 Member Information", f"You are member number **{member.guild.member_count}**.")
        builder.add_section("🏠 Server Information", "Please click the Rules button below or check the server rules channels to ensure guidelines are followed.")
        
        btn_rules = Button(label="📜 Rules", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:rules:{member.id}")
        btn_support = Button(label="🎫 Support", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:support:{member.id}")
        btn_web = Button(label="🌐 Website", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:website:{member.id}")
        btn_announce = Button(label="📢 Announcements", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:announce:{member.id}")
        builder.add_buttons(btn_rules, btn_support, btn_web, btn_announce)
        
        welcome_layout = builder.build()

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
                    await welcome_ch.send(view=welcome_layout)
                except Exception as e:
                    logger.error(f"Failed to send welcome message to channel: {e}", exc_info=True)

        # 8. Send Welcome DM
        if settings["dm_enabled"]:
            try:
                await member.send(view=welcome_layout)
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
                    log_layout = BreezeWarningContainer(
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

    welcome_group = app_commands.Group(name="welcome", description="Breeze greeting and auto-roles setup panel")

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

        success = BreezeSuccessContainer(
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
            err = BreezeErrorContainer("Not Configured", "Welcome system is not set up on this server.")
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
        translated_msg = await translate_welcome_variables(welcome_txt, interaction.user)

        # Build V2 Welcome Layout View using BreezeContainerBuilder
        builder = BreezeContainerBuilder(
            title=f"Welcome to {interaction.guild.name}",
            description="Fast. Reliable. Always Online.",
            accent_color=3447003,
            thumbnail_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None
        )
        builder.add_section("🌿 Greeting Section", translated_msg)
        builder.add_section("👤 Member Information", f"You are member number **{interaction.guild.member_count}**.")
        builder.add_section("🏠 Server Information", "Please click the Rules button below or check the server rules channels to ensure guidelines are followed.")
        
        btn_rules = Button(label="📜 Rules", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:rules:{interaction.user.id}")
        btn_support = Button(label="🎫 Support", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:support:{interaction.user.id}")
        btn_web = Button(label="🌐 Website", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:website:{interaction.user.id}")
        btn_announce = Button(label="📢 Announcements", style=discord.ButtonStyle.secondary, custom_id=f"breeze:welcome:announce:{interaction.user.id}")
        builder.add_buttons(btn_rules, btn_support, btn_web, btn_announce)
        
        welcome_layout = builder.build()

        await interaction.followup.send(view=welcome_layout, ephemeral=True)

    @welcome_group.command(name="test", description="Trigger a test welcome event for yourself")
    async def test_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        
        settings = await self.get_settings(guild_id)
        if not settings:
            err = BreezeErrorContainer("Not Configured", "Welcome system is not set up on this server.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        # Fire member join listener logic on the caller
        await self.on_member_join(interaction.user)
        
        success = BreezeSuccessContainer("Test Executed", "A test welcome banner has been generated. Please check DMs and the welcome channels.")
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="disable", description="Disable welcome system greetings")
    @app_commands.checks.has_permissions(administrator=True)
    async def disable_welcome(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM welcome_settings WHERE guild_id = ?", (guild_id,))
            await db.commit()

        success = BreezeSuccessContainer("Welcome System Disabled", "The welcome system has been disabled and cleared.")
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
            err = BreezeErrorContainer("Not Configured", "Welcome system is not set up. Run `/welcome setup` first.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            if message_type == "welcome_message":
                await db.execute("UPDATE welcome_settings SET welcome_message = ? WHERE guild_id = ?", (value, guild_id))
            elif message_type == "rules_text":
                await db.execute("UPDATE welcome_settings SET rules_text = ? WHERE guild_id = ?", (value, guild_id))
            await db.commit()

        success = BreezeSuccessContainer("Welcome Property Updated", f"Property `{message_type}` set to: **{value}**")
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
            err = BreezeErrorContainer("Not Configured", "Welcome system is not set up. Run `/welcome setup` first.")
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
                "Thank you for choosing Breeze! *Total clients: {membercount}*"
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
            err = BreezeErrorContainer("Invalid Template", "The requested template could not be found.")
            await interaction.followup.send(view=err.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE welcome_settings SET welcome_message = ? WHERE guild_id = ?", (selected_message, guild_id))
            await db.commit()

        preview_msg = await translate_welcome_variables(selected_message, interaction.user)
        success = BreezeSuccessContainer("Premade Welcome Message Applied", f"Theme `{template}` applied. Preview:")
        success.add_section("Greetings Preview", preview_msg)
        await interaction.followup.send(view=success.build(), ephemeral=True)

    @welcome_group.command(name="variables", description="Display every supported placeholder available for the welcome system")
    @app_commands.checks.has_permissions(administrator=True)
    async def welcome_variables(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # Build the visual container organizing variables into categories
        builder = BreezeContainerBuilder("Welcome Variables", "List of all supported placeholders you can use in your welcome messages.")
        
        builder.add_section("👤 Member Variables", 
            "`{user}` — Full username (e.g. Username#0000)\n"
            "`{user.name}` — Current username\n"
            "`{user.mention}` — Mentions the joining user\n"
            "`{user.id}` — Discord ID of the user\n"
            "`{user.avatar}` — Avatar URL of the user\n"
            "`{user.created_at}` — Account creation timestamp"
        )
        
        builder.add_section("🍃 Server Variables",
            "`{server}` or `{server.name}` — Server name\n"
            "`{server.id}` — Server Discord ID\n"
            "`{server.member_count}` — Total members count\n"
            "`{server.boosts}` — Total server boosts\n"
            "`{server.boost_level}` — Server boost level (tier)"
        )
        
        builder.add_section("✉️ Invite Variables",
            "`{inviter}` — Inviter's name or Vanity URL\n"
            "`{inviter.mention}` — Inviter's mention or ID link\n"
            "`{invite.code}` — The invite code used to join\n"
            "`{invite.uses}` — Number of times that invite code was used"
        )
        
        builder.add_section("⏰ Time Variables",
            "`{time}` — Current timestamp (long time)\n"
            "`{date}` — Current timestamp (short date)\n"
            "`{joined_at}` — User's join timestamp"
        )
        
        builder.add_section("📊 Statistics Variables",
            "`{membercount}` — Total members in the server\n"
            "`{humans}` — Count of human members\n"
            "`{bots}` — Count of bot accounts"
        )
        
        builder.add_section("⚠️ Legacy Support (Backward Compatible)",
            "`{username}` ➔ Same as `{user.name}`\n"
            "`{mention}` ➔ Same as `{user.mention}`\n"
            "`{created}` ➔ Same as `{user.created_at}` with relative time\n"
            "`{joined}` ➔ Same as `{joined_at}` with relative time"
        )
        
        await interaction.followup.send(view=builder.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
