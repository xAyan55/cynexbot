import re
import logging
from datetime import datetime, timedelta
from typing import Optional

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands

from ui import (
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudWarningContainer,
    CynexCloudInfoContainer
)

logger = logging.getLogger("CynexCloud.AntiSwear")
DB_PATH = "cynex.db"

# ══════════════════════════════════════════════════════════════════════
# DATABASE UTILITIES & CACHING FOR ANTI-SWEAR
# ══════════════════════════════════════════════════════════════════════

class AntiSwearCache:
    def __init__(self):
        self.settings = {}   # guild_id -> dict
        self.words = {}      # guild_id -> set of words
        self.roles = {}      # guild_id -> set of role_ids
        self.channels = {}   # guild_id -> set of channel_ids

    def invalidate(self, guild_id: str):
        self.settings.pop(guild_id, None)
        self.words.pop(guild_id, None)
        self.roles.pop(guild_id, None)
        self.channels.pop(guild_id, None)

cache = AntiSwearCache()

async def get_settings(guild_id: str) -> dict:
    if guild_id in cache.settings:
        return cache.settings[guild_id]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT enabled, max_warnings, timeout_duration, use_regex FROM antiswear_settings WHERE guild_id = ?", (guild_id,)) as c:
            row = await c.fetchone()
            if row:
                settings = {"enabled": row[0], "max_warnings": row[1], "timeout_duration": row[2], "use_regex": row[3]}
            else:
                # Default settings
                settings = {"enabled": 0, "max_warnings": 3, "timeout_duration": 600, "use_regex": 0}
                await db.execute("INSERT OR IGNORE INTO antiswear_settings (guild_id) VALUES (?)", (guild_id,))
                await db.commit()
    cache.settings[guild_id] = settings
    return settings

async def get_words(guild_id: str) -> set:
    if guild_id in cache.words:
        return cache.words[guild_id]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT word FROM antiswear_words WHERE guild_id = ?", (guild_id,)) as c:
            rows = await c.fetchall()
            words = {r[0] for r in rows}
    cache.words[guild_id] = words
    return words

async def get_roles(guild_id: str) -> set:
    if guild_id in cache.roles:
        return cache.roles[guild_id]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role_id FROM antiswear_roles WHERE guild_id = ?", (guild_id,)) as c:
            rows = await c.fetchall()
            roles = {r[0] for r in rows}
    cache.roles[guild_id] = roles
    return roles

async def get_channels(guild_id: str) -> set:
    if guild_id in cache.channels:
        return cache.channels[guild_id]
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id FROM antiswear_channels WHERE guild_id = ?", (guild_id,)) as c:
            rows = await c.fetchall()
            channels = {r[0] for r in rows}
    cache.channels[guild_id] = channels
    return channels

async def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT log_channel_id FROM ticket_configs WHERE guild_id = ?", (str(guild.id),)) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    ch = guild.get_channel(int(row[0]))
                    if ch: return ch
            async with db.execute("SELECT log_channel_id FROM welcome_settings WHERE guild_id = ?", (str(guild.id),)) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    ch = guild.get_channel(int(row[0]))
                    if ch: return ch
    except Exception as e:
        logger.warning(f"[AntiSwear] Error retrieving log channel for guild {guild.id}: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════
# TEXT NORMALIZATION / FILTERING
# ══════════════════════════════════════════════════════════════════════

def normalize_text(text: str) -> str:
    # Lowercase & strip all punctuation, spaces, numbers, and symbols
    normalized = re.sub(r'[\s\d_.,\-*~/\\|!@#$%^&()+=:;\'"?[\]{}]', '', text.lower())
    # Squash repeated characters (e.g., ssswweeeaaarrr -> swear)
    normalized = re.sub(r'(.)\1+', r'\1', normalized)
    return normalized

# ══════════════════════════════════════════════════════════════════════
# MODERATION COG
# ══════════════════════════════════════════════════════════════════════

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def log_moderation(self, guild: discord.Guild, user: discord.Member, action: str, details: str):
        log_ch = await get_log_channel(guild)
        if log_ch:
            embed = discord.Embed(
                title="🛡️ CynexCloud Moderation Alert",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
            embed.add_field(name="Action", value=action, inline=True)
            embed.add_field(name="Details", value=details, inline=False)
            embed.set_footer(text="[AntiSwear] Log System")
            try:
                await log_ch.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"[AntiSwear] Missing permissions to write in log channel {log_ch.id} (Guild: {guild.id})")
        logger.info(f"[AntiSwear] Action={action} | Guild={guild.id} | User={user.id} | Details={details}")

    # Helper checking permissions before performing actions
    def check_bot_moderation_permissions(self, interaction: discord.Interaction) -> Optional[str]:
        guild = interaction.guild
        if not guild:
            return "This command can only be used inside servers."
        if not guild.me.guild_permissions.moderate_members:
            return "❌ CynexCloud requires **Timeout Members (Moderate Members)** permission to function fully."
        if not guild.me.guild_permissions.manage_messages:
            return "❌ CynexCloud requires **Manage Messages** permission to delete flagged contents."
        return None

    # Swear Detection Hook
    async def process_anti_swear(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        
        # Bypass administrators
        if message.author.guild_permissions.administrator:
            return

        guild_id = str(message.guild.id)
        settings = await get_settings(guild_id)
        if not settings["enabled"]:
            return

        # Check channel whitelist
        whitelisted_channels = await get_channels(guild_id)
        if str(message.channel.id) in whitelisted_channels:
            return

        # Check role whitelist
        whitelisted_roles = await get_roles(guild_id)
        user_role_ids = {str(r.id) for r in message.author.roles}
        if user_role_ids.intersection(whitelisted_roles):
            return

        banned_words = await get_words(guild_id)
        if not banned_words:
            return

        content = message.content
        triggered = False
        matched_word = ""

        if settings["use_regex"]:
            for pattern in banned_words:
                try:
                    if re.search(pattern, content, re.IGNORECASE):
                        triggered = True
                        matched_word = pattern
                        break
                except re.error as e:
                    logger.warning(f"[AntiSwear] Invalid regex pattern '{pattern}' in guild {guild_id}: {e}")
        else:
            normalized = normalize_text(content)
            for word in banned_words:
                norm_word = normalize_text(word)
                if norm_word and norm_word in normalized:
                    triggered = True
                    matched_word = word
                    break

        if triggered:
            # Try deleting the message
            try:
                await message.delete()
            except discord.Forbidden:
                logger.warning(f"[AntiSwear] Forbidden: Could not delete message {message.id} in guild {guild_id}")
                return
            except discord.NotFound:
                return

            # Warn the user
            user_id = str(message.author.id)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO antiswear_warnings (guild_id, user_id, warning_count, last_warned_at)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    warning_count = warning_count + 1,
                    last_warned_at = CURRENT_TIMESTAMP
                """, (guild_id, user_id))
                await db.commit()

                async with db.execute("SELECT warning_count FROM antiswear_warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as cursor:
                    row = await cursor.fetchone()
                    warnings = row[0] if row else 1

            max_warnings = settings["max_warnings"]
            timeout_dur = settings["timeout_duration"]

            # Log to logs
            await self.log_moderation(
                message.guild, 
                message.author, 
                "Flagged Swearing", 
                f"Matched word: `{matched_word}` (Warning {warnings}/{max_warnings})\nContent: ||{content}||"
            )

            # Warning response
            if warnings >= max_warnings:
                # Timeout the member
                if message.guild.me.guild_permissions.moderate_members:
                    try:
                        # Reset warnings
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE antiswear_warnings SET warning_count = 0 WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
                            await db.commit()

                        # Apply timeout
                        await message.author.timeout(timedelta(seconds=timeout_dur), reason=f"CynexCloud Anti-Swear warning limit exceeded ({warnings}/{max_warnings})")
                        
                        # Ephemeral alert
                        try:
                            alert = CynexCloudWarningContainer(
                                "User Muted / Timed Out",
                                f"{message.author.mention} was timed out for **{timeout_dur // 60} minutes** for exceeding the bad language warning limit."
                            )
                            await message.channel.send(view=alert.build(), delete_after=15)
                        except Exception:
                            pass
                        
                        await self.log_moderation(
                            message.guild,
                            message.author,
                            "User Timeout Applied",
                            f"Warnings reached `{warnings}/{max_warnings}` limit. Applied timeout of `{timeout_dur}` seconds."
                        )
                    except Exception as e:
                        logger.error(f"[AntiSwear] Failed to apply timeout to {user_id} in guild {guild_id}: {e}")
                else:
                    logger.warning(f"[AntiSwear] Cannot timeout member {user_id} - missing moderate_members permission in guild {guild_id}")
            else:
                # Warn user
                try:
                    warn = CynexCloudWarningContainer(
                        "Language Warning",
                        f"{message.author.mention}, please watch your language! Swearing is not allowed here.\n*Warning **{warnings}** of **{max_warnings}***"
                    )
                    await message.channel.send(view=warn.build(), delete_after=10)
                except Exception:
                    pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self.process_anti_swear(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self.process_anti_swear(after)

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    antiswear = app_commands.Group(name="antiswear", description="CynexCloud server anti-swear moderation settings", default_permissions=discord.Permissions(administrator=True))

    @antiswear.command(name="enable", description="Enable anti-swear word filtering on the server")
    async def antiswear_enable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        err = self.check_bot_moderation_permissions(interaction)
        if err:
            card = CynexCloudErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO antiswear_settings (guild_id, enabled) VALUES (?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET enabled = 1
            """, (guild_id,))
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("System Enabled", "✅ Anti-Swear filtering has been successfully **enabled**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="disable", description="Disable anti-swear word filtering on the server")
    async def antiswear_disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO antiswear_settings (guild_id, enabled) VALUES (?, 0)
                ON CONFLICT(guild_id) DO UPDATE SET enabled = 0
            """, (guild_id,))
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("System Disabled", "⚠️ Anti-Swear filtering has been **disabled**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="add", description="Add a word to the server's anti-swear filter")
    async def antiswear_add(self, interaction: discord.Interaction, word: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        word_clean = word.strip()
        if not word_clean:
            card = CynexCloudErrorContainer("Invalid Word", "The provided word cannot be blank.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO antiswear_words (guild_id, word) VALUES (?, ?)", (guild_id, word_clean))
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("Word Added", f"Added `{word_clean}` to the blocked words list.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="remove", description="Remove a word from the server's anti-swear filter")
    async def antiswear_remove(self, interaction: discord.Interaction, word: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM antiswear_words WHERE guild_id = ? AND word = ?", (guild_id, word))
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("Word Removed", f"Removed `{word}` from the blocked words list.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="list", description="Show all configured blocked words")
    async def antiswear_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        words = await get_words(guild_id)
        settings = await get_settings(guild_id)

        status = "Enabled" if settings["enabled"] else "Disabled"
        regex_mode = "Regex Pattern Matching" if settings["use_regex"] else "Standard String Matching"
        
        desc = f"• **Filter Status**: `{status}`\n• **Matching Engine**: `{regex_mode}`\n\n**Blocked Words List:**\n"
        if words:
            desc += ", ".join([f"`{w}`" for w in sorted(words)])
        else:
            desc += "*No words are currently added to the filter.*"

        card = CynexCloudInfoContainer("Anti-Swear Filter List", desc)
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="regex", description="Toggle regex-based matching for the blocked words list")
    async def antiswear_regex(self, interaction: discord.Interaction, mode: bool):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        val = 1 if mode else 0
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE antiswear_settings SET use_regex = ? WHERE guild_id = ?", (val, guild_id))
            await db.commit()
        cache.invalidate(guild_id)

        mode_str = "Regex patterns" if mode else "Standard strings"
        card = CynexCloudSuccessContainer("Engine Changed", f"Anti-Swear word matching updated to use **{mode_str}**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    # Whitelist command group
    whitelist = app_commands.Group(name="whitelist", description="Configure whitelisted bypass roles and channels for anti-swear")

    @whitelist.command(name="role", description="Toggle whether a role is bypassed by the anti-swear filter")
    async def whitelist_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        role_id = str(role.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM antiswear_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)) as c:
                exists = await c.fetchone()
            if exists:
                await db.execute("DELETE FROM antiswear_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
                action_str = "removed from whitelist"
            else:
                await db.execute("INSERT INTO antiswear_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
                action_str = "added to whitelist"
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("Role Whitelist Updated", f"Role {role.mention} has been **{action_str}**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @whitelist.command(name="channel", description="Toggle whether a channel is bypassed by the anti-swear filter")
    async def whitelist_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        channel_id = str(channel.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM antiswear_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)) as c:
                exists = await c.fetchone()
            if exists:
                await db.execute("DELETE FROM antiswear_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
                action_str = "removed from whitelist"
            else:
                await db.execute("INSERT INTO antiswear_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
                action_str = "added to whitelist"
            await db.commit()
        cache.invalidate(guild_id)

        card = CynexCloudSuccessContainer("Channel Whitelist Updated", f"Channel {channel.mention} has been **{action_str}**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="warnings", description="Check the number of active swearing warnings for a member")
    async def check_warnings(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT warning_count FROM antiswear_warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as c:
                row = await c.fetchone()
                warnings = row[0] if row else 0

        card = CynexCloudInfoContainer("Swear Warning Count", f"{member.mention} currently has **{warnings}** active language warning(s).")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @antiswear.command(name="clearwarnings", description="Reset swearing warnings for a member")
    async def clear_warnings(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM antiswear_warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            await db.commit()

        card = CynexCloudSuccessContainer("Warnings Reset", f"Successfully cleared language warnings for {member.mention}.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
