import asyncio
import json
import logging
import io
import copy
from datetime import datetime
from typing import Optional, List, Dict, Any, Union, Literal
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

import tickets
from tickets import validate_v2_layout, print_v2_component_tree, parse_color

# ══════════════════════════════════════════════════════════════════════
# BOT INITIALIZATION & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

# The bot token is loaded from token.txt or the DISCORD_TOKEN environment variable.
import os
if os.path.exists("token.txt"):
    with open("token.txt", "r", encoding="utf-8") as f:
        TOKEN = f.read().strip()
else:
    TOKEN = os.getenv("DISCORD_TOKEN", "PUT_TOKEN_HERE")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("Breeze")

DB_PATH = "breeze.db"

# Auto-migrate database from cynex.db to breeze.db if it exists
import shutil
import os
if os.path.exists("cynex.db") and not os.path.exists("breeze.db"):
    try:
        shutil.copy("cynex.db", "breeze.db")
        logger.info("Auto-migrated database from cynex.db to breeze.db successfully.")
    except Exception as e:
        logger.error(f"Failed to migrate cynex.db to breeze.db: {e}")


class BreezeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # Required to capture message transcripts
        intents.members = True          # Required for welcome system events
        super().__init__(command_prefix="breeze!", intents=intents)
        self.start_time = datetime.now()

    async def setup_hook(self):
        # Initialize SQLite DB schemas
        await init_db()
        await tickets.init_ticket_db()
        
        # Register persistent views for restarts survival
        self.add_view(tickets.GlobalTicketPanelView())
        self.add_view(tickets.GlobalTicketWelcomeView())
        self.add_view(tickets.GlobalTicketControlView())
        
        # Register Slash Commands Group
        self.tree.add_command(ContainerGroup(self))
        self.tree.add_command(tickets.TicketGroup(self))
        
        # Load extensions
        await self.load_extension("utilities")
        await self.load_extension("reviews")
        await self.load_extension("suggestions")
        await self.load_extension("welcome")
        await self.load_extension("dashboard")
        await self.load_extension("moderation")
        await self.load_extension("invites")
        await self.load_extension("boosts")
        await self.load_extension("activity")
        await self.load_extension("reactionroles")
        await self.load_extension("leveling")
        
        await self.tree.sync()
        logger.info("Command tree synced globally.")

bot = BreezeBot()

# ══════════════════════════════════════════════════════════════════════
# DATABASE OPERATIONS (breeze.db)
# ══════════════════════════════════════════════════════════════════════

async def init_db():
    """Initializes SQLite tables in breeze.db if they do not exist."""
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        # User finalized saved container builds
        await db.execute("""
            CREATE TABLE IF NOT EXISTS builders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                build_name TEXT NOT NULL,
                build_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Autosaved sessions for timeout recovery
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id TEXT PRIMARY KEY,
                session_data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Library attachments uploaded by users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                data BLOB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Logs of published messages using V2 layouts
        await db.execute("""
            CREATE TABLE IF NOT EXISTS published (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                build_name TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 1. Anti-Swear System Tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antiswear_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                max_warnings INTEGER DEFAULT 3,
                timeout_duration INTEGER DEFAULT 600,
                use_regex INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antiswear_words (
                guild_id TEXT,
                word TEXT,
                PRIMARY KEY (guild_id, word)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antiswear_roles (
                guild_id TEXT,
                role_id TEXT,
                PRIMARY KEY (guild_id, role_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antiswear_channels (
                guild_id TEXT,
                channel_id TEXT,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antiswear_warnings (
                guild_id TEXT,
                user_id TEXT,
                warning_count INTEGER DEFAULT 0,
                last_warned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # 2. Boost Alerts / Tracking Tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_alert_configs (
                guild_id TEXT PRIMARY KEY,
                channel_id TEXT,
                role_id TEXT,
                highest_tier INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_track (
                guild_id TEXT,
                user_id TEXT,
                first_boost_date TIMESTAMP,
                total_boosts INTEGER DEFAULT 0,
                is_boosting INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boost_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                user_id TEXT,
                action TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. Invite Tracking Tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_stats (
                guild_id TEXT,
                user_id TEXT,
                total INTEGER DEFAULT 0,
                regular INTEGER DEFAULT 0,
                fake INTEGER DEFAULT 0,
                left INTEGER DEFAULT 0,
                bonus INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invited_by (
                guild_id TEXT,
                invited_user_id TEXT,
                inviter_user_id TEXT,
                invite_code TEXT,
                status TEXT,
                PRIMARY KEY (guild_id, invited_user_id)
            )
        """)

        # 4. Message Tracking Tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_settings (
                guild_id TEXT PRIMARY KEY,
                ignore_bots INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_activity (
                guild_id TEXT,
                user_id TEXT,
                total_messages INTEGER DEFAULT 0,
                attachments_count INTEGER DEFAULT 0,
                images_count INTEGER DEFAULT 0,
                links_count INTEGER DEFAULT 0,
                voice_messages_count INTEGER DEFAULT 0,
                last_active_date TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS message_daily_stats (
                guild_id TEXT,
                user_id TEXT,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, date)
            )
        """)

        # Performance Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_msg_activity_guild_total ON message_activity (guild_id, total_messages DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_msg_daily_guild_date ON message_daily_stats (guild_id, date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_invite_stats_guild_total ON invite_stats (guild_id, total DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_boost_track_guild_boosting ON boost_track (guild_id, is_boosting DESC)")

        # 5. Reaction Roles Tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id TEXT,
                message_id TEXT,
                emoji TEXT,
                role_id TEXT,
                PRIMARY KEY (guild_id, message_id, emoji)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_role_panels (
                guild_id TEXT,
                message_id TEXT,
                channel_id TEXT,
                title TEXT,
                description TEXT,
                multi_role INTEGER DEFAULT 1,
                PRIMARY KEY (guild_id, message_id)
            )
        """)

        await db.commit()
    logger.info("Database initialized successfully.")

# Session Helper Functions
async def get_session_from_db(user_id: str) -> Optional[dict]:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT session_data FROM sessions WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return None
    return None

async def save_session_to_db(user_id: str, session: dict):
    import aiosqlite
    session_copy = copy.deepcopy(session)
    session_copy['undo_stack'] = []
    session_copy['undo_props_stack'] = []
    session_copy['redo_stack'] = []
    session_copy['redo_props_stack'] = []
    session_copy['selected_index'] = None
    
    session_json = json.dumps(session_copy)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sessions (user_id, session_data, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (user_id, session_json)
        )
        await db.commit()

async def delete_session_from_db(user_id: str):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.commit()

# Container Build Helper Functions
async def save_build_to_db(user_id: str, name: str, session: dict):
    import aiosqlite
    build_data = json.dumps({
        "components": session["components"],
        "container_props": session["container_props"]
    })
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO builders (user_id, build_name, build_data, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, name, build_data)
        )
        await db.commit()

async def load_build_from_db(user_id: str, name: str) -> Optional[dict]:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT build_data FROM builders WHERE user_id = ? AND build_name = ?", (user_id, name)) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return None
    return None

async def delete_build_from_db(user_id: str, name: str) -> bool:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM builders WHERE user_id = ? AND build_name = ?", (user_id, name)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
        await db.execute("DELETE FROM builders WHERE user_id = ? AND build_name = ?", (user_id, name))
        await db.commit()
    return True

async def list_builds_from_db(user_id: str) -> List[dict]:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT build_name, created_at FROM builders WHERE user_id = ? ORDER BY created_at DESC", (user_id,)) as cursor:
            rows = await cursor.fetchall()
            return [{"build_name": r[0], "created_at": r[1]} for r in rows]

# Attachment Helper Functions
async def save_attachment_to_db(user_id: str, filename: str, data: bytes) -> int:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO attachments (user_id, filename, data, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, filename, data)
        )
        await db.commit()
        return cursor.lastrowid

async def get_attachment_by_id(user_id: str, file_id: int) -> Optional[dict]:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT filename, data FROM attachments WHERE user_id = ? AND id = ?", (user_id, file_id)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"filename": row[0], "data": row[1]}
    return None

async def get_attachment_by_name(user_id: str, filename: str) -> Optional[dict]:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT filename, data FROM attachments WHERE user_id = ? AND filename = ? ORDER BY id DESC LIMIT 1", (user_id, filename)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"filename": row[0], "data": row[1]}
    return None

# Published Tracking Helper
async def save_published_to_db(user_id: str, channel_id: str, message_id: str, name: Optional[str]):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO published (user_id, channel_id, message_id, build_name, published_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, channel_id, message_id, name)
        )
        await db.commit()

# ══════════════════════════════════════════════════════════════════════
# COMPONENT UTILITIES & RENDERING LOGIC
# ══════════════════════════════════════════════════════════════════════



def get_component_summary(comp: dict) -> str:
    """Returns a short description of the component configuration."""
    t = comp['type']
    if t == 'container':
        accent = comp.get('accent_color') or 'None'
        spoiler = 'Yes' if comp.get('spoiler') else 'No'
        return f"Accent: {accent}, Spoiler: {spoiler}"
    elif t == 'text_display':
        content = comp.get('content', '')
        return f"\"{content[:30]}...\"" if len(content) > 30 else f"\"{content}\""
    elif t == 'separator':
        visible = 'Visible' if comp.get('visible', True) else 'Hidden'
        spacing = comp.get('spacing', 'small')
        return f"{spacing.title()} Spacing, {visible}"
    elif t == 'section':
        title = comp.get('title', '')
        desc = comp.get('description', '')
        acc = comp.get('accessory_type', 'none')
        return f"Title: {title}, Desc: {desc[:15]}, Acc: {acc}"
    elif t == 'media':
        url = comp.get('url', '')
        m_type = comp.get('media_type', 'image')
        return f"{m_type.title()} URL: {url[:30]}..." if len(url) > 30 else f"{m_type.title()} URL: {url}"
    elif t == 'action_row':
        buttons = comp.get('buttons', [])
        return f"{len(buttons)} buttons"
    elif t == 'file':
        url_or_id = comp.get('url_or_id', '')
        spoiler = 'Yes' if comp.get('spoiler') else 'No'
        return f"Ref: {url_or_id[:20]}, Spoiler: {spoiler}"
    return "Unknown component"

def generate_tree_view(components: List[dict], container_props: dict) -> str:
    """Builds a nice Unicode text layout tree showing the components nesting."""
    accent = container_props.get('accent_color') or 'None'
    spoiler = 'Yes' if container_props.get('spoiler') else 'No'
    
    root_node = {
        "name": f"Container (Root | Accent: {accent}, Spoiler: {spoiler})",
        "children": []
    }
    
    current_container = root_node
    
    for comp in components:
        comp_type = comp['type']
        if comp_type == 'container':
            sub_accent = comp.get('accent_color') or 'None'
            sub_spoiler = 'Yes' if comp.get('spoiler') else 'No'
            sub_node = {
                "name": f"Container (Sub | Accent: {sub_accent}, Spoiler: {sub_spoiler})",
                "children": []
            }
            root_node["children"].append(sub_node)
            current_container = sub_node
        elif comp_type == 'text_display':
            content = comp.get('content', '')
            summary = f"\"{content[:15]}...\"" if len(content) > 15 else f"\"{content}\""
            current_container["children"].append({"name": f"Text Display {summary}", "children": []})
        elif comp_type == 'separator':
            spacing = comp.get('spacing', 'small')
            visible = 'Visible' if comp.get('visible', True) else 'Hidden'
            current_container["children"].append({"name": f"Separator ({spacing}, {visible})", "children": []})
        elif comp_type == 'section':
            sec_node = {"name": f"Section: \"{comp.get('title', '')}\"", "children": []}
            desc = comp.get('description', '')
            if desc:
                summary = f"\"{desc[:15]}...\"" if len(desc) > 15 else f"\"{desc}\""
                sec_node["children"].append({"name": f"Text Display {summary}", "children": []})
            acc_type = comp.get('accessory_type', 'none')
            if acc_type == 'button':
                label = comp.get('accessory_label', 'Button')
                sec_node["children"].append({"name": f"Button accessory (\"{label}\")", "children": []})
            elif acc_type == 'thumbnail':
                url = comp.get('accessory_url', '')
                summary = f"{url[:15]}..." if len(url) > 15 else url
                sec_node["children"].append({"name": f"Thumbnail accessory ({summary})", "children": []})
            current_container["children"].append(sec_node)
        elif comp_type == 'media':
            m_type = comp.get('media_type', 'image')
            url = comp.get('url', '')
            summary = f"{url[:15]}..." if len(url) > 15 else url
            current_container["children"].append({"name": f"Media ({m_type.title()}): {summary}", "children": []})
        elif comp_type == 'action_row':
            row_node = {"name": "Action Row", "children": []}
            for btn in comp.get('buttons', []):
                row_node["children"].append({"name": f"Button ({btn.get('style', 'secondary').title()}): \"{btn.get('label', '')}\"", "children": []})
            current_container["children"].append(row_node)
        elif comp_type == 'file':
            url_or_id = comp.get('url_or_id', '')
            summary = f"{url_or_id[:15]}..." if len(url_or_id) > 15 else url_or_id
            current_container["children"].append({"name": f"File: {summary}", "children": []})

    tree_lines = []
    
    def render_node(node, prefix="", is_last=True, is_root=False):
        if is_root:
            tree_lines.append(node["name"])
        else:
            connector = "└─ " if is_last else "├─ "
            tree_lines.append(prefix + connector + node["name"])
            
        child_count = len(node["children"])
        for idx, child in enumerate(node["children"]):
            child_is_last = (idx == child_count - 1)
            new_prefix = "" if is_root else prefix + ("   " if is_last else "│  ")
            render_node(child, new_prefix, child_is_last, is_root=False)
            
    render_node(root_node, is_root=True)
    return "\n".join(tree_lines)

def validate_components(components: List[dict]) -> Optional[str]:
    """Validates structure constraints and component limits."""
    if not components:
        return "❌ Build has no components. Add at least one component before previewing or publishing."
    if len(components) > 40:
        return f"❌ Components count ({len(components)}) exceeds the Discord limit of 40."
        
    for idx, comp in enumerate(components, 1):
        c_type = comp['type']
        if c_type == 'section':
            acc_type = comp.get('accessory_type')
            if acc_type not in ('button', 'thumbnail', 'none', None):
                return f"❌ Component {idx} (Section): Invalid accessory type `{acc_type}`. Must be 'button', 'thumbnail', or 'none'."
            if not comp.get('title'):
                return f"❌ Component {idx} (Section): Section title is required."
        elif c_type == 'action_row':
            buttons = comp.get('buttons', [])
            if not buttons:
                return f"❌ Component {idx} (Action Row): Must contain at least one button."
            if len(buttons) > 5:
                return f"❌ Component {idx} (Action Row): Cannot contain more than 5 buttons."
        elif c_type == 'text_display':
            if not comp.get('content'):
                return f"❌ Component {idx} (Text Display): Content cannot be empty."
        elif c_type == 'media':
            if not comp.get('url'):
                return f"❌ Component {idx} (Media): Media URL is required."
        elif c_type == 'file':
            if not comp.get('url_or_id'):
                return f"❌ Component {idx} (File): File URL or Library ID is required."
    return None

async def render_v2_layout(user_id: str, components: List[dict], container_props: dict) -> tuple[LayoutView, List[discord.File]]:
    """Generates the actual Components V2 LayoutView and attachments list from components list."""
    view = LayoutView()
    accent_int = parse_color(container_props.get('accent_color', ''))
    root = Container(
        accent_color=accent_int,
        spoiler=container_props.get('spoiler', False)
    )
    
    current_container = root
    files_to_attach = []
    
    for idx, comp in enumerate(components):
        comp_type = comp['type']
        
        if comp_type == 'container':
            sub_accent = parse_color(comp.get('accent_color', ''))
            sub_spoiler = comp.get('spoiler', False)
            sub = Container(accent_color=sub_accent, spoiler=sub_spoiler)
            root.add_item(sub)
            current_container = sub
            
        elif comp_type == 'text_display':
            td = TextDisplay(content=comp.get('content', ''))
            current_container.add_item(td)
            
        elif comp_type == 'separator':
            visible = comp.get('visible', True)
            spacing_str = comp.get('spacing', 'small')
            spacing = SeparatorSpacing.large if spacing_str == 'large' else SeparatorSpacing.small
            sep = Separator(visible=visible, spacing=spacing)
            current_container.add_item(sep)
            
        elif comp_type == 'section':
            title = comp.get('title', '')
            desc = comp.get('description', '')
            acc_type = comp.get('accessory_type', 'none')
            acc_url = comp.get('accessory_url', '')
            acc_label = comp.get('accessory_label', 'Button')
            
            accessory = None
            if acc_type == 'button':
                if acc_url:
                    accessory = Button(label=acc_label, style=discord.ButtonStyle.link, url=acc_url)
                else:
                    accessory = Button(label=acc_label, style=discord.ButtonStyle.secondary, custom_id=f"published_sec_btn_{idx}")
            elif acc_type == 'thumbnail' and acc_url:
                accessory = Thumbnail(acc_url)
            else:
                # Section requires an accessory in discord.py. If none provided, we default to a transparent Thumbnail
                accessory = Thumbnail("https://i.imgur.com/5z7P4Vq.png")
                
            children = []
            if title:
                children.append(title)
            if desc:
                children.append(desc)
            if not children:
                children.append("\u200b")
                
            sec = Section(*children, accessory=accessory)
            current_container.add_item(sec)
            
        elif comp_type == 'media':
            url = comp.get('url', '')
            m_type = comp.get('media_type', 'image')
            if m_type == 'thumbnail':
                current_container.add_item(Section("\u200b", accessory=Thumbnail(url)))
            else:
                current_container.add_item(MediaGallery(MediaGalleryItem(url)))
                
        elif comp_type == 'action_row':
            buttons = []
            for b_idx, btn in enumerate(comp.get('buttons', [])):
                label = btn.get('label', 'Button')
                url = btn.get('url')
                
                if url:
                    buttons.append(Button(label=label, style=discord.ButtonStyle.link, url=url))
                else:
                    buttons.append(Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"published_act_btn_{idx}_{b_idx}"))
            if buttons:
                current_container.add_item(ActionRow(*buttons))
                
        elif comp_type == 'file':
            url_or_id = comp.get('url_or_id', '')
            spoiler = comp.get('spoiler', False)
            
            # Resolve database file if input is file_id or matching filename
            resolved = None
            if url_or_id.isdigit():
                resolved = await get_attachment_by_id(user_id, int(url_or_id))
            if not resolved:
                resolved = await get_attachment_by_name(user_id, url_or_id)
                
            if resolved:
                file_name = resolved['filename']
                file_bytes = resolved['data']
                discord_file = discord.File(io.BytesIO(file_bytes), filename=file_name, spoiler=spoiler)
                files_to_attach.append(discord_file)
                current_container.add_item(File(discord_file, spoiler=spoiler))
            else:
                current_container.add_item(File(url_or_id, spoiler=spoiler))
                
    view.add_item(root)
    return view, files_to_attach

# ══════════════════════════════════════════════════════════════════════
# COMPONENT CONFIGURATION MODALS (CONTAINERS & BUILDERS)
# ══════════════════════════════════════════════════════════════════════

class RootContainerModal(discord.ui.Modal, title="Configure Root Container"):
    accent_color = discord.ui.TextInput(label="Accent Color (Hex e.g. #ff0000)", required=False, placeholder="#ffffff")
    spoiler = discord.ui.TextInput(label="Spoiler (yes/no or true/false)", required=False, placeholder="no")
    
    def __init__(self, builder_view):
        super().__init__()
        self.builder_view = builder_view
        props = builder_view.session['container_props']
        self.accent_color.default = props.get('accent_color', '')
        self.spoiler.default = 'yes' if props.get('spoiler') else 'no'
        
    async def on_submit(self, interaction: discord.Interaction):
        color = self.accent_color.value.strip()
        spoiler_val = self.spoiler.value.strip().lower() in ('yes', 'true', 'y')
        
        if color:
            parsed = parse_color(color)
            if parsed is None:
                await interaction.response.send_message("❌ Invalid hex color code. E.g. #ff0000 or ff0000.", ephemeral=True)
                return
                
        self.builder_view.push_undo()
        self.builder_view.session['container_props']['accent_color'] = color
        self.builder_view.session['container_props']['spoiler'] = spoiler_val
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        await self.builder_view.update_view(interaction)

class ContainerModal(discord.ui.Modal, title="Configure Sub Container"):
    accent_color = discord.ui.TextInput(label="Accent Color (Hex e.g. #ff0000)", required=False, placeholder="#ffffff")
    spoiler = discord.ui.TextInput(label="Spoiler (yes/no or true/false)", required=False, placeholder="no")
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.accent_color.default = comp.get('accent_color', '')
            self.spoiler.default = 'yes' if comp.get('spoiler') else 'no'
            
    async def on_submit(self, interaction: discord.Interaction):
        color = self.accent_color.value.strip()
        spoiler_val = self.spoiler.value.strip().lower() in ('yes', 'true', 'y')
        
        if color:
            parsed = parse_color(color)
            if parsed is None:
                await interaction.response.send_message("❌ Invalid hex color code.", ephemeral=True)
                return
                
        self.builder_view.push_undo()
        comp_data = {"type": "container", "accent_color": color, "spoiler": spoiler_val}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class TextDisplayModal(discord.ui.Modal, title="Configure Text Display"):
    content = discord.ui.TextInput(label="Text Content (Markdown supported)", style=discord.TextStyle.paragraph, placeholder="Type layout text here...", required=True)
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.content.default = comp.get('content', '')
            
    async def on_submit(self, interaction: discord.Interaction):
        content_val = self.content.value.strip()
        if not content_val:
            await interaction.response.send_message("❌ Content cannot be empty.", ephemeral=True)
            return
            
        self.builder_view.push_undo()
        comp_data = {"type": "text_display", "content": content_val}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class SeparatorModal(discord.ui.Modal, title="Configure Separator"):
    spacing = discord.ui.TextInput(label="Spacing (small/large)", required=False, placeholder="small")
    visible = discord.ui.TextInput(label="Visible (yes/no or true/false)", required=False, placeholder="yes")
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.spacing.default = comp.get('spacing', 'small')
            self.visible.default = 'yes' if comp.get('visible', True) else 'no'
            
    async def on_submit(self, interaction: discord.Interaction):
        spacing_val = self.spacing.value.strip().lower()
        visible_val = self.visible.value.strip().lower() in ('yes', 'true', 'y', '')
        
        if spacing_val not in ('small', 'large'):
            spacing_val = 'small'
            
        self.builder_view.push_undo()
        comp_data = {"type": "separator", "spacing": spacing_val, "visible": visible_val}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class SectionModal(discord.ui.Modal, title="Configure Section"):
    sec_title = discord.ui.TextInput(label="Section Title", placeholder="Section Header Title", required=True)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, placeholder="Optional section description text...", required=False)
    accessory_type = discord.ui.TextInput(label="Accessory Type (button/thumbnail/none)", placeholder="none", required=False)
    accessory_url = discord.ui.TextInput(label="Accessory URL / Button Link", placeholder="https://...", required=False)
    accessory_label = discord.ui.TextInput(label="Accessory Button Label", placeholder="Click Me", required=False)
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.sec_title.default = comp.get('title', '')
            self.description.default = comp.get('description', '')
            self.accessory_type.default = comp.get('accessory_type', 'none')
            self.accessory_url.default = comp.get('accessory_url', '')
            self.accessory_label.default = comp.get('accessory_label', '')
            
    async def on_submit(self, interaction: discord.Interaction):
        title = self.sec_title.value.strip()
        desc = self.description.value.strip()
        acc_type = self.accessory_type.value.strip().lower()
        acc_url = self.accessory_url.value.strip()
        acc_label = self.accessory_label.value.strip()
        
        if acc_type not in ('button', 'thumbnail', 'none'):
            acc_type = 'none'
        if acc_type == 'button' and not acc_label:
            acc_label = 'Button'
            
        self.builder_view.push_undo()
        comp_data = {
            "type": "section",
            "title": title,
            "description": desc,
            "accessory_type": acc_type,
            "accessory_url": acc_url,
            "accessory_label": acc_label
        }
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class MediaModal(discord.ui.Modal, title="Configure Media"):
    url = discord.ui.TextInput(label="Media URL (Image / GIF / Thumbnail)", placeholder="https://...", required=True)
    media_type = discord.ui.TextInput(label="Type (image/thumbnail)", placeholder="image", required=False)
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.url.default = comp.get('url', '')
            self.media_type.default = comp.get('media_type', 'image')
            
    async def on_submit(self, interaction: discord.Interaction):
        url_val = self.url.value.strip()
        m_type = self.media_type.value.strip().lower()
        
        if m_type not in ('image', 'thumbnail'):
            m_type = 'image'
            
        self.builder_view.push_undo()
        comp_data = {"type": "media", "url": url_val, "media_type": m_type}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class ActionRowModal(discord.ui.Modal, title="Configure Action Row"):
    buttons_def = discord.ui.TextInput(
        label="Buttons (Format: Label | Style | URL)",
        style=discord.TextStyle.paragraph,
        placeholder="Submit | secondary\nGoogle | link | https://google.com\nCancel | secondary",
        required=True
    )
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            lines = []
            for btn in comp.get('buttons', []):
                line = f"{btn['label']} | {btn['style']}"
                if btn.get('url'):
                    line += f" | {btn['url']}"
                lines.append(line)
            self.buttons_def.default = "\n".join(lines)
            
    async def on_submit(self, interaction: discord.Interaction):
        def_text = self.buttons_def.value.strip()
        buttons = []
        
        for line in def_text.split('\n'):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split('|')]
            label = parts[0]
            style_str = parts[1].lower() if len(parts) > 1 else 'secondary'
            url = parts[2] if len(parts) > 2 else None
            
            if style_str != 'link':
                style_str = 'secondary'
                
            buttons.append({
                "label": label,
                "style": style_str,
                "url": url
            })
            
        if not buttons:
            await interaction.response.send_message("❌ Action Row must contain at least one button.", ephemeral=True)
            return
        if len(buttons) > 5:
            await interaction.response.send_message("❌ Action Row cannot contain more than 5 buttons.", ephemeral=True)
            return
            
        self.builder_view.push_undo()
        comp_data = {"type": "action_row", "buttons": buttons}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class FileModal(discord.ui.Modal, title="Configure File Component"):
    url_or_id = discord.ui.TextInput(label="File URL or Stored Library ID", placeholder="Enter URL or ID from upload command", required=True)
    spoiler = discord.ui.TextInput(label="Spoiler (yes/no or true/false)", required=False, placeholder="no")
    
    def __init__(self, builder_view, idx=None):
        super().__init__()
        self.builder_view = builder_view
        self.idx = idx
        if idx is not None:
            comp = builder_view.session['components'][idx]
            self.url_or_id.default = comp.get('url_or_id', '')
            self.spoiler.default = 'yes' if comp.get('spoiler') else 'no'
            
    async def on_submit(self, interaction: discord.Interaction):
        val = self.url_or_id.value.strip()
        sp_val = self.spoiler.value.strip().lower() in ('yes', 'true', 'y')
        
        if not val:
            await interaction.response.send_message("❌ File URL or Library ID is required.", ephemeral=True)
            return
            
        self.builder_view.push_undo()
        comp_data = {"type": "file", "url_or_id": val, "spoiler": sp_val}
        
        if self.idx is None:
            self.builder_view.session['components'].append(comp_data)
            self.builder_view.selected_index = len(self.builder_view.session['components']) - 1
        else:
            self.builder_view.session['components'][self.idx] = comp_data
            
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class SaveBuildModal(discord.ui.Modal, title="Save Container Build"):
    build_name = discord.ui.TextInput(label="Build Name", placeholder="Enter unique name", required=True)
    
    def __init__(self, builder_view):
        super().__init__()
        self.builder_view = builder_view
        
    async def on_submit(self, interaction: discord.Interaction):
        name = self.build_name.value.strip()
        if not name:
            await interaction.response.send_message("❌ Build name cannot be empty.", ephemeral=True)
            return
            
        await save_build_to_db(self.builder_view.user_id, name, self.builder_view.session)
        await interaction.response.send_message(f"💾 Successfully saved build as `{name}`!", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════
# CONTROLLER VIEW FOR VISUAL BUILDER
# ══════════════════════════════════════════════════════════════════════

class BuilderView(discord.ui.View):
    def __init__(self, user_id: str, session: dict):
        super().__init__(timeout=900)  # 15 minute session timeout
        self.user_id = user_id
        self.session = session
        self.selected_index = None
        self.message = None
        self.setup_items()
        
    def push_undo(self):
        """Pushes the current state to the undo stack."""
        self.session['undo_stack'].append(copy.deepcopy(self.session['components']))
        self.session['undo_props_stack'].append(copy.deepcopy(self.session['container_props']))
        if len(self.session['undo_stack']) > 20:
            self.session['undo_stack'].pop(0)
            self.session['undo_props_stack'].pop(0)
        self.session['redo_stack'].clear()
        self.session['redo_props_stack'].clear()
        
    def setup_items(self):
        self.clear_items()
        
        # Row 0: Add Component Dropdown
        self.add_item(AddComponentSelect(self))
        
        # Row 1: Builder Control Buttons
        self.add_item(PreviewButton(self))
        self.add_item(PublishButton(self))
        self.add_item(UndoButton(self))
        self.add_item(RedoButton(self))
        self.add_item(ClearButton(self))
        
        # Row 2: Select/Edit Component Dropdown
        if self.session['components']:
            self.add_item(EditComponentSelect(self))
            
        # Row 3: Edit Action Buttons
        has_sel = self.selected_index is not None and 0 <= self.selected_index < len(self.session['components'])
        self.add_item(MoveUpButton(self, disabled=not has_sel or self.selected_index == 0))
        self.add_item(MoveDownButton(self, disabled=not has_sel or self.selected_index == len(self.session['components']) - 1))
        self.add_item(DuplicateButton(self, disabled=not has_sel))
        self.add_item(EditButton(self, disabled=not has_sel))
        self.add_item(DeleteButton(self, disabled=not has_sel))
        
        # Row 4: Database Load/Save Buttons
        self.add_item(SaveBuildButton(self))
        self.add_item(LoadBuildButton(self))
        
    async def update_view(self, interaction: discord.Interaction):
        """Re-renders the visual embed and layouts tree in real-time."""
        embed = make_builder_embed(self.user_id, self.session['components'], self.session['container_props'])
        tree_str = generate_tree_view(self.session['components'], self.session['container_props'])
        embed.add_field(name="🌲 Builder Layout Tree", value=f"```\n{tree_str}\n```", inline=False)
        
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
            
    async def on_timeout(self):
        # Save session before exit
        await save_session_to_db(self.user_id, self.session)
        if self.message:
            try:
                embed = discord.Embed(
                    title="⌛ Breeze Builder (Timed Out) ⌛",
                    description="This building session timed out due to inactivity. Progress saved as draft. Run `/container builder` to recover.",
                    color=discord.Color.red()
                )
                await self.message.edit(embed=embed, view=None)
            except Exception:
                pass

# Custom Interactive Buttons & Selects
class AddComponentSelect(discord.ui.Select):
    def __init__(self, view):
        self.builder_view = view
        options = [
            discord.SelectOption(label="Root Container Config", value="root_config", description="Configure the top-level container settings", emoji="📦"),
            discord.SelectOption(label="Sub Container", value="container", description="Add a new container element box", emoji="📦"),
            discord.SelectOption(label="Text Display", value="text_display", description="Add a rich text display element", emoji="📝"),
            discord.SelectOption(label="Separator", value="separator", description="Add a spacing divider line", emoji="➖"),
            discord.SelectOption(label="Section", value="section", description="Add a content section with accessory", emoji="🧱"),
            discord.SelectOption(label="Media Gallery", value="media", description="Add an image, thumbnail or GIF", emoji="🖼️"),
            discord.SelectOption(label="Action Row", value="action_row", description="Add a row for buttons", emoji="🔘"),
            discord.SelectOption(label="File Component", value="file", description="Add a downloadable file component", emoji="📁")
        ]
        super().__init__(
            placeholder="➕ Add component or configure root...",
            options=options,
            row=0
        )
        
    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        if val == "root_config":
            await interaction.response.send_modal(RootContainerModal(self.builder_view))
            return
            
        if len(self.builder_view.session['components']) >= 40:
            await interaction.response.send_message("❌ Maximum limit of 40 components reached.", ephemeral=True)
            return
            
        if val == "container":
            await interaction.response.send_modal(ContainerModal(self.builder_view))
        elif val == "text_display":
            await interaction.response.send_modal(TextDisplayModal(self.builder_view))
        elif val == "separator":
            await interaction.response.send_modal(SeparatorModal(self.builder_view))
        elif val == "section":
            await interaction.response.send_modal(SectionModal(self.builder_view))
        elif val == "media":
            await interaction.response.send_modal(MediaModal(self.builder_view))
        elif val == "action_row":
            await interaction.response.send_modal(ActionRowModal(self.builder_view))
        elif val == "file":
            await interaction.response.send_modal(FileModal(self.builder_view))

class EditComponentSelect(discord.ui.Select):
    def __init__(self, view):
        self.builder_view = view
        options = []
        for idx, comp in enumerate(view.session['components']):
            comp_type = comp['type'].replace('_', ' ').title()
            summary = get_component_summary(comp)
            if len(summary) > 50:
                summary = summary[:47] + "..."
            options.append(discord.SelectOption(
                label=f"{idx + 1}. {comp_type}",
                value=str(idx),
                description=summary,
                default=(view.selected_index == idx)
            ))
            if len(options) >= 25:
                break
        super().__init__(
            placeholder="Select a component to edit/move/delete...",
            options=options,
            row=2
        )
        
    async def callback(self, interaction: discord.Interaction):
        self.builder_view.selected_index = int(self.values[0])
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class PreviewButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="👁 Preview", style=discord.ButtonStyle.secondary, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        err = validate_components(self.builder_view.session['components'])
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        v2_view, files = await render_v2_layout(self.builder_view.user_id, self.builder_view.session['components'], self.builder_view.session['container_props'])
        
        try:
            print("Generated Component Tree (Preview):")
            print(print_v2_component_tree(v2_view))
        except Exception as e:
            logger.warning(f"Could not print component tree: {e}")
            
        try:
            validate_v2_layout(v2_view)
        except Exception as e:
            await interaction.followup.send(f"V2 Validation Error: {e}", ephemeral=True)
            return

        # In discord.py 2.7.1 Components V2 layouts cannot carry standard text content.
        if files:
            await interaction.followup.send(view=v2_view, files=files, ephemeral=True)
        else:
            await interaction.followup.send(view=v2_view, ephemeral=True)

class PublishButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="📤 Publish", style=discord.ButtonStyle.secondary, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        err = validate_components(self.builder_view.session['components'])
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        v2_view, files = await render_v2_layout(self.builder_view.user_id, self.builder_view.session['components'], self.builder_view.session['container_props'])
        
        try:
            print("Generated Component Tree (Publish):")
            print(print_v2_component_tree(v2_view))
        except Exception as e:
            logger.warning(f"Could not print component tree: {e}")
            
        try:
            validate_v2_layout(v2_view)
        except Exception as e:
            await interaction.followup.send(f"V2 Validation Error: {e}", ephemeral=True)
            return

        channel = interaction.channel
        try:
            if files:
                msg = await channel.send(view=v2_view, files=files)
            else:
                msg = await channel.send(view=v2_view)
            await save_published_to_db(self.builder_view.user_id, str(interaction.channel_id), str(msg.id), None)
            await interaction.followup.send("📤 **Successfully published container!**", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to publish container: {e}", ephemeral=True)

class UndoButton(discord.ui.Button):
    def __init__(self, view):
        has_undo = bool(view.session['undo_stack'])
        super().__init__(label="↩ Undo", style=discord.ButtonStyle.secondary, disabled=not has_undo, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        if not self.builder_view.session['undo_stack']:
            await interaction.response.send_message("❌ Nothing to undo.", ephemeral=True)
            return
            
        self.builder_view.session['redo_stack'].append(copy.deepcopy(self.builder_view.session['components']))
        self.builder_view.session['redo_props_stack'].append(copy.deepcopy(self.builder_view.session['container_props']))
        
        self.builder_view.session['components'] = self.builder_view.session['undo_stack'].pop()
        self.builder_view.session['container_props'] = self.builder_view.session['undo_props_stack'].pop()
        self.builder_view.selected_index = None
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class RedoButton(discord.ui.Button):
    def __init__(self, view):
        has_redo = bool(view.session['redo_stack'])
        super().__init__(label="↪ Redo", style=discord.ButtonStyle.secondary, disabled=not has_redo, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        if not self.builder_view.session['redo_stack']:
            await interaction.response.send_message("❌ Nothing to redo.", ephemeral=True)
            return
            
        self.builder_view.session['undo_stack'].append(copy.deepcopy(self.builder_view.session['components']))
        self.builder_view.session['undo_props_stack'].append(copy.deepcopy(self.builder_view.session['container_props']))
        
        self.builder_view.session['components'] = self.builder_view.session['redo_stack'].pop()
        self.builder_view.session['container_props'] = self.builder_view.session['redo_props_stack'].pop()
        self.builder_view.selected_index = None
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class ClearButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="🗑 Clear", style=discord.ButtonStyle.secondary, row=1)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        self.builder_view.push_undo()
        self.builder_view.session['components'].clear()
        self.builder_view.session['container_props'] = {"accent_color": "", "spoiler": False}
        self.builder_view.selected_index = None
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class MoveUpButton(discord.ui.Button):
    def __init__(self, view, disabled=False):
        super().__init__(label="⬆ Move Up", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        idx = self.builder_view.selected_index
        if idx is None or idx == 0:
            await interaction.response.send_message("❌ Cannot move up.", ephemeral=True)
            return
        self.builder_view.push_undo()
        comps = self.builder_view.session['components']
        comps[idx], comps[idx - 1] = comps[idx - 1], comps[idx]
        self.builder_view.selected_index = idx - 1
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class MoveDownButton(discord.ui.Button):
    def __init__(self, view, disabled=False):
        super().__init__(label="⬇ Move Down", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        idx = self.builder_view.selected_index
        comps = self.builder_view.session['components']
        if idx is None or idx >= len(comps) - 1:
            await interaction.response.send_message("❌ Cannot move down.", ephemeral=True)
            return
        self.builder_view.push_undo()
        comps[idx], comps[idx + 1] = comps[idx + 1], comps[idx]
        self.builder_view.selected_index = idx + 1
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class DuplicateButton(discord.ui.Button):
    def __init__(self, view, disabled=False):
        super().__init__(label="📋 Duplicate", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        idx = self.builder_view.selected_index
        comps = self.builder_view.session['components']
        if idx is None or idx < 0 or idx >= len(comps):
            await interaction.response.send_message("❌ No component selected.", ephemeral=True)
            return
        if len(comps) >= 40:
            await interaction.response.send_message("❌ Max component limit reached (40).", ephemeral=True)
            return
            
        self.builder_view.push_undo()
        dup = copy.deepcopy(comps[idx])
        comps.insert(idx + 1, dup)
        self.builder_view.selected_index = idx + 1
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class EditButton(discord.ui.Button):
    def __init__(self, view, disabled=False):
        super().__init__(label="✏ Edit", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        idx = self.builder_view.selected_index
        comps = self.builder_view.session['components']
        if idx is None or idx < 0 or idx >= len(comps):
            await interaction.response.send_message("❌ No component selected.", ephemeral=True)
            return
            
        comp = comps[idx]
        t = comp['type']
        if t == 'container':
            await interaction.response.send_modal(ContainerModal(self.builder_view, idx))
        elif t == 'text_display':
            await interaction.response.send_modal(TextDisplayModal(self.builder_view, idx))
        elif t == 'separator':
            await interaction.response.send_modal(SeparatorModal(self.builder_view, idx))
        elif t == 'section':
            await interaction.response.send_modal(SectionModal(self.builder_view, idx))
        elif t == 'media':
            await interaction.response.send_modal(MediaModal(self.builder_view, idx))
        elif t == 'action_row':
            await interaction.response.send_modal(ActionRowModal(self.builder_view, idx))
        elif t == 'file':
            await interaction.response.send_modal(FileModal(self.builder_view, idx))

class DeleteButton(discord.ui.Button):
    def __init__(self, view, disabled=False):
        super().__init__(label="❌ Delete", style=discord.ButtonStyle.secondary, disabled=disabled, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        idx = self.builder_view.selected_index
        comps = self.builder_view.session['components']
        if idx is None or idx < 0 or idx >= len(comps):
            await interaction.response.send_message("❌ No component selected.", ephemeral=True)
            return
            
        self.builder_view.push_undo()
        comps.pop(idx)
        self.builder_view.selected_index = None
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class SaveBuildButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="💾 Save Build", style=discord.ButtonStyle.secondary, row=4)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        if not self.builder_view.session['components']:
            await interaction.response.send_message("❌ No components to save.", ephemeral=True)
            return
        await interaction.response.send_modal(SaveBuildModal(self.builder_view))

class LoadBuildButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="📂 Load Build", style=discord.ButtonStyle.secondary, row=4)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        builds = await list_builds_from_db(self.builder_view.user_id)
        if not builds:
            await interaction.response.send_message("❌ You have no saved container builds.", ephemeral=True)
            return
            
        self.builder_view.clear_items()
        self.builder_view.add_item(AddComponentSelect(self.builder_view))
        self.builder_view.add_item(PreviewButton(self.builder_view))
        self.builder_view.add_item(PublishButton(self.builder_view))
        self.builder_view.add_item(UndoButton(self.builder_view))
        self.builder_view.add_item(RedoButton(self.builder_view))
        self.builder_view.add_item(ClearButton(self.builder_view))
        
        self.builder_view.add_item(LoadBuildSelect(self.builder_view, builds))
        self.builder_view.add_item(CancelLoadButton(self.builder_view))
        
        await interaction.response.edit_message(view=self.builder_view)

class LoadBuildSelect(discord.ui.Select):
    def __init__(self, builder_view, builds):
        self.builder_view = builder_view
        options = []
        for b in builds[:25]:
            options.append(discord.SelectOption(
                label=b['build_name'],
                value=b['build_name'],
                description=f"Created: {b['created_at']}"
            ))
        super().__init__(
            placeholder="Select a build to load...",
            options=options,
            row=2
        )
        
    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        build = await load_build_from_db(self.builder_view.user_id, name)
        if not build:
            await interaction.response.send_message("❌ Failed to load build data.", ephemeral=True)
            return
            
        self.builder_view.session['components'] = build['components']
        self.builder_view.session['container_props'] = build.get('container_props', {"accent_color": "", "spoiler": False})
        self.builder_view.session['undo_stack'].clear()
        self.builder_view.session['undo_props_stack'].clear()
        self.builder_view.session['redo_stack'].clear()
        self.builder_view.session['redo_props_stack'].clear()
        self.builder_view.selected_index = None
        
        await save_session_to_db(self.builder_view.user_id, self.builder_view.session)
        self.builder_view.setup_items()
        await self.builder_view.update_view(interaction)

class CancelLoadButton(discord.ui.Button):
    def __init__(self, view):
        super().__init__(label="Cancel Load", style=discord.ButtonStyle.secondary, row=3)
        self.builder_view = view
        
    async def callback(self, interaction: discord.Interaction):
        self.builder_view.setup_items()
        await interaction.response.edit_message(view=self.builder_view)

# Draft Restore Prompt UI
class DraftPromptView(discord.ui.View):
    def __init__(self, user_id: str, session: dict):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.session = session
        
    @discord.ui.button(label="Restore Draft", style=discord.ButtonStyle.secondary)
    async def restore(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.session['undo_stack'] = []
        self.session['undo_props_stack'] = []
        self.session['redo_stack'] = []
        self.session['redo_props_stack'] = []
        
        view = BuilderView(self.user_id, self.session)
        embed = make_builder_embed(self.user_id, self.session['components'], self.session['container_props'])
        tree_str = generate_tree_view(self.session['components'], self.session['container_props'])
        embed.add_field(name="🌲 Builder Layout Tree", value=f"```\n{tree_str}\n```", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = await interaction.original_response()
        self.stop()
        
    @discord.ui.button(label="Start Fresh", style=discord.ButtonStyle.secondary)
    async def fresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await delete_session_from_db(self.user_id)
        
        session = {
            "components": [],
            "container_props": {"accent_color": "", "spoiler": False},
            "undo_stack": [],
            "undo_props_stack": [],
            "redo_stack": [],
            "redo_props_stack": []
        }
        
        view = BuilderView(self.user_id, session)
        embed = make_builder_embed(self.user_id, session['components'], session['container_props'])
        tree_str = generate_tree_view(session['components'], session['container_props'])
        embed.add_field(name="🌲 Builder Layout Tree", value=f"```\n{tree_str}\n```", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = await interaction.original_response()
        self.stop()

# Embed Generator
def make_builder_embed(user_id: str, components: List[dict], container_props: dict) -> discord.Embed:
    count = len(components)
    remaining = 40 - count
    
    embed = discord.Embed(
        title="⚡ Breeze Container Builder ⚡",
        description="Build professional Discord Components V2 layouts interactively.",
        color=discord.Color.blurple()
    )
    
    accent = container_props.get('accent_color') or 'None'
    spoiler = 'Yes' if container_props.get('spoiler') else 'No'
    
    embed.add_field(
        name="📦 Root Container Config",
        value=f"• **Accent Color:** `{accent}`\n• **Spoiler:** `{spoiler}`",
        inline=False
    )
    
    comp_list_str = ""
    if not components:
        comp_list_str = "*No components added yet. Add a component to start!*"
    else:
        for idx, comp in enumerate(components, 1):
            c_type = comp['type'].replace('_', ' ').title()
            summary = get_component_summary(comp)
            comp_list_str += f"`{idx:02d}.` **{c_type}** — {summary}\n"
            
    if len(comp_list_str) > 1024:
        comp_list_str = comp_list_str[:1014] + "\n*(truncated)*"
        
    embed.add_field(
        name=f"🧱 Components ({count}/40) — {remaining} remaining",
        value=comp_list_str,
        inline=False
    )
    
    embed.set_footer(text="Breeze V2 Builder • Multi-user • Autosaved")
    return embed

# ══════════════════════════════════════════════════════════════════════
# SLASH CONTAINER COMMAND TREE GROUP (/container)
# ══════════════════════════════════════════════════════════════════════

class ContainerGroup(app_commands.Group, name="container"):
    def __init__(self, bot):
        super().__init__(description="Breeze Container commands")
        self.bot = bot
        
    @app_commands.command(name="builder", description="Open the visual Components V2 Container Builder")
    async def builder(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        
        db_session = await get_session_from_db(user_id)
        if db_session:
            view = DraftPromptView(user_id, db_session)
            embed = discord.Embed(
                title="📦 Breeze Unsaved Draft Found",
                description="You have an unsaved container draft. Would you like to restore it or start fresh?",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            session = {
                "components": [],
                "container_props": {"accent_color": "", "spoiler": False},
                "undo_stack": [],
                "undo_props_stack": [],
                "redo_stack": [],
                "redo_props_stack": []
            }
            view = BuilderView(user_id, session)
            embed = make_builder_embed(user_id, session['components'], session['container_props'])
            tree_str = generate_tree_view(session['components'], session['container_props'])
            embed.add_field(name="🌲 Builder Layout Tree", value=f"```\n{tree_str}\n```", inline=False)
            
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            view.message = await interaction.original_response()

    @app_commands.command(name="save", description="Save your active container build to the database")
    @app_commands.describe(name="Name of the build")
    async def save(self, interaction: discord.Interaction, name: str):
        user_id = str(interaction.user.id)
        session = await get_session_from_db(user_id)
        if not session or not session.get('components'):
            await interaction.response.send_message("❌ You do not have an active builder session with components to save.", ephemeral=True)
            return
            
        await save_build_to_db(user_id, name, session)
        await interaction.response.send_message(f"💾 Successfully saved build as `{name}`!", ephemeral=True)

    @app_commands.command(name="load", description="Load a saved container build into your active session")
    @app_commands.describe(name="Name of the build to load")
    async def load(self, interaction: discord.Interaction, name: str):
        user_id = str(interaction.user.id)
        build = await load_build_from_db(user_id, name)
        if not build:
            await interaction.response.send_message(f"❌ Build `{name}` not found.", ephemeral=True)
            return
            
        session = {
            "components": build["components"],
            "container_props": build.get("container_props", {"accent_color": "", "spoiler": False}),
            "undo_stack": [],
            "undo_props_stack": [],
            "redo_stack": [],
            "redo_props_stack": []
        }
        await save_session_to_db(user_id, session)
        
        view = BuilderView(user_id, session)
        embed = make_builder_embed(user_id, session['components'], session['container_props'])
        tree_str = generate_tree_view(session['components'], session['container_props'])
        embed.add_field(name="🌲 Builder Layout Tree", value=f"```\n{tree_str}\n```", inline=False)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="delete", description="Delete a saved container build from the database")
    @app_commands.describe(name="Name of the build to delete")
    async def delete(self, interaction: discord.Interaction, name: str):
        user_id = str(interaction.user.id)
        deleted = await delete_build_from_db(user_id, name)
        if deleted:
            await interaction.response.send_message(f"🗑 Successfully deleted build `{name}`.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ Saved build `{name}` not found.", ephemeral=True)

    @app_commands.command(name="list", description="List all your saved container builds")
    async def list_builds(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        builds = await list_builds_from_db(user_id)
        if not builds:
            await interaction.response.send_message("📂 You have no saved container builds.", ephemeral=True)
            return
            
        embed = discord.Embed(
            title="📂 Your Saved Container Builds",
            color=discord.Color.blue()
        )
        desc = ""
        for idx, b in enumerate(builds, 1):
            desc += f"`{idx}.` **{b['build_name']}** — Created: {b['created_at']}\n"
        embed.description = desc
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="upload", description="Upload a file for use in File components (max 8MB)")
    @app_commands.describe(attachment="The file to upload")
    async def upload(self, interaction: discord.Interaction, attachment: discord.Attachment):
        user_id = str(interaction.user.id)
        
        if attachment.size > 8 * 1024 * 1024:
            await interaction.response.send_message("❌ File exceeds the 8MB size limit. Please upload a smaller file.", ephemeral=True)
            return
            
        try:
            data = await attachment.read()
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to read file: {e}", ephemeral=True)
            return
            
        file_id = await save_attachment_to_db(user_id, attachment.filename, data)
        await interaction.response.send_message(
            f"📁 Successfully uploaded file!\n• **Name:** `{attachment.filename}`\n• **Library ID:** `{file_id}`\n• Use this ID or Name inside your **File** components.",
            ephemeral=True
        )

# ══════════════════════════════════════════════════════════════════════
# MESSAGE LOGGER & BOT LISTENERS
# ══════════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    await tickets.log_ticket_message(message)
    utilities_cog = bot.get_cog("Utilities")
    if utilities_cog:
        await utilities_cog.handle_afk_messages(message)
        await utilities_cog.handle_sticky_messages(message)
    await bot.process_commands(message)

# ══════════════════════════════════════════════════════════════════════
# MAIN BOT RUNNER
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if TOKEN == "PUT_TOKEN_HERE":
        logger.warning("Token placeholder found. Please replace TOKEN = 'PUT_TOKEN_HERE' with your real token inside bot.py to run the bot.")
    else:
        bot.run(TOKEN)
