import logging
from typing import Optional, Dict, Set

import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands

from ui import (
    BreezeSuccessContainer,
    BreezeErrorContainer,
    BreezeInfoContainer,
    BreezeWarningContainer,
    BreezeContainerBuilder
)

logger = logging.getLogger("Breeze.ReactionRoles")
DB_PATH = "breeze.db"

# ══════════════════════════════════════════════════════════════════════
def build_panel_embed(title: str, description: Optional[str], mappings: dict, guild: discord.Guild, multi_role: bool) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description or "React to get your roles!",
        color=3447003
    )
    
    body = ""
    if mappings:
        lines = []
        for emoji_str, role_id in mappings.items():
            role = guild.get_role(int(role_id))
            role_str = role.mention if role else f"Unknown Role (ID: {role_id})"
            lines.append(f"{emoji_str} → {role_str}")
        body = "\n".join(lines)
    else:
        body = "*No roles mapped yet. Use `/reactionroles add` to add one.*"
        
    embed.add_field(name="Roles List", value=body, inline=False)
    
    mode_str = "Multi-Role Allowed" if multi_role else "Unique / Single-Role Only"
    embed.set_footer(text=f"🎭 React below to get your role | Mode: {mode_str}")
    return embed


# ══════════════════════════════════════════════════════════════════════
# COG IMPLEMENTATION
# ══════════════════════════════════════════════════════════════════════

class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # In-memory caches for rapid checks
        self.panels_cache = {}    # message_id (str) -> dict of panel details
        self.mappings_cache = {}  # message_id (str) -> {emoji_str -> role_id_str}

    async def cog_load(self):
        self.bot.loop.create_task(self.initialize_cache())

    async def initialize_cache(self):
        await self.bot.wait_until_ready()
        logger.info("[ReactionRoles] Initializing reaction roles cache from database...")
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # 1. Load panels
                async with db.execute("SELECT message_id, guild_id, channel_id, title, description, multi_role FROM reaction_role_panels") as cursor:
                    rows = await cursor.fetchall()
                    for r in rows:
                        self.panels_cache[r[0]] = {
                            "guild_id": r[1],
                            "channel_id": r[2],
                            "title": r[3],
                            "description": r[4],
                            "multi_role": bool(r[5])
                        }
                
                # 2. Load mappings
                async with db.execute("SELECT message_id, emoji, role_id FROM reaction_roles") as cursor:
                    rows = await cursor.fetchall()
                    for r in rows:
                        msg_id = r[0]
                        if msg_id not in self.mappings_cache:
                            self.mappings_cache[msg_id] = {}
                        self.mappings_cache[msg_id][r[1]] = r[2]
            
            logger.info(f"[ReactionRoles] Loaded {len(self.panels_cache)} panels and mappings into cache.")
            
            # Start background auto-sync tasks
            self.bot.loop.create_task(self.auto_sync_reactions())
            
        except Exception as e:
            logger.error(f"[ReactionRoles] Error loading cache: {e}")

    async def auto_sync_reactions(self):
        await self.bot.wait_until_ready()
        logger.info("[ReactionRoles] Auto-syncing panel reactions...")
        for msg_id, panel in list(self.panels_cache.items()):
            guild = self.bot.get_guild(int(panel["guild_id"]))
            if not guild:
                continue
            
            ch = guild.get_channel(int(panel["channel_id"]))
            if not ch:
                continue
            
            try:
                message = await ch.fetch_message(int(msg_id))
                mappings = self.mappings_cache.get(msg_id, {})
                
                # Verify that each mapped emoji is reacted to by the bot
                existing_reactions = {str(r.emoji) for r in message.reactions if r.me}
                for emoji_str in mappings.keys():
                    if emoji_str not in existing_reactions:
                        try:
                            # Parse emoji structure
                            parsed_emoji = discord.PartialEmoji.from_str(emoji_str)
                            await message.add_reaction(parsed_emoji)
                            logger.info(f"[ReactionRoles] Auto-synced reaction {emoji_str} to message {msg_id}")
                        except Exception as react_err:
                            logger.warning(f"[ReactionRoles] Failed to add reaction {emoji_str} during sync: {react_err}")
            except discord.NotFound:
                logger.warning(f"[ReactionRoles] Panel message {msg_id} not found in channel {ch.id}. Cleaning up database.")
                await self.delete_panel_data(msg_id)
            except discord.Forbidden:
                logger.warning(f"[ReactionRoles] Forbidden: Cannot fetch/react to message {msg_id} in channel {ch.id}")
            except Exception as e:
                logger.error(f"[ReactionRoles] Error syncing message {msg_id}: {e}")

    async def delete_panel_data(self, message_id: str):
        self.panels_cache.pop(message_id, None)
        self.mappings_cache.pop(message_id, None)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM reaction_role_panels WHERE message_id = ?", (message_id,))
            await db.execute("DELETE FROM reaction_roles WHERE message_id = ?", (message_id,))
            await db.commit()

    # Helper checking permissions before performing actions
    def check_permissions(self, interaction: discord.Interaction, channel: discord.TextChannel) -> Optional[str]:
        guild = interaction.guild
        # Bot system permissions check
        me_perms = channel.permissions_for(guild.me)
        if not guild.me.guild_permissions.manage_roles:
            return "❌ Bot lacks global **Manage Roles** permission."
        if not me_perms.send_messages or not me_perms.embed_links:
            return f"❌ Bot lacks permission to **Send Messages** / **Embed Links** in channel {channel.mention}."
        if not me_perms.add_reactions:
            return f"❌ Bot lacks permission to **Add Reactions** in channel {channel.mention}."
        if not me_perms.read_message_history:
            return f"❌ Bot lacks permission to **Read Message History** in channel {channel.mention}."
        if not me_perms.manage_messages:
            return f"❌ Bot lacks permission to **Manage Messages** in channel {channel.mention} (required for unique role mode and reaction removals)."
        return None

    # ══════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════

    reactionroles = app_commands.Group(name="reactionroles", description="Breeze emoji-based reaction role panels settings", default_permissions=discord.Permissions(administrator=True))

    @reactionroles.command(name="create", description="Create a new reaction roles panel message")
    async def reaction_roles_create(self, interaction: discord.Interaction, channel: discord.TextChannel, title: str, description: Optional[str] = None, multi_role: bool = True):
        await interaction.response.defer(ephemeral=True)
        
        # Pre-check permissions
        err = self.check_permissions(interaction, channel)
        if err:
            card = BreezeErrorContainer("Missing Bot Permissions", err)
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        # Send empty panel message
        embed = build_panel_embed(title, description, {}, interaction.guild, multi_role)
        try:
            panel_msg = await channel.send(embed=embed)
        except Exception as e:
            card = BreezeErrorContainer("Send Message Failed", f"Failed to send panel embed: {e}")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        msg_id = str(panel_msg.id)
        guild_id = str(interaction.guild_id)

        # Write to SQLite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO reaction_role_panels (guild_id, message_id, channel_id, title, description, multi_role)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (guild_id, msg_id, str(channel.id), title, description, 1 if multi_role else 0))
            await db.commit()

        # Update cache
        self.panels_cache[msg_id] = {
            "guild_id": guild_id,
            "channel_id": str(channel.id),
            "title": title,
            "description": description,
            "multi_role": multi_role
        }
        self.mappings_cache[msg_id] = {}

        card = BreezeSuccessContainer(
            "Panel Created Successfully",
            f"Panel sent to {channel.mention}.\n"
            f"**Message ID:** `{msg_id}`\n\n"
            f"Use `/reactionroles add {msg_id} [emoji] [role]` to link roles."
        )
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @reactionroles.command(name="add", description="Add an emoji reaction mapping to an existing role panel")
    async def reaction_roles_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        guild_id = str(guild.id)
        
        # 1. Verify that message_id is a registered panel
        if message_id not in self.panels_cache:
            card = BreezeErrorContainer("Panel Not Found", f"No registered reaction role panel exists with Message ID `{message_id}`.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        panel = self.panels_cache[message_id]
        
        # 2. Check Role Hierarchy
        if role >= guild.me.top_role:
            card = BreezeErrorContainer("Hierarchy Alert", f"❌ Cannot assign {role.mention} because it is equal or higher than Breeze's top role.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return
        if role >= interaction.user.top_role and not interaction.guild.owner == interaction.user:
            card = BreezeErrorContainer("Hierarchy Alert", f"❌ Cannot assign {role.mention} because it is equal or higher than your highest role.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        # 3. Parse and Validate Emoji
        try:
            parsed_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception:
            card = BreezeErrorContainer("Invalid Emoji", f"Could not parse `{emoji}`. Make sure it is a valid unicode emoji or custom server emoji.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        if parsed_emoji.is_custom_emoji():
            # Check if bot can access custom emoji
            custom_emoji = self.bot.get_emoji(parsed_emoji.id)
            if not custom_emoji:
                card = BreezeErrorContainer("Inaccessible Emoji", f"❌ Custom emoji `{emoji}` is not accessible to Breeze. Emojis must belong to a server the bot is present in.")
                await interaction.followup.send(view=card.build(), ephemeral=True)
                return

        # Fetch message and test if bot can react to validate standard unicode emoji
        ch = guild.get_channel(int(panel["channel_id"]))
        if not ch:
            card = BreezeErrorContainer("Channel Not Found", "The channel containing this panel no longer exists.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        try:
            message = await ch.fetch_message(int(message_id))
        except discord.NotFound:
            card = BreezeErrorContainer("Message Not Found", "The panel message was not found or has been deleted.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return
        except discord.Forbidden:
            card = BreezeErrorContainer("Forbidden", "Breeze does not have access permissions to view the target channel.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        # Attempt to react to validate standard emoji
        emoji_key = str(parsed_emoji)
        try:
            await message.add_reaction(parsed_emoji)
        except discord.HTTPException as e:
            card = BreezeErrorContainer("Validation Failed", f"❌ Discord rejected reaction using `{emoji}`. Check if it is a valid standard emoji.\nError: `{e}`")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        # 4. Atomic Write to DB and Cache
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO reaction_roles (guild_id, message_id, emoji, role_id)
                VALUES (?, ?, ?, ?)
            """, (guild_id, message_id, emoji_key, str(role.id)))
            await db.commit()

        # Update cache
        if message_id not in self.mappings_cache:
            self.mappings_cache[message_id] = {}
        self.mappings_cache[message_id][emoji_key] = str(role.id)

        # 5. Rebuild embed and edit panel message
        updated_embed = build_panel_embed(
            panel["title"],
            panel["description"],
            self.mappings_cache[message_id],
            guild,
            panel["multi_role"]
        )
        await message.edit(embed=updated_embed, view=None)

        card = BreezeSuccessContainer("Mapping Registered", f"Reacting with {emoji_key} will now grant the role {role.mention}.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @reactionroles.command(name="remove", description="Remove an emoji reaction mapping from a role panel")
    async def reaction_roles_remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        guild_id = str(guild.id)
        
        if message_id not in self.panels_cache:
            card = BreezeErrorContainer("Panel Not Found", f"No panel registered with Message ID `{message_id}`.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        panel = self.panels_cache[message_id]
        
        try:
            parsed_emoji = discord.PartialEmoji.from_str(emoji)
        except Exception:
            card = BreezeErrorContainer("Invalid Emoji", "Could not parse emoji string.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        emoji_key = str(parsed_emoji)
        mappings = self.mappings_cache.get(message_id, {})
        if emoji_key not in mappings:
            card = BreezeErrorContainer("Mapping Not Found", f"No role mapped to `{emoji_key}` on this panel.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        # 1. Update Database
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?", (guild_id, message_id, emoji_key))
            await db.commit()

        # 2. Update Cache
        mappings.pop(emoji_key, None)
        self.mappings_cache[message_id] = mappings

        # Fetch message to update embed and clear reactions
        ch = guild.get_channel(int(panel["channel_id"]))
        if ch:
            try:
                message = await ch.fetch_message(int(message_id))
                
                # Rebuild and edit panel message
                updated_embed = build_panel_embed(
                    panel["title"],
                    panel["description"],
                    mappings,
                    guild,
                    panel["multi_role"]
                )
                await message.edit(embed=updated_embed, view=None)

                # Clear reactions (removes bot and user reactions of this emoji)
                if ch.permissions_for(guild.me).manage_messages:
                    try:
                        await message.clear_reaction(parsed_emoji)
                    except Exception:
                        pass
                else:
                    # Fallback: remove only bot's reaction
                    try:
                        await message.remove_reaction(parsed_emoji, guild.me)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[ReactionRoles] Failed to clean up reaction on message {message_id}: {e}")

        card = BreezeSuccessContainer("Mapping Removed", f"Successfully cleared role mapping for {emoji_key}.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    @reactionroles.command(name="list", description="Show all configured emoji mappings for a role panel")
    async def reaction_roles_list(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        
        if message_id not in self.panels_cache:
            card = BreezeErrorContainer("Panel Not Found", f"No panel registered with Message ID `{message_id}`.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        panel = self.panels_cache[message_id]
        mappings = self.mappings_cache.get(message_id, {})

        builder = BreezeContainerBuilder(f"Reaction Roles List: {panel['title']}", "Visual settings configuration overview.")
        
        info_text = (
            f"• **Panel Message ID:** `{message_id}`\n"
            f"• **Target Channel:** <#{panel['channel_id']}>\n"
            f"• **Settings Mode:** `{'Multi-Role Allowed' if panel['multi_role'] else 'Unique Mode (Single Role)'}`"
        )
        builder.add_section("⚙️ Panel Details", info_text)
        builder.add_separator()
        
        mapping_lines = []
        if mappings:
            for emoji_str, role_id in mappings.items():
                role = guild.get_role(int(role_id))
                role_str = role.mention if role else f"Unknown Role (ID: {role_id})"
                mapping_lines.append(f"• {emoji_str} → {role_str}")
            mappings_text = "\n".join(mapping_lines)
        else:
            mappings_text = "*No mappings configured yet. Use `/reactionroles add` to associate.*"
            
        builder.add_section("🎭 Active Mappings", mappings_text)
        await interaction.followup.send(view=builder.build(), ephemeral=True)

    @reactionroles.command(name="delete", description="Delete a reaction role panel and clean up databases")
    async def reaction_roles_delete(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        
        if message_id not in self.panels_cache:
            card = BreezeErrorContainer("Panel Not Found", "No panel registered with that Message ID.")
            await interaction.followup.send(view=card.build(), ephemeral=True)
            return

        panel = self.panels_cache[message_id]

        # Try to delete the message in channel
        ch = guild.get_channel(int(panel["channel_id"]))
        if ch:
            try:
                msg = await ch.fetch_message(int(message_id))
                await msg.delete()
            except Exception:
                pass

        # Atomic database cleanup
        await self.delete_panel_data(message_id)

        card = BreezeSuccessContainer("Panel Deleted", "🗑️ Successfully deleted panel message, cleared mappings cache, and removed SQL records.")
        await interaction.followup.send(view=card.build(), ephemeral=True)

    # ══════════════════════════════════════════════════════════════════════
    # EVENT LISTENERS (RAW REACTION ADD/REMOVE)
    # ══════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Ignore bot reactions
        if payload.user_id == self.bot.user.id:
            return

        msg_id = str(payload.message_id)
        
        # Performance: in-memory dict lookup first
        if msg_id not in self.panels_cache:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = payload.member
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return

        # Ignore other bot members
        if member.bot:
            return

        panel = self.panels_cache[msg_id]
        mappings = self.mappings_cache.get(msg_id, {})
        emoji_str = str(payload.emoji)

        if emoji_str in mappings:
            role_id = mappings[emoji_str]
            role = guild.get_role(int(role_id))
            if not role:
                return

            # Check bot permissions and hierarchy before assigning
            if not guild.me.guild_permissions.manage_roles:
                logger.warning(f"[ReactionRoles] Missing Manage Roles permission in guild {guild.id}")
                return
            if role >= guild.me.top_role:
                logger.warning(f"[ReactionRoles] Role {role.id} is higher than bot's top role in guild {guild.id}")
                return
            if member.top_role >= guild.me.top_role and guild.owner_id != member.id:
                # Bot cannot modify members equal or higher in hierarchy
                logger.debug(f"[ReactionRoles] Member {member.id} has higher role than bot. Skipping.")
                return

            # Prevent duplicate role assignment
            if role in member.roles:
                return

            # Unique mode check: remove other role mappings first
            if not panel["multi_role"]:
                other_roles_to_remove = []
                for other_emoji, other_role_id in mappings.items():
                    if other_emoji != emoji_str:
                        orole = guild.get_role(int(other_role_id))
                        if orole and orole in member.roles:
                            other_roles_to_remove.append(orole)
                
                if other_roles_to_remove:
                    try:
                        await member.remove_roles(*other_roles_to_remove, reason="Breeze Reaction Role Unique Mode Toggle")
                    except Exception as e:
                        logger.error(f"[ReactionRoles] Failed to remove unique roles: {e}")

                # Clean up user's other reactions on the message (requires manage_messages)
                ch = guild.get_channel(int(panel["channel_id"]))
                if ch and ch.permissions_for(guild.me).manage_messages:
                    try:
                        message = await ch.fetch_message(payload.message_id)
                        for other_emoji in mappings.keys():
                            if other_emoji != emoji_str:
                                p_emoji = discord.PartialEmoji.from_str(other_emoji)
                                await message.remove_reaction(p_emoji, member)
                    except Exception:
                        pass

            # Assign Role
            try:
                await member.add_roles(role, reason="Breeze Reaction Role Assignment")
                logger.info(f"[ReactionRoles] Assigned role {role.id} to user {member.id} in guild {guild.id}")
            except Exception as e:
                logger.error(f"[ReactionRoles] Failed to assign role {role.id} to {member.id}: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        # Ignore bot actions
        if payload.user_id == self.bot.user.id:
            return

        msg_id = str(payload.message_id)
        if msg_id not in self.panels_cache:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Fetch member since RawReactionActionEvent remove does not contain member object
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.NotFound:
            return
        except Exception:
            return

        if member.bot:
            return

        panel = self.panels_cache[msg_id]
        mappings = self.mappings_cache.get(msg_id, {})
        emoji_str = str(payload.emoji)

        if emoji_str in mappings:
            role_id = mappings[emoji_str]
            role = guild.get_role(int(role_id))
            if not role:
                return

            # Check bot permissions and hierarchy before removing
            if not guild.me.guild_permissions.manage_roles:
                return
            if role >= guild.me.top_role:
                return
            if member.top_role >= guild.me.top_role and guild.owner_id != member.id:
                return

            # Verify member has the role before trying to remove
            if role not in member.roles:
                return

            # Remove Role
            try:
                await member.remove_roles(role, reason="Breeze Reaction Role Removal")
                logger.info(f"[ReactionRoles] Removed role {role.id} from user {member.id} in guild {guild.id}")
            except Exception as e:
                logger.error(f"[ReactionRoles] Failed to remove role {role.id} from {member.id}: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
