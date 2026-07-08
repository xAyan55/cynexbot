import asyncio
import aiosqlite
import json
import logging
import io
import copy
from datetime import datetime
from typing import Optional, List, Dict, Any, Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import (
    LayoutView,
    Container,
    TextDisplay,
    Separator,
    Section,
    MediaGallery,
    ActionRow,
    File,
    Thumbnail,
    Button
)
from discord import MediaGalleryItem, SeparatorSpacing

logger = logging.getLogger("CynexCloud.Tickets")
DB_PATH = "cynex.db"

# ══════════════════════════════════════════════════════════════════════
# DATABASE SCHEMAS & INITIALIZATION
# ══════════════════════════════════════════════════════════════════════

async def init_ticket_db():
    """Initializes SQLite tables in cynex.db if they do not exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        # Support tickets table (with claimed and sequential number indexing)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT UNIQUE NOT NULL,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            )
        """)
        # Ticket message history for transcript generation
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_avatar TEXT,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                attachments TEXT DEFAULT '[]',
                embeds TEXT DEFAULT '[]'
            )
        """)
        # Published ticket panels mapping
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                panel_name TEXT NOT NULL,
                panel_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Ticket configs for categories, limits, support roles
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_configs (
                guild_id TEXT PRIMARY KEY,
                admin_roles TEXT DEFAULT '[]',
                support_roles TEXT DEFAULT '[]',
                log_channel_id TEXT,
                categories TEXT DEFAULT '[]',
                ticket_counter INTEGER DEFAULT 0,
                max_tickets_per_user INTEGER DEFAULT 3
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (user_id)")
        await db.commit()
    logger.info("Ticket database tables initialized successfully.")

# ══════════════════════════════════════════════════════════════════════
# TICKET DATABASE HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

async def get_ticket_config(guild_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT admin_roles, support_roles, log_channel_id, categories, ticket_counter, max_tickets_per_user FROM ticket_configs WHERE guild_id = ?",
            (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "admin_roles": json.loads(row[0]),
                    "support_roles": json.loads(row[1]),
                    "log_channel_id": row[2],
                    "categories": json.loads(row[3]),
                    "ticket_counter": row[4],
                    "max_tickets_per_user": row[5]
                }
    return {
        "admin_roles": [],
        "support_roles": [],
        "log_channel_id": None,
        "categories": [],
        "ticket_counter": 0,
        "max_tickets_per_user": 3
    }

async def save_ticket_config(guild_id: str, config: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO ticket_configs (guild_id, admin_roles, support_roles, log_channel_id, categories, ticket_counter, max_tickets_per_user)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            guild_id,
            json.dumps(config.get("admin_roles", [])),
            json.dumps(config.get("support_roles", [])),
            config.get("log_channel_id"),
            json.dumps(config.get("categories", [])),
            config.get("ticket_counter", 0),
            config.get("max_tickets_per_user", 3)
        ))
        await db.commit()

async def get_ticket_by_channel(channel_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT ticket_id, guild_id, channel_id, user_id, category, subject, status, claimed_by, created_at, closed_at 
            FROM tickets WHERE channel_id = ?
        """, (channel_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "ticket_id": row[0],
                    "guild_id": row[1],
                    "channel_id": row[2],
                    "user_id": row[3],
                    "category": row[4],
                    "subject": row[5],
                    "status": row[6],
                    "claimed_by": row[7],
                    "created_at": row[8],
                    "closed_at": row[9]
                }
    return None

async def save_ticket(ticket_id: str, guild_id: str, channel_id: str, user_id: str, category: str, subject: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tickets (ticket_id, guild_id, channel_id, user_id, category, subject, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
        """, (ticket_id, guild_id, channel_id, user_id, category, subject))
        await db.commit()

async def update_ticket_status(channel_id: str, status: str):
    closed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if status == 'closed' else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE tickets SET status = ?, closed_at = ? WHERE channel_id = ?
        """, (status, closed_at, channel_id))
        await db.commit()

async def update_ticket_claim(channel_id: str, claimed_by: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE tickets SET claimed_by = ? WHERE channel_id = ?
        """, (claimed_by, channel_id))
        await db.commit()

async def get_user_open_tickets(user_id: str, guild_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM tickets WHERE user_id = ? AND guild_id = ? AND status = 'open'", (user_id, guild_id)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def check_duplicate_ticket(user_id: str, guild_id: str, category: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM tickets WHERE user_id = ? AND guild_id = ? AND category = ? AND status = 'open'", (user_id, guild_id, category)) as cursor:
            row = await cursor.fetchone()
            return bool(row)

async def get_ticket_panel(message_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT panel_name, panel_data FROM ticket_panels WHERE message_id = ?", (message_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "panel_name": row[0],
                    "panel_data": json.loads(row[1])
                }
    return None

async def save_ticket_panel(guild_id: str, channel_id: str, message_id: str, name: str, data: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO ticket_panels (guild_id, channel_id, message_id, panel_name, panel_data)
            VALUES (?, ?, ?, ?, ?)
        """, (guild_id, channel_id, message_id, name, json.dumps(data)))
        await db.commit()

# ══════════════════════════════════════════════════════════════════════
# COMPONENT V2 LAYOUT VALIDATION & UTILITIES
# ══════════════════════════════════════════════════════════════════════

def parse_color(hex_str: str) -> Optional[int]:
    """Helper to convert a hex color string to integer."""
    if not hex_str:
        return None
    hex_str = hex_str.strip().lstrip('#')
    try:
        return int(hex_str, 16)
    except ValueError:
        return None

def print_v2_component_tree(layout: discord.ui.LayoutView) -> str:
    """Generates a textual representation of the V2 layout tree."""
    lines = []
    
    def get_name(item):
        return item.__class__.__name__
        
    def traverse(item, indent="", prefix=""):
        name = get_name(item)
        lines.append(f"{indent}{prefix}{name}")
        
        sub_items = []
        accessory_str = ""
        
        if hasattr(item, 'children') and item.children:
            sub_items = item.children
        elif hasattr(item, 'items') and item.items:
            sub_items = item.items
            
        if name == "Section" and hasattr(item, 'accessory') and item.accessory:
            acc_name = get_name(item.accessory)
            accessory_str = f"Accessory: {acc_name}"
            
        new_indent = indent + ("|  " if prefix == "+- " else "   ")
        
        if name == "Section":
            lines.append(f"{new_indent}+- Child Count: {len(sub_items)}")
            if accessory_str:
                lines.append(f"{new_indent}+- {accessory_str}")
        else:
            for i, child in enumerate(sub_items):
                traverse(child, new_indent, "+- ")
                
    for child in layout.children:
        traverse(child, "", "+- ")
        
    return "\n".join(lines)

def validate_v2_layout(layout: discord.ui.LayoutView):
    """Traverses the component tree and checks that it complies with all Discord Components V2 validation rules."""
    if not layout.children:
        raise ValueError("Invalid LayoutView: Must contain at least one top-level component (Container/Section/etc.).")
    if len(layout.children) > 5:
        raise ValueError(f"Invalid LayoutView: Contains {len(layout.children)} top-level components, exceeding the Discord limit of 5.")

    def validate_item(item, path=""):
        name = item.__class__.__name__
        current_path = f"{path} -> {name}" if path else name

        if name == "Container":
            if not item.children:
                raise ValueError(f"Invalid Container ({current_path}): No empty component collections allowed. Must contain at least 1 component.")
            if len(item.children) > 5:
                raise ValueError(f"Invalid Container ({current_path}): Contains {len(item.children)} child components, exceeding the Discord limit of 5.")
            for child in item.children:
                validate_item(child, current_path)

        elif name == "Section":
            if not item.children:
                raise ValueError(f"Invalid Section ({current_path}): Expected 1-3 children, found 0.")
            if len(item.children) > 3:
                raise ValueError(f"Invalid Section ({current_path}): Expected 1-3 children, found {len(item.children)}.")
            
            for child in item.children:
                if child.__class__.__name__ != "TextDisplay":
                    raise ValueError(f"Invalid Section ({current_path}): Child {child.__class__.__name__} is not a TextDisplay.")
                validate_item(child, current_path)

            if hasattr(item, 'accessory') and item.accessory:
                acc_name = item.accessory.__class__.__name__
                if acc_name not in ("Button", "Thumbnail"):
                    raise ValueError(f"Invalid Section ({current_path}): Accessory of type {acc_name} is invalid. Must be Button or Thumbnail.")

        elif name == "ActionRow":
            if not item.children:
                raise ValueError(f"Invalid ActionRow ({current_path}): Expected 1-5 buttons/selects, found 0.")
            if len(item.children) > 5:
                raise ValueError(f"Invalid ActionRow ({current_path}): Expected 1-5 buttons/selects, found {len(item.children)}.")
            for child in item.children:
                c_name = child.__class__.__name__
                if c_name not in ("Button", "Select", "UserSelect", "RoleSelect", "MentionableSelect", "ChannelSelect"):
                    raise ValueError(f"Invalid ActionRow ({current_path}): Child {c_name} is not a valid button/select.")

        elif name == "MediaGallery":
            if not hasattr(item, 'items') or not item.items:
                raise ValueError(f"Invalid MediaGallery ({current_path}): Expected 1-10 items, found 0.")
            if len(item.items) > 10:
                raise ValueError(f"Invalid MediaGallery ({current_path}): Expected 1-10 items, found {len(item.items)}.")

        elif name == "TextDisplay":
            if not hasattr(item, 'content') or not item.content:
                raise ValueError(f"Invalid TextDisplay ({current_path}): Content cannot be empty.")
            content_stripped = item.content.strip()
            if not content_stripped and '\u200b' not in item.content and '\u2800' not in item.content:
                raise ValueError(f"Invalid TextDisplay ({current_path}): Content cannot be empty or only whitespace.")

    for child in layout.children:
        validate_item(child)

# ══════════════════════════════════════════════════════════════════════
# TICKET HELPER VALIDATIONS & PERMISSIONS
# ══════════════════════════════════════════════════════════════════════

async def check_support_permission(user: discord.Member, guild: discord.Guild) -> bool:
    """Checks if the user has Support or Admin Roles."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
        
    config = await get_ticket_config(str(guild.id))
    admin_roles = config.get('admin_roles', [])
    support_roles = config.get('support_roles', [])
    
    for role in user.roles:
        if str(role.id) in admin_roles or str(role.id) in support_roles:
            return True
    return False

# ══════════════════════════════════════════════════════════════════════
# TRANSCRIPT GENERATION & LOGGING HELPERS
# ══════════════════════════════════════════════════════════════════════

def generate_html_transcript(ticket_id: str, messages: list) -> str:
    """Produces a beautiful dark-themed HTML layout of all messages."""
    html_lines = [
        "<!DOCTYPE html>",
        "<html>",
        "<head>",
        "    <meta charset='utf-8'>",
        "    <title>Transcript for Ticket " + ticket_id + "</title>",
        "    <style>",
        "        body { background-color: #2f3136; color: #dcddde; font-family: 'Inter', Arial, sans-serif; padding: 20px; margin: 0; }",
        "        .header { border-bottom: 1px solid #4f545c; padding-bottom: 20px; margin-bottom: 20px; }",
        "        .header h1 { color: #fff; margin: 0; font-size: 24px; }",
        "        .header p { color: #72767d; margin: 5px 0 0 0; font-size: 14px; }",
        "        .message { display: flex; margin-bottom: 20px; border-bottom: 1px solid #36393f; padding-bottom: 10px; }",
        "        .avatar { width: 40px; height: 40px; border-radius: 50%; margin-right: 15px; background-color: #4f545c; }",
        "        .msg-body { display: flex; flex-direction: column; width: 100%; }",
        "        .author { display: flex; align-items: baseline; margin-bottom: 5px; }",
        "        .author-name { color: #fff; font-weight: 600; margin-right: 10px; font-size: 15px; }",
        "        .timestamp { color: #72767d; font-size: 12px; }",
        "        .content { font-size: 15px; line-height: 1.4; word-break: break-word; }",
        "        .attachment { margin-top: 10px; padding: 10px; background-color: #202225; border: 1px solid #4f545c; border-radius: 4px; display: inline-block; }",
        "        .attachment a { color: #00b0f4; text-decoration: none; font-weight: 500; }",
        "    </style>",
        "</head>",
        "<body>",
        "    <div class='header'>",
        "        <h1>Transcript: Ticket " + ticket_id + "</h1>",
        "        <p>Generated on " + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " • Total Messages: " + str(len(messages)) + "</p>",
        "    </div>"
    ]
    
    for msg in messages:
        avatar_url = msg.get('author_avatar') or 'https://cdn.discordapp.com/embed/avatars/0.png'
        
        attachments_html = ""
        if msg.get('attachments'):
            try:
                urls = json.loads(msg['attachments'])
                for url in urls:
                    filename = url.split('/')[-1].split('?')[0]
                    attachments_html += f"<div class='attachment'>📁 File: <a href='{url}' target='_blank'>{filename}</a></div>"
            except Exception:
                pass
                
        html_lines.append(f"""
        <div class='message'>
            <img class='avatar' src='{avatar_url}' alt='avatar'>
            <div class='msg-body'>
                <div class='author'>
                    <span class='author-name'>{msg['author_name']}</span>
                    <span class='timestamp'>{msg['created_at']}</span>
                </div>
                <div class='content'>{msg['content']}</div>
                {attachments_html}
            </div>
        </div>
        """)
        
    html_lines.append("</body></html>")
    return "\n".join(html_lines)

async def log_ticket_action(guild: discord.Guild, action_title: str, description: str, file: Optional[discord.File] = None):
    """Sends log container components messages to the configured log channel (V2 layout)."""
    config = await get_ticket_config(str(guild.id))
    log_chan_id = config.get('log_channel_id')
    if not log_chan_id:
        return
        
    channel = guild.get_channel(int(log_chan_id))
    if not channel:
        return
        
    root_container = Container(accent_color=3447003)
    root_container.add_item(TextDisplay(
        f"📋 **Ticket Action Log: {action_title}**\n"
        f"{description}\n"
        f"Timestamp: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    ))
    
    layout_view = LayoutView()
    layout_view.add_item(root_container)
    
    try:
        if file:
            await channel.send(view=layout_view, file=file)
        else:
            await channel.send(view=layout_view)
    except Exception:
        pass

async def log_ticket_message(message: discord.Message):
    """Transcript logging for messages sent in support ticket channels."""
    if message.author.bot:
        return
        
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ticket_id FROM tickets WHERE channel_id = ?", (str(message.channel.id),)) as cursor:
            row = await cursor.fetchone()
            if row:
                ticket_id = row[0]
                attachments_list = [att.url for att in message.attachments]
                attachments_json = json.dumps(attachments_list)
                
                await conn.execute("""
                    INSERT INTO ticket_messages (ticket_id, author_name, author_avatar, author_id, content, created_at, attachments, embeds)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, '[]')
                """, (
                    ticket_id,
                    str(message.author),
                    str(message.author.display_avatar.url) if message.author.avatar else None,
                    str(message.author.id),
                    message.content,
                    attachments_json
                ))
                await conn.commit()

# ══════════════════════════════════════════════════════════════════════
# TICKET SYSTEM SUPPORT: PERSISTENT VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════════

class GlobalTicketPanelView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception(f"Error in view {self.__class__.__name__} for item {item.custom_id or item.label if hasattr(item, 'custom_id') else 'unknown'}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:create_ticket", emoji="🎫")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            panel = await get_ticket_panel(str(interaction.message.id))
            guild_id = str(interaction.guild_id)
            
            if panel:
                categories = panel['panel_data'].get('categories', [])
            else:
                config = await get_ticket_config(guild_id)
                categories = config.get('categories', [])
                
            if not categories:
                categories = [{"name": "Support", "category_channel_id": None}]
                
            user_id = str(interaction.user.id)
            config = await get_ticket_config(guild_id)
            open_count = await get_user_open_tickets(user_id, guild_id)
            if open_count >= config.get('max_tickets_per_user', 3):
                await interaction.response.send_message("❌ You have reached the open tickets limit in this server.", ephemeral=True)
                return
                
            if len(categories) > 1:
                view = CategorySelectionView(categories, panel['panel_data'] if panel else {})
                await interaction.response.send_message("⚙ Please select a support category below:", view=view, ephemeral=True)
            else:
                cat_name = categories[0]['name']
                is_dup = await check_duplicate_ticket(user_id, guild_id, cat_name)
                if is_dup:
                    await interaction.response.send_message(f"❌ You already have an open ticket in the `{cat_name}` category.", ephemeral=True)
                    return
                await interaction.response.send_modal(OpenTicketModal(cat_name, panel['panel_data'] if panel else {}))
        except Exception as e:
            logger.exception("Error in create_ticket button:")
            msg = f"❌ Error initiating ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

class CategorySelectionView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception(f"Error in view {self.__class__.__name__} for item {item.custom_id or item.label if hasattr(item, 'custom_id') else 'unknown'}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    def __init__(self, categories: List[dict], panel_data: dict):
        super().__init__(timeout=60)
        self.add_item(CategoryDropdown(categories, panel_data))

class CategoryDropdown(discord.ui.Select):
    def __init__(self, categories: List[dict], panel_data: dict):
        self.panel_data = panel_data
        options = []
        for cat in categories:
            options.append(discord.SelectOption(
                label=cat['name'],
                value=cat['name'],
                description=f"Request assistance for {cat['name']}"
            ))
        super().__init__(placeholder="Choose ticket category...", options=options)
        
    async def callback(self, interaction: discord.Interaction):
        try:
            user_id = str(interaction.user.id)
            guild_id = str(interaction.guild_id)
            cat_name = self.values[0]
            
            is_dup = await check_duplicate_ticket(user_id, guild_id, cat_name)
            if is_dup:
                await interaction.response.send_message(f"❌ You already have an open ticket in the `{cat_name}` category.", ephemeral=True)
                return
                
            await interaction.response.send_modal(OpenTicketModal(cat_name, self.panel_data))
        except Exception as e:
            logger.exception("Error in CategoryDropdown.callback:")
            msg = f"❌ Error selecting category: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

class OpenTicketModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(f"Error in modal {self.__class__.__name__}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    subject = discord.ui.TextInput(label="Subject", placeholder="Brief summary of the issue...", required=True)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="Detailed description of your issue...", required=False)
    
    def __init__(self, category: str, panel_data: dict):
        super().__init__(title=f"Open {category} Ticket")
        self.category = category
        self.panel_data = panel_data
        
    async def on_submit(self, interaction: discord.Interaction):
        # 1. Defer immediately to avoid timeout
        await interaction.response.defer(ephemeral=True)
        
        try:
            guild = interaction.guild
            user = interaction.user
            guild_id = str(guild.id)
            user_id = str(user.id)
            
            # Anti-duplicate & limit double-checks
            config = await get_ticket_config(guild_id)
            open_count = await get_user_open_tickets(user_id, guild_id)
            if open_count >= config.get('max_tickets_per_user', 3):
                await interaction.followup.send("❌ Limit of open tickets exceeded.", ephemeral=True)
                return
            if await check_duplicate_ticket(user_id, guild_id, self.category):
                await interaction.followup.send(f"❌ You already have a ticket in the `{self.category}` category.", ephemeral=True)
                return
            
            # Increment Counter
            counter = config.get('ticket_counter', 0) + 1
            config['ticket_counter'] = counter
            await save_ticket_config(guild_id, config)
            
            ticket_num = f"#{counter:04d}"
            
            # Permissions Overwrites Setup
            # Explicitly include guild.me to prevent bot lockout!
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True, manage_channels=True, manage_permissions=True)
            }
            
            for r_id in config.get('support_roles', []):
                role = guild.get_role(int(r_id))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True)
            for r_id in config.get('admin_roles', []):
                role = guild.get_role(int(r_id))
                if role:
                    overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True)
                    
            parent_cat_id = None
            for cat in config.get('categories', []):
                if cat['name'] == self.category:
                    parent_cat_id = cat.get('category_channel_id')
                    
            parent_category = None
            if parent_cat_id:
                try:
                    parent_category = guild.get_channel(int(parent_cat_id))
                except Exception:
                    pass
                
            # Create text channel
            channel = await guild.create_text_channel(
                name=f"ticket-{counter:04d}",
                category=parent_category,
                overwrites=overwrites,
                topic=f"Category: {self.category} | Subject: {self.subject.value}"
            )
            
            # Save Ticket Metadata
            await save_ticket(ticket_num, guild_id, str(channel.id), user_id, self.category, self.subject.value)
            
            # Render V2 greeting welcome components
            accent_color_int = parse_color(self.panel_data.get('accent_color', '')) or 3447003 # Blurple
            
            root_container = Container(accent_color=accent_color_int)
            root_container.add_item(TextDisplay(
                f"# Ticket {ticket_num}\n"
                f"Hello {user.mention}, thank you for opening a support ticket!\n"
                f"• **Subject:** {self.subject.value}\n"
                f"• **Category:** {self.category}\n"
                f"• **Description:** {self.description.value or 'No description provided.'}"
            ))
            root_container.add_item(Separator())
            
            welcome_accessory = Button(label="🔐 Support", style=discord.ButtonStyle.secondary, disabled=True, custom_id="welcome_dummy_accessory")
            root_container.add_item(Section(
                "Welcome to Support Channel",
                "Please stand by. Support staff will respond shortly.",
                accessory=welcome_accessory
            ))
            
            # Action Row containing Close, Claim, Transcript, Delete buttons
            welcome_row = ActionRow(
                Button(label="Close", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:close_ticket", emoji="🔒"),
                Button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:claim_ticket", emoji="👤"),
                Button(label="Transcript", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:transcript_ticket", emoji="📄"),
                Button(label="Delete", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:delete_ticket", emoji="🗑")
            )
            root_container.add_item(welcome_row)
            
            layout_view = LayoutView()
            layout_view.add_item(root_container)
            
            await channel.send(view=layout_view)
            await interaction.followup.send(f"🎫 **Your ticket has been created!** {channel.mention}", ephemeral=True)
            
            # Log action
            await log_ticket_action(guild, "Ticket Open", f"Ticket {ticket_num} opened by {user.mention}.\n• Category: `{self.category}`\n• Subject: `{self.subject.value}`")
        except Exception as e:
            logger.exception("Error in OpenTicketModal.on_submit:")
            await interaction.followup.send(f"❌ Failed to create ticket: {e}", ephemeral=True)

class GlobalTicketWelcomeView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception(f"Error in view {self.__class__.__name__} for item {item.custom_id or item.label if hasattr(item, 'custom_id') else 'unknown'}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:close_ticket", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.followup.send("❌ Metadata not found for this ticket.", ephemeral=True)
                return
                
            has_perm = await check_support_permission(user, guild) or (str(user.id) == ticket['user_id'])
            if not has_perm:
                await interaction.followup.send("❌ Only staff or the ticket creator can close the ticket.", ephemeral=True)
                return
                
            await update_ticket_status(str(channel.id), "closed")
            
            creator = guild.get_member(int(ticket['user_id']))
            if creator:
                await channel.set_permissions(creator, send_messages=False, read_messages=True)
                
            root_container = Container(accent_color=16711680)  # Red
            root_container.add_item(TextDisplay(
                f"🔒 **This ticket was closed by {user.mention}.**\n"
                f"The ticket creator can no longer send messages in this channel.\n"
                f"Staff can manage this closed channel using the options below."
            ))
            root_container.add_item(Separator())
            
            reopen_accessory = Button(label="🔓 Reopen", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:reopen_ticket")
            root_container.add_item(Section(
                "Manage Closed Ticket",
                "Reopen the channel or trigger transcript generation.",
                accessory=reopen_accessory
            ))
            
            layout_view = LayoutView()
            layout_view.add_item(root_container)
            await channel.send(view=layout_view)
            
            await log_ticket_action(guild, "Ticket Close", f"Ticket `{ticket['ticket_id']}` closed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in close_ticket button:")
            msg = f"❌ Error closing ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:claim_ticket", emoji="👤")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.followup.send("❌ Metadata not found.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.followup.send("❌ Only staff can claim tickets.", ephemeral=True)
                return
                
            if ticket['claimed_by']:
                claimed_staff = guild.get_member(int(ticket['claimed_by']))
                mention = claimed_staff.mention if claimed_staff else f"`{ticket['claimed_by']}`"
                await interaction.followup.send(f"❌ This ticket is already claimed by {mention}.", ephemeral=True)
                return
                
            await update_ticket_claim(str(channel.id), str(user.id))
            
            root_container = Container(accent_color=3447003)
            root_container.add_item(TextDisplay(f"👤 **Ticket claimed by {user.mention}.**\nThis staff member will now assist you."))
            root_container.add_item(Separator())
            
            unclaim_btn = Button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:unclaim_ticket")
            root_container.add_item(Section(
                "Claim Actions",
                "Staff can release this ticket if needed.",
                accessory=unclaim_btn
            ))
            
            layout_view = LayoutView()
            layout_view.add_item(root_container)
            await channel.send(view=layout_view)
            
            await log_ticket_action(guild, "Ticket Claim", f"Ticket `{ticket['ticket_id']}` claimed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in claim_ticket button:")
            msg = f"❌ Error claiming ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Transcript", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:transcript_ticket", emoji="📄")
    async def transcript_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.followup.send("❌ Metadata not found.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.followup.send("❌ Only staff can generate transcripts.", ephemeral=True)
                return
                
            # Use aiosqlite for DB transcript load
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT author_name, author_avatar, author_id, content, created_at, attachments FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
                    (ticket['ticket_id'],)
                ) as cursor:
                    rows = await cursor.fetchall()
                    messages = [dict(r) for r in rows]
                    
            html_str = generate_html_transcript(ticket['ticket_id'], messages)
            fp = io.BytesIO(html_str.encode('utf-8'))
            transcript_file = discord.File(fp, filename=f"transcript-{ticket['ticket_id'].replace('#', '')}.html")
            
            await channel.send(content="📄 **Transcript generated successfully!**", file=transcript_file)
            
            # Log transcript action
            await log_ticket_action(
                guild,
                "Transcript Generation",
                f"Transcript generated for ticket `{ticket['ticket_id']}` by {user.mention}.",
                file=discord.File(io.BytesIO(html_str.encode('utf-8')), filename=f"transcript-{ticket['ticket_id'].replace('#', '')}.html")
            )
        except Exception as e:
            logger.exception("Error in transcript_ticket button:")
            msg = f"❌ Error generating transcript: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:delete_ticket", emoji="🗑")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Metadata not found.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can delete tickets.", ephemeral=True)
                return
                
            await interaction.response.send_modal(TicketDeleteConfirmationModal())
        except Exception as e:
            logger.exception("Error in delete_ticket button:")
            msg = f"❌ Error initiating deletion: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

class TicketDeleteConfirmationModal(discord.ui.Modal, title="Confirm Ticket Deletion"):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(f"Error in modal {self.__class__.__name__}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    confirm = discord.ui.TextInput(label="Type 'DELETE' to confirm", placeholder="DELETE", required=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            if self.confirm.value.strip().upper() != "DELETE":
                await interaction.response.send_message("❌ Deletion cancelled (confirmation text did not match).", ephemeral=True)
                return
                
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            ticket = await get_ticket_by_channel(str(channel.id))
            
            await interaction.response.send_message("🗑 **Deleting channel in 5 seconds...**")
            await log_ticket_action(guild, "Ticket Delete", f"Ticket `{ticket['ticket_id'] if ticket else 'unknown'}` deleted by {user.mention}.")
            
            await asyncio.sleep(5)
            await channel.delete()
        except Exception as e:
            logger.exception("Error in TicketDeleteConfirmationModal.on_submit:")
            msg = f"❌ Failed to delete ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

class GlobalTicketControlView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception(f"Error in view {self.__class__.__name__} for item {item.custom_id or item.label if hasattr(item, 'custom_id') else 'unknown'}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    """View to handle persistent Reopen and Unclaim buttons (survives restarts)."""
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:reopen_ticket", emoji="🔓")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.followup.send("❌ Metadata not found for this ticket.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.followup.send("❌ Only staff can reopen tickets.", ephemeral=True)
                return
                
            if ticket['status'] == 'open':
                await interaction.followup.send("❌ This ticket is already open.", ephemeral=True)
                return
                
            await update_ticket_status(str(channel.id), "open")
            
            creator = guild.get_member(int(ticket['user_id']))
            if creator:
                await channel.set_permissions(creator, send_messages=True, read_messages=True)
                
            root = Container(accent_color=65280) # Green
            root.add_item(TextDisplay(f"🔓 **Ticket reopened by {user.mention}.**\nMessaging has been restored."))
            
            layout = LayoutView()
            layout.add_item(root)
            await channel.send(view=layout)
            
            await log_ticket_action(guild, "Ticket Reopen", f"Ticket `{ticket['ticket_id']}` reopened by {user.mention}.")
        except Exception as e:
            logger.exception("Error in reopen persistent button:")
            msg = f"❌ Failed to reopen ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:unclaim_ticket", emoji="👤")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.followup.send("❌ Metadata not found.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.followup.send("❌ Only support staff can unclaim tickets.", ephemeral=True)
                return
                
            if not ticket['claimed_by']:
                await interaction.followup.send("❌ Ticket is not currently claimed.", ephemeral=True)
                return
                
            await update_ticket_claim(str(channel.id), None)
            
            root = Container(accent_color=15105570) # Orange
            root.add_item(TextDisplay(f"👤 **Ticket unclaimed by {user.mention}.**\nIt is now open for any staff member."))
            
            layout = LayoutView()
            layout.add_item(root)
            await channel.send(view=layout)
            
            await log_ticket_action(guild, "Ticket Unclaim", f"Ticket `{ticket['ticket_id']}` unclaimed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in unclaim persistent button:")
            msg = f"❌ Failed to unclaim ticket: {e}"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# TICKET PANEL CONFIGURATION BUILDER VIEW
# ══════════════════════════════════════════════════════════════════════

class TicketPanelBuilderView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.exception(f"Error in view {self.__class__.__name__} for item {item.custom_id or item.label if hasattr(item, 'custom_id') else 'unknown'}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    def __init__(self, user_id: str, guild_id: str):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.guild_id = guild_id
        self.config = {
            "title": "CynexCloud Support System",
            "description": "Click the button below to get help from our support team.",
            "accent_color": "#206694",
            "categories": [],
            "image_url": "",
            "thumbnail_url": "",
            "support_roles": []
        }
        
    def setup_items(self):
        self.clear_items()
        self.add_item(ConfigurePanelTextButton(self))
        self.add_item(ConfigurePanelCategoriesButton(self))
        self.add_item(ConfigurePanelMediaButton(self))
        self.add_item(PublishPanelButton(self))
        
    async def update_view(self, interaction: discord.Interaction):
        try:
            embed = discord.Embed(
                title="🎫 CynexCloud Ticket Panel Builder 🎫",
                description="Design your Components V2 support ticket panels visually.",
                color=discord.Color.green()
            )
            
            cats_raw = ", ".join([c['name'] for c in self.config['categories']]) or "None"
            desc_val = self.config['description']
            if len(desc_val) > 250:
                desc_val = desc_val[:247] + "..."
            cats_val = cats_raw
            if len(cats_val) > 250:
                cats_val = cats_val[:247] + "..."
            img_val = self.config['image_url'] or 'None'
            if len(img_val) > 150:
                img_val = img_val[:147] + "..."
            thumb_val = self.config['thumbnail_url'] or 'None'
            if len(thumb_val) > 150:
                thumb_val = thumb_val[:147] + "..."
                
            embed.add_field(
                name="📋 Panel Settings",
                value=f"• **Title:** `{self.config['title']}`\n"
                      f"• **Description:** `{desc_val}`\n"
                      f"• **Accent Color:** `{self.config['accent_color']}`\n"
                      f"• **Categories:** `{cats_val}`\n"
                      f"• **Image URL:** `{img_val}`\n"
                      f"• **Thumbnail URL:** `{thumb_val}`",
                inline=False
            )
            
            tree = "Panel Container (Root)\n"
            if self.config['thumbnail_url']:
                tree += "├─ Thumbnail Accessory\n"
            tree += f"├─ Text Display (\"{self.config['title']}\")\n"
            tree += "├─ Separator\n"
            if self.config['image_url']:
                tree += "├─ Media Gallery\n"
            tree += "└─ Action Row (🎫 Create Ticket)"
            
            embed.add_field(name="🌲 Panel V2 Layout Tree", value=f"```\n{tree}\n```", inline=False)
            
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
        except Exception as e:
            logger.exception("Error in update_view:")

class ConfigurePanelTextModal(discord.ui.Modal, title="Configure Panel Text"):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(f"Error in modal {self.__class__.__name__}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    panel_title = discord.ui.TextInput(label="Panel Title", placeholder="Support Support", required=True)
    panel_desc = discord.ui.TextInput(label="Panel Description", style=discord.TextStyle.paragraph, placeholder="Click the button to open a ticket...", required=True)
    accent_color = discord.ui.TextInput(label="Accent Color (Hex e.g. #ff0000)", required=False, placeholder="#206694")
    
    def __init__(self, builder_view: TicketPanelBuilderView):
        super().__init__()
        self.builder_view = builder_view
        self.panel_title.default = builder_view.config['title']
        self.panel_desc.default = builder_view.config['description']
        self.accent_color.default = builder_view.config['accent_color']
        
    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = self.accent_color.value.strip()
            if color:
                parsed = parse_color(color)
                if parsed is None:
                    await interaction.response.send_message("❌ Invalid hex color code.", ephemeral=True)
                    return
                    
            self.builder_view.config['title'] = self.panel_title.value.strip()
            self.builder_view.config['description'] = self.panel_desc.value.strip()
            self.builder_view.config['accent_color'] = color
            
            await self.builder_view.update_view(interaction)
        except Exception as e:
            logger.exception("Error in ConfigurePanelTextModal.on_submit:")

class ConfigurePanelCategoriesModal(discord.ui.Modal, title="Configure Categories"):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(f"Error in modal {self.__class__.__name__}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    categories_def = discord.ui.TextInput(
        label="Categories (Format: Name | CategoryID)",
        style=discord.TextStyle.paragraph,
        placeholder="Billing | 123456789012345678\nGeneral | 987654321098765432",
        required=True
    )
    
    def __init__(self, builder_view: TicketPanelBuilderView):
        super().__init__()
        self.builder_view = builder_view
        lines = []
        for cat in builder_view.config['categories']:
            line = cat['name']
            if cat.get('category_channel_id'):
                line += f" | {cat['category_channel_id']}"
            lines.append(line)
        self.categories_def.default = "\n".join(lines)
        
    async def on_submit(self, interaction: discord.Interaction):
        try:
            text = self.categories_def.value.strip()
            categories = []
            for line in text.split('\n'):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split('|')]
                cat_name = parts[0]
                cat_id = parts[1] if len(parts) > 1 else None
                categories.append({
                    "name": cat_name,
                    "category_channel_id": cat_id
                })
                
            if not categories:
                await interaction.response.send_message("❌ Must configure at least one category.", ephemeral=True)
                return
                
            self.builder_view.config['categories'] = categories
            await self.builder_view.update_view(interaction)
        except Exception as e:
            logger.exception("Error in ConfigurePanelCategoriesModal.on_submit:")

class ConfigurePanelMediaModal(discord.ui.Modal, title="Configure Panel Media"):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception(f"Error in modal {self.__class__.__name__}:")
        msg = f"❌ An error occurred: {error}"
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    image = discord.ui.TextInput(label="Image URL (Image / GIF)", required=False, placeholder="https://...")
    thumbnail = discord.ui.TextInput(label="Thumbnail URL", required=False, placeholder="https://...")
    
    def __init__(self, builder_view: TicketPanelBuilderView):
        super().__init__()
        self.builder_view = builder_view
        self.image.default = builder_view.config['image_url']
        self.thumbnail.default = builder_view.config['thumbnail_url']
        
    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.builder_view.config['image_url'] = self.image.value.strip()
            self.builder_view.config['thumbnail_url'] = self.thumbnail.value.strip()
            await self.builder_view.update_view(interaction)
        except Exception as e:
            logger.exception("Error in ConfigurePanelMediaModal.on_submit:")

class ConfigurePanelTextButton(discord.ui.Button):
    def __init__(self, view: TicketPanelBuilderView):
        super().__init__(label="✏ Configure Text", style=discord.ButtonStyle.secondary, row=0)
        self.builder_view = view
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfigurePanelTextModal(self.builder_view))

class ConfigurePanelCategoriesButton(discord.ui.Button):
    def __init__(self, view: TicketPanelBuilderView):
        super().__init__(label="🗂 Configure Categories", style=discord.ButtonStyle.secondary, row=0)
        self.builder_view = view
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfigurePanelCategoriesModal(self.builder_view))

class ConfigurePanelMediaButton(discord.ui.Button):
    def __init__(self, view: TicketPanelBuilderView):
        super().__init__(label="🖼 Configure Media", style=discord.ButtonStyle.secondary, row=0)
        self.builder_view = view
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfigurePanelMediaModal(self.builder_view))

class PublishPanelButton(discord.ui.Button):
    def __init__(self, view: TicketPanelBuilderView):
        super().__init__(label="📤 Publish Panel", style=discord.ButtonStyle.secondary, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        try:
            if not self.builder_view.config['categories']:
                await interaction.response.send_message("❌ Please configure at least one category before publishing.", ephemeral=True)
                return
                
            await interaction.response.defer(ephemeral=True)
            
            # Build Components V2 Panel
            v2_view = LayoutView()
            accent_int = parse_color(self.builder_view.config['accent_color']) or 3447003
            root = Container(accent_color=accent_int)
            
            # Text display title & description
            root.add_item(TextDisplay(f"# {self.builder_view.config['title']}\n{self.builder_view.config['description']}"))
            root.add_item(Separator())
            
            if self.builder_view.config['thumbnail_url']:
                root.add_item(Section("\u200b", accessory=Thumbnail(self.builder_view.config['thumbnail_url'])))
                
            if self.builder_view.config['image_url']:
                root.add_item(MediaGallery(MediaGalleryItem(self.builder_view.config['image_url'])))
                
            # Create button row
            root.add_item(ActionRow(
                Button(label="Create Ticket", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:create_ticket", emoji="🎫")
            ))
            
            v2_view.add_item(root)
            
            try:
                print("Generated Component Tree (Ticket Panel):")
                print(print_v2_component_tree(v2_view))
            except Exception as tree_err:
                logger.warning(f"Could not print component tree: {tree_err}")
                
            try:
                validate_v2_layout(v2_view)
                msg = await interaction.channel.send(view=v2_view)
            except Exception as e:
                await interaction.followup.send(f"V2 Validation Error: {e}", ephemeral=True)
                return
            
            # Save Panel metadata
            await save_ticket_panel(str(interaction.guild_id), str(interaction.channel_id), str(msg.id), self.builder_view.config['title'], self.builder_view.config)
            
            # Auto-update general guild category configs
            g_config = await get_ticket_config(str(interaction.guild_id))
            g_config['categories'] = self.builder_view.config['categories']
            await save_ticket_config(str(interaction.guild_id), g_config)
            
            await interaction.followup.send("📤 **Successfully published ticket panel in this channel!**", ephemeral=True)
        except Exception as e:
            logger.exception("Error in PublishPanelButton.callback:")
            await interaction.followup.send(f"❌ Failed to publish panel: {e}", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# SLASH TICKET COMMAND TREE GROUP (/ticket)
# ══════════════════════════════════════════════════════════════════════

class TicketGroup(app_commands.Group, name="ticket"):
    def __init__(self, bot: commands.Bot):
        super().__init__(description="CynexCloud Support Ticket commands")
        self.bot = bot
        
    @app_commands.command(name="setup", description="Configure server support ticket settings")
    @app_commands.describe(
        support_role="Staff Support Role",
        admin_role="Admin Management Role",
        log_channel="Channel for logging ticket actions",
        limit="Max open tickets per user"
    )
    async def setup(self, interaction: discord.Interaction, support_role: discord.Role, admin_role: discord.Role, log_channel: discord.TextChannel, limit: int = 3):
        try:
            user = interaction.user
            if not user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Only Administrators can set up the ticket system.", ephemeral=True)
                return
                
            guild_id = str(interaction.guild_id)
            config = await get_ticket_config(guild_id)
            
            config['support_roles'] = [str(support_role.id)]
            config['admin_roles'] = [str(admin_role.id)]
            config['log_channel_id'] = str(log_channel.id)
            config['max_tickets_per_user'] = limit
            
            await save_ticket_config(guild_id, config)
            await interaction.response.send_message(
                f"⚙ **Support Ticket System configured successfully!**\n"
                f"• Support Role: {support_role.mention}\n"
                f"• Admin Role: {admin_role.mention}\n"
                f"• Log Channel: {log_channel.mention}\n"
                f"• Limit Per User: `{limit}`",
                ephemeral=True
            )
        except Exception as e:
            logger.exception("Error in /ticket setup:")
            await interaction.response.send_message(f"❌ Failed to configure settings: {e}", ephemeral=True)

    @app_commands.command(name="panel", description="Launch the ticket panel builder")
    async def panel(self, interaction: discord.Interaction):
        try:
            user = interaction.user
            if not user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Only Administrators can create ticket panels.", ephemeral=True)
                return
                
            await interaction.response.defer(ephemeral=True)
            
            view = TicketPanelBuilderView(str(user.id), str(interaction.guild_id))
            view.setup_items()
            
            embed = discord.Embed(
                title="🎫 CynexCloud Ticket Panel Builder 🎫",
                description="Design your Components V2 support ticket panels visually.",
                color=discord.Color.green()
            )
            embed.add_field(name="🌲 Panel V2 Layout Tree", value="```\nPanel Container (Root)\n+- Action Row (🎫 Create Ticket)\n```")
            
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            logger.exception("Error in /ticket panel:")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to launch builder: {e}", ephemeral=True)

    @app_commands.command(name="close", description="Close the current support ticket")
    async def close(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ This command can only be used inside a ticket channel.", ephemeral=True)
                return
                
            has_perm = await check_support_permission(user, guild) or (str(user.id) == ticket['user_id'])
            if not has_perm:
                await interaction.response.send_message("❌ You do not have permissions to close this ticket.", ephemeral=True)
                return
                
            if ticket['status'] == 'closed':
                await interaction.response.send_message("❌ This ticket is already closed.", ephemeral=True)
                return
                
            await interaction.response.send_message("🔒 **Closing ticket...**")
            
            # Database Update
            await update_ticket_status(str(channel.id), "closed")
            
            creator = guild.get_member(int(ticket['user_id']))
            if creator:
                await channel.set_permissions(creator, send_messages=False, read_messages=True)
                
            root_container = Container(accent_color=16711680)  # Red
            root_container.add_item(TextDisplay(
                f"🔒 **This ticket was closed by {user.mention}.**\n"
                f"The ticket creator can no longer send messages in this channel.\n"
                f"Staff can manage this closed channel using the options below."
            ))
            root_container.add_item(Separator())
            
            reopen_accessory = Button(label="🔓 Reopen", style=discord.ButtonStyle.secondary, custom_id="cynexcloud:reopen_ticket")
            root_container.add_item(Section(
                "Manage Closed Ticket",
                "Reopen the channel or trigger transcript generation.",
                accessory=reopen_accessory
            ))
            
            layout_view = LayoutView()
            layout_view.add_item(root_container)
            await channel.send(view=layout_view)
            
            await log_ticket_action(guild, "Ticket Close", f"Ticket `{ticket['ticket_id']}` closed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket close:")

    @app_commands.command(name="reopen", description="Reopen a closed support ticket")
    async def reopen(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can reopen tickets.", ephemeral=True)
                return
                
            if ticket['status'] == 'open':
                await interaction.response.send_message("❌ This ticket is already open.", ephemeral=True)
                return
                
            await interaction.response.send_message("🔓 **Reopening ticket...**")
            await update_ticket_status(str(channel.id), "open")
            
            creator = guild.get_member(int(ticket['user_id']))
            if creator:
                await channel.set_permissions(creator, send_messages=True, read_messages=True)
                
            root = Container(accent_color=65280) # Green
            root.add_item(TextDisplay(f"🔓 **Ticket reopened by {user.mention}.**\nMessaging has been restored."))
            
            layout = LayoutView()
            layout.add_item(root)
            await channel.send(view=layout)
            
            await log_ticket_action(guild, "Ticket Reopen", f"Ticket `{ticket['ticket_id']}` reopened by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket reopen:")

    @app_commands.command(name="delete", description="Delete the support ticket channel")
    async def delete(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can delete tickets.", ephemeral=True)
                return
                
            await interaction.response.send_modal(TicketDeleteConfirmationModal())
        except Exception as e:
            logger.exception("Error in /ticket delete:")

    @app_commands.command(name="claim", description="Claim current support ticket")
    async def claim(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only support staff can claim tickets.", ephemeral=True)
                return
                
            if ticket['claimed_by']:
                staff = guild.get_member(int(ticket['claimed_by']))
                mention = staff.mention if staff else f"`{ticket['claimed_by']}`"
                await interaction.response.send_message(f"❌ Already claimed by {mention}.", ephemeral=True)
                return
                
            await update_ticket_claim(str(channel.id), str(user.id))
            
            root = Container(accent_color=3447003)
            root.add_item(TextDisplay(f"👤 **Ticket claimed by {user.mention}.**\nThey will assist you shortly."))
            layout = LayoutView()
            layout.add_item(root)
            await channel.send(view=layout)
            
            await log_ticket_action(guild, "Ticket Claim", f"Ticket `{ticket['ticket_id']}` claimed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket claim:")

    @app_commands.command(name="unclaim", description="Unclaim current support ticket")
    async def unclaim(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only support staff can unclaim tickets.", ephemeral=True)
                return
                
            if not ticket['claimed_by']:
                await interaction.response.send_message("❌ Ticket is not currently claimed.", ephemeral=True)
                return
                
            await update_ticket_claim(str(channel.id), None)
            
            root = Container(accent_color=15105570) # Orange
            root.add_item(TextDisplay(f"👤 **Ticket unclaimed by {user.mention}.**\nIt is now open for any staff member."))
            layout = LayoutView()
            layout.add_item(root)
            await channel.send(view=layout)
            
            await log_ticket_action(guild, "Ticket Unclaim", f"Ticket `{ticket['ticket_id']}` unclaimed by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket unclaim:")

    @app_commands.command(name="rename", description="Rename ticket channel")
    @app_commands.describe(name="New channel name")
    async def rename(self, interaction: discord.Interaction, name: str):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can rename tickets.", ephemeral=True)
                return
                
            old_name = channel.name
            await channel.edit(name=name)
            
            await interaction.response.send_message(f"📝 **Channel renamed from `{old_name}` to `{name}`.**")
            await log_ticket_action(guild, "Channel Rename", f"Ticket `{ticket['ticket_id']}` renamed from `{old_name}` to `{name}` by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket rename:")

    @app_commands.command(name="add", description="Add a member to the support ticket")
    @app_commands.describe(member="Member to add")
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can add members.", ephemeral=True)
                return
                
            await channel.set_permissions(member, read_messages=True, send_messages=True, embed_links=True, attach_files=True, read_message_history=True)
            await interaction.response.send_message(f"➕ {member.mention} **added to this support ticket.**")
            await log_ticket_action(guild, "Member Add", f"{member.mention} added to ticket `{ticket['ticket_id']}` by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket add:")

    @app_commands.command(name="remove", description="Remove a member from the support ticket")
    @app_commands.describe(member="Member to remove")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can remove members.", ephemeral=True)
                return
                
            await channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(f"➖ {member.mention} **removed from this support ticket.**")
            await log_ticket_action(guild, "Member Remove", f"{member.mention} removed from ticket `{ticket['ticket_id']}` by {user.mention}.")
        except Exception as e:
            logger.exception("Error in /ticket remove:")

    @app_commands.command(name="transcript", description="Generate Styled HTML transcript in-memory")
    async def transcript(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            user = interaction.user
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only staff can generate transcripts.", ephemeral=True)
                return
                
            await interaction.response.defer()
            
            # aiosqlite implementation to prevent blocking/crashing context manager issues
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT author_name, author_avatar, author_id, content, created_at, attachments FROM ticket_messages WHERE ticket_id = ? ORDER BY id ASC",
                    (ticket['ticket_id'],)
                ) as cursor:
                    rows = await cursor.fetchall()
                    messages = [dict(r) for r in rows]
                    
            html_str = generate_html_transcript(ticket['ticket_id'], messages)
            fp = io.BytesIO(html_str.encode('utf-8'))
            t_file = discord.File(fp, filename=f"transcript-{ticket['ticket_id'].replace('#', '')}.html")
            
            await interaction.followup.send(content="📄 **Transcript generated successfully!**", file=t_file)
            
            # Send log
            log_fp = io.BytesIO(html_str.encode('utf-8'))
            log_file = discord.File(log_fp, filename=f"transcript-{ticket['ticket_id'].replace('#', '')}.html")
            await log_ticket_action(guild, "Transcript Generation", f"Transcript generated for ticket `{ticket['ticket_id']}` by {user.mention}.", file=log_file)
        except Exception as e:
            logger.exception("Error in /ticket transcript:")

    @app_commands.command(name="info", description="Display support ticket metadata information")
    async def info(self, interaction: discord.Interaction):
        try:
            channel = interaction.channel
            guild = interaction.guild
            
            ticket = await get_ticket_by_channel(str(channel.id))
            if not ticket:
                await interaction.response.send_message("❌ Not inside a ticket channel.", ephemeral=True)
                return
                
            creator = guild.get_member(int(ticket['user_id']))
            creator_mention = creator.mention if creator else f"`{ticket['user_id']}`"
            
            claimed = "None"
            if ticket['claimed_by']:
                staff = guild.get_member(int(ticket['claimed_by']))
                claimed = staff.mention if staff else f"`{ticket['claimed_by']}`"
                
            embed = discord.Embed(
                title=f"🎫 Ticket Details: {ticket['ticket_id']}",
                color=discord.Color.blue()
            )
            embed.add_field(name="User", value=creator_mention, inline=True)
            embed.add_field(name="Category", value=ticket['category'], inline=True)
            embed.add_field(name="Subject", value=ticket['subject'], inline=True)
            embed.add_field(name="Status", value=f"`{ticket['status'].upper()}`", inline=True)
            embed.add_field(name="Claimed Staff", value=claimed, inline=True)
            embed.add_field(name="Created At", value=ticket['created_at'], inline=True)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("Error in /ticket info:")
            await interaction.response.send_message(f"❌ Failed to retrieve info: {e}", ephemeral=True)

    @app_commands.command(name="list", description="List open support tickets in the server")
    async def list_open_tickets(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            user = interaction.user
            
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only support staff can list open tickets.", ephemeral=True)
                return
                
            guild_id = str(guild.id)
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT ticket_id, channel_id, user_id, category, subject FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY id ASC",
                    (guild_id,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    tickets_list = [dict(r) for r in rows]
                    
            if not tickets_list:
                await interaction.response.send_message("📂 No open tickets in this guild.", ephemeral=True)
                return
                
            embed = discord.Embed(title="📂 Open Support Tickets", color=discord.Color.orange())
            desc = ""
            for idx, t in enumerate(tickets_list, 1):
                creator = guild.get_member(int(t['user_id']))
                creator_mention = creator.mention if creator else f"`{t['user_id']}`"
                channel = guild.get_channel(int(t['channel_id']))
                chan_mention = channel.mention if channel else f"`{t['channel_id']}`"
                
                desc += f"`{idx}.` **{t['ticket_id']}** — {chan_mention} by {creator_mention}\n• Category: `{t['category']}` | Subject: `{t['subject']}`\n"
                
            embed.description = desc
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("Error in /ticket list:")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to list tickets: {e}", ephemeral=True)

    @app_commands.command(name="stats", description="Display support ticket statistics")
    async def stats(self, interaction: discord.Interaction):
        try:
            guild = interaction.guild
            user = interaction.user
            
            if not await check_support_permission(user, guild):
                await interaction.response.send_message("❌ Only support staff can check ticket stats.", ephemeral=True)
                return
                
            guild_id = str(guild.id)
            async with aiosqlite.connect(DB_PATH) as conn:
                # Total opened tickets
                async with conn.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ?", (guild_id,)) as cursor:
                    total = (await cursor.fetchone())[0]
                # Total closed tickets
                async with conn.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'closed'", (guild_id,)) as cursor:
                    closed = (await cursor.fetchone())[0]
                # Total open tickets
                async with conn.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'", (guild_id,)) as cursor:
                    open_count = (await cursor.fetchone())[0]
                # Total claimed tickets
                async with conn.execute("SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND claimed_by IS NOT NULL", (guild_id,)) as cursor:
                    claimed = (await cursor.fetchone())[0]
                    
            embed = discord.Embed(
                title="📊 CynexCloud Support Ticket Statistics",
                color=discord.Color.green()
            )
            embed.add_field(name="Open Tickets", value=f"`{open_count}`", inline=True)
            embed.add_field(name="Closed Tickets", value=f"`{closed}`", inline=True)
            embed.add_field(name="Claimed Tickets", value=f"`{claimed}`", inline=True)
            embed.add_field(name="Total Tickets Created", value=f"`{total}`", inline=True)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("Error in /ticket stats:")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to check stats: {e}", ephemeral=True)
