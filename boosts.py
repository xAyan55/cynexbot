import logging
from datetime import datetime, timezone
from typing import Optional

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands

from ui import (
    CynexCloudSuccessContainer,
    CynexCloudErrorContainer,
    CynexCloudInfoContainer,
    CynexCloudWarningContainer,
    CynexCloudPaginationContainer
)

logger = logging.getLogger("CynexCloud.BoostTracker")
DB_PATH = "fb.db"

# ══════════════════════════════════════════════════════════════════════
# DATABASE OPERATIONS FOR BOOSTS
# ══════════════════════════════════════════════════════════════════════

async def get_boost_config(guild_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id, role_id, highest_tier FROM boost_alert_configs WHERE guild_id = ?", (guild_id,)) as c:
            row = await c.fetchone()
            if row:
                return {"channel_id": row[0], "role_id": row[1], "highest_tier": row[2]}
    return None

# ══════════════════════════════════════════════════════════════════════
# COG IMPLEMENTATION
# ══════════════════════════════════════════════════════════════════════

class BoostTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def log_boost_history(self, guild_id: str, user_id: str, action: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_history (guild_id, user_id, action, timestamp)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """, (guild_id, user_id, action))
            await db.commit()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        guild_id = str(guild.id)
        user_id = str(after.id)

        # Detect start boosting
        if before.premium_since is None and after.premium_since is not None:
            logger.info(f"[BoostTracker] User {after.id} started boosting guild {guild.id}")
            
            # Record in boost track
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO boost_track (guild_id, user_id, first_boost_date, total_boosts, is_boosting)
                    VALUES (?, ?, CURRENT_TIMESTAMP, 1, 1)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    total_boosts = total_boosts + 1,
                    is_boosting = 1
                """, (guild_id, user_id))
                await db.commit()

            await self.log_boost_history(guild_id, user_id, "started")

            # Check config
            config = await get_boost_config(guild_id)
            if config:
                # Send announcement
                ch = guild.get_channel(int(config["channel_id"])) if config["channel_id"] else None
                if ch:
                    alert = CynexCloudInfoContainer(
                        "🎉 Server Boosted!",
                        f"Thank you so much to {after.mention} for boosting our server! ❤️\n"
                        f"We now have **{guild.premium_subscription_count}** boosts!"
                    )
                    try:
                        await ch.send(view=alert.build())
                    except discord.Forbidden:
                        logger.warning(f"[BoostTracker] Forbidden: Cannot write boost alert in {ch.id}")

                # Thank-you role assignment
                if config["role_id"]:
                    role = guild.get_role(int(config["role_id"]))
                    if role and guild.me.guild_permissions.manage_roles and role < guild.me.top_role:
                        try:
                            await after.add_roles(role, reason="CynexCloud Booster Alert thank-you assignment")
                        except Exception as e:
                            logger.error(f"[BoostTracker] Failed to add thank-you role {role.id} to booster {user_id}: {e}")

        # Detect stop boosting
        elif before.premium_since is not None and after.premium_since is None:
            logger.info(f"[BoostTracker] User {after.id} stopped boosting guild {guild.id}")
            
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    UPDATE boost_track SET is_boosting = 0 
                    WHERE guild_id = ? AND user_id = ?
                """, (guild_id, user_id))
                await db.commit()

            await self.log_boost_history(guild_id, user_id, "stopped")

            config = await get_boost_config(guild_id)
            if config:
                ch = guild.get_channel(int(config["channel_id"])) if config["channel_id"] else None
                if ch:
                    alert = CynexCloudWarningContainer(
                        "💔 Server Boost Removed",
                        f"{after.mention} is no longer boosting. We now have **{guild.premium_subscription_count}** boosts."
                    )
                    try:
                        await ch.send(view=alert.build())
                    except discord.Forbidden:
                        pass

                # Thank-you role removal
                if config["role_id"]:
                    role = guild.get_role(int(config["role_id"]))
                    if role and guild.me.guild_permissions.manage_roles and role < guild.me.top_role:
                        try:
                            await after.remove_roles(role, reason="CynexCloud Booster Alert thank-you removal")
                        except Exception as e:
                            logger.error(f"[BoostTracker] Failed to remove thank-you role {role.id} from former booster {user_id}: {e}")

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        guild_id = str(after.id)
        
        # Check Level Tier Changes
        if before.premium_tier != after.premium_tier:
            logger.info(f"[BoostTracker] Guild {after.id} premium tier changed from {before.premium_tier} to {after.premium_tier}")
            
            config = await get_boost_config(guild_id)
            if config:
                highest_tier = config["highest_tier"]
                new_highest = highest_tier
                
                # Check if level went up
                if after.premium_tier > before.premium_tier:
                    if after.premium_tier > highest_tier:
                        new_highest = after.premium_tier
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("UPDATE boost_alert_configs SET highest_tier = ? WHERE guild_id = ?", (new_highest, guild_id))
                            await db.commit()

                    ch = after.get_channel(int(config["channel_id"])) if config["channel_id"] else None
                    if ch:
                        alert = CynexCloudSuccessContainer(
                            "🚀 Server Tier Upgraded!",
                            f"Congratulations! Our server premium tier has upgraded to **Level {after.premium_tier}**!\n"
                            f"We currently have **{after.premium_subscription_count}** total boosts!\n"
                            f"*Highest level reached historically: Level {new_highest}*"
                        )
                        try:
                            await ch.send(view=alert.build())
                        except discord.Forbidden:
                            pass

                # Check if level went down
                elif after.premium_tier < before.premium_tier:
                    ch = after.get_channel(int(config["channel_id"])) if config["channel_id"] else None
                    if ch:
                        alert = CynexCloudWarningContainer(
                            "⚠️ Server Tier Downgraded",
                            f"Our server premium tier has dropped to **Level {after.premium_tier}** due to a boost removal.\n"
                            f"We currently have **{after.premium_subscription_count}** boosts remaining."
                        )
                        try:
                            await ch.send(view=alert.build())
                        except discord.Forbidden:
                            pass

    # Helper checking permissions before performing actions
    def check_bot_boost_permissions(self, interaction: discord.Interaction) -> Optional[str]:
        guild = interaction.guild
        if not guild:
            return "This command can only be used inside servers."
        return None

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    boostalerts = app_commands.Group(name="boostalerts", description="CynexCloud Server Boost Alert Settings", default_permissions=discord.Permissions(administrator=True))

    @boostalerts.command(name="setup", description="Configure server boost announcements channel and thank-you role")
    async def boostalerts_setup(self, interaction: discord.Interaction, channel: discord.TextChannel, role: Optional[discord.Role] = None):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        role_id = str(role.id) if role else None

        # Validate bot manage roles permission if role is specified
        if role:
            if not interaction.guild.me.guild_permissions.manage_roles:
                card = CynexCloudErrorContainer("Missing Bot Permissions", "❌ CynexCloud requires **Manage Roles** permission to assign the thank-you role.")
                await interaction.followup.send(view=card.build(), ephemeral=True)
                return
            if role >= interaction.guild.me.top_role:
                card = CynexCloudErrorContainer("Invalid Role", f"❌ Role {role.mention} is higher than CynexCloud's highest role. Please move CynexCloud's role above it in Server Settings.")
                await interaction.followup.send(view=card.build(), ephemeral=True)
                return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO boost_alert_configs (guild_id, channel_id, role_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = ?,
                role_id = ?
            """, (guild_id, str(channel.id), role_id, str(channel.id), role_id))
            await db.commit()

        role_mention = role.mention if role else "`None`"
        card = CynexCloudSuccessContainer(
            "Boost Alerts Configured",
            f"Announcements will be posted to {channel.mention}.\n"
            f"Thank-you role to assign: {role_mention}"
        )
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @boostalerts.command(name="disable", description="Disable server boost alerts and role assignments")
    async def boostalerts_disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM boost_alert_configs WHERE guild_id = ?", (guild_id,))
            await db.commit()

        card = CynexCloudSuccessContainer("Boost Alerts Disabled", "Server boost alert announcements have been successfully **disabled**.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    boost = app_commands.Group(name="boost", description="CynexCloud Server Booster stats and records")

    @boost.command(name="stats", description="Display current guild server boost tier, counts, and stats")
    async def boost_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        guild_id = str(guild.id)

        # Retrieve highest tier achieved
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT highest_tier FROM boost_alert_configs WHERE guild_id = ?", (guild_id,)) as c:
                row = await c.fetchone()
                highest_tier = row[0] if row else 0

            # Count total boosters logged
            async with db.execute("SELECT COUNT(*) FROM boost_track WHERE guild_id = ? AND is_boosting = 1", (guild_id,)) as c:
                active_boosters = await c.fetchone()[0]
                
            async with db.execute("SELECT COUNT(DISTINCT user_id) FROM boost_track WHERE guild_id = ?", (guild_id,)) as c:
                total_historical_boosters = await c.fetchone()[0]

        desc = (
            f"• **Current Boosts**: `{guild.premium_subscription_count}` boosts\n"
            f"• **Current Server Level**: `Level {guild.premium_tier}`\n"
            f"• **Highest Level Reached**: `Level {highest_tier}`\n"
            f"• **Active Boosters Count**: `{active_boosters}` members\n"
            f"• **Total Historical Boosters**: `{total_historical_boosters}` members"
        )
        card = CynexCloudInfoContainer(f"Boost Statistics for {guild.name}", desc)
        await interaction.followup.send(view=card.build())

    @boost.command(name="leaderboard", description="Display leaderboard of top boosters sorted by boost duration")
    async def boost_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)

        # Retrieve active boosters with their first boost date
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id, first_boost_date, total_boosts 
                FROM boost_track 
                WHERE guild_id = ? AND is_boosting = 1 
                ORDER BY first_boost_date ASC
            """, (guild_id,)) as c:
                rows = await c.fetchall()

        if not rows:
            card = CynexCloudInfoContainer("Booster Leaderboard", "*There are currently no active boosters logged on this server.*")
            await interaction.followup.send(view=card.build())
            return

        pages = []
        lines = []
        for idx, row in enumerate(rows, 1):
            user_id, date_str, total = row
            member_obj = interaction.guild.get_member(int(user_id))
            user_str = member_obj.mention if member_obj else f"`ID: {user_id}`"
            
            # Duration calculation
            try:
                # SQL dates are usually YYYY-MM-DD HH:MM:SS or ISO format
                # datetime.fromisoformat doesn't always support trailing space and timezone without tzinfo
                clean_date = date_str.split(".")[0]  # strip sub-seconds
                dt = datetime.strptime(clean_date, "%Y-%m-%d %H:%M:%S")
                diff = datetime.now() - dt
                days = diff.days
                duration_str = f"**{days} days**"
            except Exception:
                duration_str = "Unknown duration"

            lines.append(f"**#{idx}** {user_str} — Boosting for {duration_str} (`{total}` times boosted)")
            
            if len(lines) == 10:
                pages.append("\n".join(lines))
                lines = []
        if lines:
            pages.append("\n".join(lines))

        paginator = CynexCloudPaginationContainer("Server Booster Leaderboard", pages, interaction.user.id)
        await interaction.followup.send(view=paginator)

    @boost.command(name="history", description="Check server boost actions history for a specific member")
    async def boost_history_cmd(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT action, timestamp 
                FROM boost_history 
                WHERE guild_id = ? AND user_id = ? 
                ORDER BY timestamp DESC 
                LIMIT 30
            """, (guild_id, user_id)) as c:
                rows = await c.fetchall()

        if not rows:
            card = CynexCloudInfoContainer("Booster History", f"No boost activity logs found for {member.name}.")
            await interaction.followup.send(view=card.build())
            return

        lines = []
        for action, timestamp in rows:
            emoji = "🟢" if action == "started" else "🔴"
            action_text = "Started Boosting" if action == "started" else "Stopped Boosting"
            # Format timestamp nicely
            try:
                clean_time = timestamp.split(".")[0]
                dt = datetime.strptime(clean_time, "%Y-%m-%d %H:%M:%S")
                time_str = f"<t:{int(dt.replace(tzinfo=timezone.utc).timestamp())}:F>"
            except Exception:
                time_str = timestamp
            lines.append(f"{emoji} **{action_text}** — {time_str}")

        desc = "\n".join(lines)
        card = CynexCloudInfoContainer(f"Boost History for {member.name}", desc)
        await interaction.followup.send(view=card.build())

async def setup(bot: commands.Bot):
    await bot.add_cog(BoostTracker(bot))
