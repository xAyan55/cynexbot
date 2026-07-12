import logging
from datetime import datetime, timezone
from typing import Optional

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands

from ui import (
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeInfoContainer,
    BreezePaginationContainer
)

logger = logging.getLogger("Breeze.InviteTracker")
DB_PATH = "breeze.db"

# ══════════════════════════════════════════════════════════════════════
# COG IMPLEMENTATION
# ══════════════════════════════════════════════════════════════════════

class InviteTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invite_cache = {}  # guild_id -> {code -> uses}

    async def cog_load(self):
        # Cache invites on startup
        self.bot.loop.create_task(self.initialize_cache())

    async def initialize_cache(self):
        await self.bot.wait_until_ready()
        logger.info("[InviteTracker] Pre-populating invite caches...")
        for guild in self.bot.guilds:
            await self.update_guild_cache(guild)

    async def update_guild_cache(self, guild: discord.Guild):
        guild_id = str(guild.id)
        self.invite_cache[guild_id] = {}
        
        # Check permissions
        if not guild.me.guild_permissions.manage_guild:
            logger.warning(f"[InviteTracker] Missing Manage Guild permission in {guild.name} ({guild.id}); cannot cache invites.")
            return

        try:
            invites = await guild.invites()
            for inv in invites:
                self.invite_cache[guild_id][inv.code] = inv.uses
            
            # Cache vanity url uses if it exists
            if guild.vanity_url_code:
                try:
                    vanity = await guild.vanity_invite()
                    self.invite_cache[guild_id][guild.vanity_url_code] = vanity.uses
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[InviteTracker] Error updating invite cache for guild {guild.id}: {e}")

    # Listeners for invite creation/deletion to keep cache in sync
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild_id = str(invite.guild.id)
        if guild_id not in self.invite_cache:
            self.invite_cache[guild_id] = {}
        self.invite_cache[guild_id][invite.code] = invite.uses
        logger.debug(f"[InviteTracker] Cached new invite {invite.code} in guild {guild_id}")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild_id = str(invite.guild.id)
        if guild_id in self.invite_cache:
            self.invite_cache[guild_id].pop(invite.code, None)
        logger.debug(f"[InviteTracker] Removed deleted invite {invite.code} from guild {guild_id}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.update_guild_cache(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.invite_cache.pop(str(guild.id), None)

    # Member Join Event Handler
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        guild_id = str(guild.id)
        
        if not guild.me.guild_permissions.manage_guild:
            return

        used_invite = None
        inviter = None
        is_vanity = False

        try:
            new_invites = await guild.invites()
            cached_invites = self.invite_cache.get(guild_id, {})

            # 1. Compare regular invites
            for inv in new_invites:
                cached_uses = cached_invites.get(inv.code)
                if cached_uses is not None and inv.uses > cached_uses:
                    used_invite = inv
                    inviter = inv.inviter
                    break
            
            # 2. Check vanity URL if no invite matched
            if not used_invite and guild.vanity_url_code:
                try:
                    vanity = await guild.vanity_invite()
                    cached_vanity_uses = cached_invites.get(guild.vanity_url_code)
                    if cached_vanity_uses is not None and vanity.uses > cached_vanity_uses:
                        is_vanity = True
                except Exception:
                    pass

            # Update cache with new values
            for inv in new_invites:
                cached_invites[inv.code] = inv.uses
            if guild.vanity_url_code:
                try:
                    vanity = await guild.vanity_invite()
                    cached_invites[guild.vanity_url_code] = vanity.uses
                except Exception:
                    pass
            self.invite_cache[guild_id] = cached_invites

        except Exception as e:
            logger.error(f"[InviteTracker] Error fetching invites on member join in guild {guild_id}: {e}")
            return

        # Attribute the join
        inviter_id = "Unknown"
        invite_code = "Unknown"

        if is_vanity:
            inviter_id = "Vanity URL"
            invite_code = guild.vanity_url_code
        elif used_invite and inviter:
            inviter_id = str(inviter.id)
            invite_code = used_invite.code

        # Log details
        logger.info(f"[InviteTracker] Member Joined: {member.id} | Inviter: {inviter_id} | Code: {invite_code} in Guild: {guild_id}")

        # Update SQL data if inviter is known (and not vanity URL)
        if inviter_id != "Unknown":
            async with aiosqlite.connect(DB_PATH) as db:
                # Store who invited whom
                await db.execute("""
                    INSERT OR REPLACE INTO invited_by (guild_id, invited_user_id, inviter_user_id, invite_code, status)
                    VALUES (?, ?, ?, ?, 'joined')
                """, (guild_id, str(member.id), inviter_id, invite_code))

                # If vanity URL, we don't increment stats for any user
                if inviter_id != "Vanity URL":
                    # Check for fake invite: account age < 24 hours
                    time_diff = datetime.now(timezone.utc) - member.created_at
                    is_fake = time_diff.total_seconds() < 86400
                    
                    if is_fake:
                        await db.execute("""
                            INSERT INTO invite_stats (guild_id, user_id, total, fake)
                            VALUES (?, ?, 1, 1)
                            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                            total = total + 1,
                            fake = fake + 1
                        """, (guild_id, inviter_id))
                    else:
                        await db.execute("""
                            INSERT INTO invite_stats (guild_id, user_id, total, regular)
                            VALUES (?, ?, 1, 1)
                            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                            total = total + 1,
                            regular = regular + 1
                        """, (guild_id, inviter_id))
                await db.commit()

    # Member Leave Event Handler
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild_id = str(member.guild.id)
        
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT inviter_user_id, status FROM invited_by WHERE guild_id = ? AND invited_user_id = ?", (guild_id, str(member.id))) as cursor:
                row = await cursor.fetchone()
            
            if row:
                inviter_id = row[0]
                status = row[1]
                
                # If they were marked as joined, update to left and decrement inviter regular stats
                if status == "joined":
                    await db.execute("UPDATE invited_by SET status = 'left' WHERE guild_id = ? AND invited_user_id = ?", (guild_id, str(member.id)))
                    
                    if inviter_id not in ("Unknown", "Vanity URL"):
                        await db.execute("""
                            UPDATE invite_stats 
                            SET regular = MAX(0, regular - 1), left = left + 1 
                            WHERE guild_id = ? AND user_id = ?
                        """, (guild_id, inviter_id))
                    await db.commit()
                    logger.info(f"[InviteTracker] Member Left: {member.id} | Adjusted inviter: {inviter_id} stats in Guild: {guild_id}")

    # Helper checking permissions before performing actions
    def check_bot_invite_permissions(self, interaction: discord.Interaction) -> Optional[str]:
        guild = interaction.guild
        if not guild:
            return "This command can only be used inside servers."
        if not guild.me.guild_permissions.manage_guild:
            return "❌ Breeze requires **Manage Server** permission to read and track server invites."
        return None

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    invites = app_commands.Group(name="invites", description="Breeze Server Invite tracking commands")

    @invites.command(name="me", description="View your server invite statistics")
    async def invites_me(self, interaction: discord.Interaction):
        await interaction.response.defer()
        err = self.check_bot_invite_permissions(interaction)
        if err:
            card = BreezeErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build())
            return

        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT total, regular, fake, left, bonus FROM invite_stats WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as c:
                row = await c.fetchone()

        if row:
            total, regular, fake, left, bonus = row
        else:
            total, regular, fake, left, bonus = 0, 0, 0, 0, 0

        # Calculate net valid invites: regular + bonus - left
        valid = regular + bonus - left

        desc = (
            f"• **Valid Invites**: `{valid}`\n"
            f"• **Total Attempts**: `{total}`\n"
            f"• **Regular Joins**: `{regular}`\n"
            f"• **Left Users**: `{left}`\n"
            f"• **Fake / New Accounts**: `{fake}`\n"
            f"• **Admin Bonus**: `{bonus}`"
        )
        card = BreezeInfoContainer(f"Invite Stats for {interaction.user.name}", desc)
        await interaction.followup.send(view=card.build())

    @invites.command(name="stats", description="View invite statistics for a specific member")
    async def invites_stats(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        err = self.check_bot_invite_permissions(interaction)
        if err:
            card = BreezeErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build())
            return

        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT total, regular, fake, left, bonus FROM invite_stats WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as c:
                row = await c.fetchone()

        if row:
            total, regular, fake, left, bonus = row
        else:
            total, regular, fake, left, bonus = 0, 0, 0, 0, 0

        valid = regular + bonus - left

        desc = (
            f"• **Valid Invites**: `{valid}`\n"
            f"• **Total Attempts**: `{total}`\n"
            f"• **Regular Joins**: `{regular}`\n"
            f"• **Left Users**: `{left}`\n"
            f"• **Fake / New Accounts**: `{fake}`\n"
            f"• **Admin Bonus**: `{bonus}`"
        )
        card = BreezeInfoContainer(f"Invite Stats for {member.name}", desc)
        await interaction.followup.send(view=card.build())

    @invites.command(name="invited", description="View exactly who joined the server using a member's invites")
    async def invites_invited(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        err = self.check_bot_invite_permissions(interaction)
        if err:
            card = BreezeErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build())
            return

        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT invited_user_id, invite_code, status FROM invited_by WHERE guild_id = ? AND inviter_user_id = ? ORDER BY invited_user_id ASC", (guild_id, user_id)) as c:
                rows = await c.fetchall()

        if not rows:
            card = BreezeInfoContainer(f"Invited Members List", f"No members have joined using invites from {member.name}.")
            await interaction.followup.send(view=card.build())
            return

        # Paginate results
        pages = []
        page_size = 4
        for i in range(0, len(rows), page_size):
            chunk = rows[i:i + page_size]
            page_sections = []
            for invited_user_id, code, status in chunk:
                user_obj = interaction.guild.get_member(int(invited_user_id))
                user_str = user_obj.mention if user_obj else f"User ID: {invited_user_id}"
                status_emoji = "🟢 Active" if status == "joined" else "🔴 Left"
                
                sec_title = f"👤 Invited User: {user_obj.display_name if user_obj else invited_user_id}"
                sec_desc = f"• **User:** {user_str}\n• **Invite Code:** `{code}`\n• **Status:** {status_emoji}"
                page_sections.append((sec_title, sec_desc))
            pages.append({
                "title": f"Invited Members",
                "description": f"Members invited by {member.display_name} | Page {i//page_size + 1} of {(len(rows) - 1)//page_size + 1}",
                "sections": page_sections
            })

        paginator = BreezePaginationContainer(f"Members Invited by {member.name}", pages, interaction.user.id)
        await interaction.followup.send(view=paginator)

    @invites.command(name="leaderboard", description="Display the server's top inviters leaderboard")
    async def invites_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        err = self.check_bot_invite_permissions(interaction)
        if err:
            card = BreezeErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build())
            return

        guild_id = str(interaction.guild_id)

        # Retrieve invite counts sorted by net valid (regular + bonus - left)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id, (regular + bonus - left) as net_invites, total, regular, left, fake 
                FROM invite_stats 
                WHERE guild_id = ? 
                ORDER BY net_invites DESC, total DESC
            """, (guild_id,)) as c:
                rows = await c.fetchall()

        if not rows:
            card = BreezeInfoContainer("Top Inviters Leaderboard", "*No invite statistics found on this server.*")
            await interaction.followup.send(view=card.build())
            return

        pages = []
        page_size = 4
        for i in range(0, len(rows), page_size):
            chunk = rows[i:i + page_size]
            page_sections = []
            for offset, row in enumerate(chunk):
                idx = i + offset + 1
                user_id, net, total, reg, left, fake = row
                member_obj = interaction.guild.get_member(int(user_id))
                user_str = member_obj.mention if member_obj else f"User ID: {user_id}"
                sec_title = f"🏆 Rank #{idx}: {member_obj.display_name if member_obj else user_id}"
                sec_desc = f"• **Member:** {user_str}\n• **Net Invites:** `{net}` valid\n• **Details:** `{reg}` joins, `{left}` left, `{fake}` fake"
                page_sections.append((sec_title, sec_desc))
            pages.append({
                "title": "Top Inviters Leaderboard",
                "description": f"Page {i//page_size + 1} of {(len(rows) - 1)//page_size + 1}",
                "sections": page_sections
            })

        paginator = BreezePaginationContainer("Top Inviters Leaderboard", pages, interaction.user.id)
        await interaction.followup.send(view=paginator)

    @invites.command(name="reset", description="Clear all invite stats for the server")
    @app_commands.checks.has_permissions(administrator=True)
    async def invites_reset(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM invite_stats WHERE guild_id = ?", (guild_id,))
            await db.execute("DELETE FROM invited_by WHERE guild_id = ?", (guild_id,))
            await db.commit()

        card = BreezeSuccessContainer("Statistics Reset", "🗑️ All server invite tracking logs and statistics have been successfully cleared.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @invites.command(name="bonus", description="Grant bonus invites to a specific member")
    @app_commands.checks.has_permissions(administrator=True)
    async def invites_bonus(self, interaction: discord.Interaction, member: discord.Member, amount: int):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        user_id = str(member.id)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO invite_stats (guild_id, user_id, bonus)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                bonus = bonus + ?
            """, (guild_id, user_id, amount, amount))
            await db.commit()

        card = BreezeSuccessContainer("Bonus Invites Updated", f"Granted **{amount}** bonus invites to {member.mention}.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracker(bot))
