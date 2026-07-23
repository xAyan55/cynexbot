import discord
from discord.ui import LayoutView, Container, TextDisplay, Separator, Section, ActionRow, Button, Thumbnail, MediaGallery
from discord import MediaGalleryItem, SeparatorSpacing
from typing import Optional, List

class BreezeContainerBuilder:
    """Universal Breeze UI Builder using Components V2 Layouts with auto-splitting."""
    def __init__(self, title: str, description: Optional[str] = None, accent_color: int = 3447003, thumbnail_url: Optional[str] = None):
        self.layout = LayoutView()
        self.accent_color = accent_color
        self.containers = []
        self._new_container()
        
        header_text = f"**{title}**"
        if description:
            header_text += f"\n{description}"
        self.current_container.add_item(TextDisplay(header_text))
        
        if thumbnail_url:
            self.current_container.add_item(Thumbnail(thumbnail_url))
            
        self.current_container.add_item(Separator())

    def _new_container(self):
        # Only the root container has self.accent_color. Subsequent containers have None.
        color = self.accent_color if not self.containers else None
        container = Container(accent_color=color)
        self.layout.add_item(container)
        self.containers.append(container)
        self.current_container = container

    def _ensure_space(self, items_needed: int = 1):
        # A container can have max 5 child components
        if len(self.current_container.children) + items_needed > 5:
            # If we already have 5 top-level containers in LayoutView, we can't add more.
            # Otherwise, spin up a new container.
            if len(self.layout.children) < 5:
                self._new_container()

    def add_section(self, title: str, content: str, accessory = None):
        """Adds a labeled native Section to the container."""
        self._ensure_space()
        if accessory is None:
            accessory = Thumbnail("https://upload.wikimedia.org/wikipedia/commons/c/c0/1x1.png")
        
        children = []
        if title:
            children.append(title)
        if content:
            children.append(content)
        if not children:
            children.append("\u200b")
            
        self.current_container.add_item(Section(*children, accessory=accessory))
        return self

    def add_text(self, text: str):
        """Adds a plain text display section."""
        self._ensure_space()
        self.current_container.add_item(TextDisplay(text))
        return self

    def add_separator(self):
        """Adds a separator line cleanly (avoiding duplicates or trailing before buttons)."""
        self._ensure_space()
        if len(self.current_container.children) > 0:
            last = self.current_container.children[-1]
            if not isinstance(last, Separator) and not isinstance(last, ActionRow):
                self.current_container.add_item(Separator())
        return self

    def add_buttons(self, *buttons: Button):
        """Adds an action row containing buttons."""
        self._ensure_space()
        self.current_container.add_item(ActionRow(*buttons))
        return self

    def build(self) -> LayoutView:
        # Clean up any trailing separators across all containers
        for container in self.containers:
            try:
                while len(container._children) > 0 and isinstance(container._children[-1], Separator):
                    container._children.pop()
            except Exception:
                pass
        
        # Validate using core discord components v2 validation rules
        from tickets import validate_v2_layout
        validate_v2_layout(self.layout)
        return self.layout

class BreezeSuccessContainer(BreezeContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"Success: {title}", description, accent_color=3066993) # Green

class BreezeErrorContainer(BreezeContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"Error: {title}", description, accent_color=15158332) # Red

class BreezeWarningContainer(BreezeContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"Warning: {title}", description, accent_color=15844367) # Yellow/Orange

class BreezeInfoContainer(BreezeContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"Info: {title}", description, accent_color=3447003) # Blue

class BreezePaginationContainer(LayoutView):
    """Component V2 pagination view displaying pages of content dynamically."""
    def __init__(self, title: str, pages: List[dict], user_id: int, accent_color: int = 3447003):
        super().__init__(timeout=180)
        self.title = title
        self.pages = pages
        self.user_id = user_id
        self.current_page = 0
        self.accent_color = accent_color
        self.update_layout()

    def update_layout(self):
        self.clear_items()
        
        page_data = self.pages[self.current_page]
        
        # Handle string fallbacks safely by parsing to dict
        if isinstance(page_data, str):
            page_data = {
                "title": self.title,
                "sections": [("Page Content", page_data)]
            }
            
        builder = BreezeContainerBuilder(
            title=page_data.get("title", self.title),
            description=page_data.get("description"),
            accent_color=self.accent_color
        )
        
        content_lines = []
        for sec_title, sec_desc in page_data.get("sections", []):
            content_lines.append(f"**{sec_title}**\n{sec_desc}")
            
        combined_content = "\n\n".join(content_lines)
        is_index = (self.current_page == 0)
        section_name = "Categories" if is_index else "Commands"
        builder.add_section(section_name, combined_content)
        builder.add_separator()
            
        prev_btn = Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="breeze:paginate:prev",
            disabled=(self.current_page == 0)
        )
        next_btn = Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="breeze:paginate:next",
            disabled=(self.current_page == len(self.pages) - 1)
        )
        
        async def prev_callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("You are not authorized to paginate this view.", ephemeral=True)
                return
            self.current_page -= 1
            self.update_layout()
            await interaction.response.edit_message(view=self)

        async def next_callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("You are not authorized to paginate this view.", ephemeral=True)
                return
            self.current_page += 1
            self.update_layout()
            await interaction.response.edit_message(view=self)

        prev_btn.callback = prev_callback
        next_btn.callback = next_callback
        
        builder.add_buttons(prev_btn, next_btn)
        
        layout_view = builder.build()
        for child in layout_view.children:
            self.add_item(child)
            
        from tickets import validate_v2_layout
        validate_v2_layout(self)


# Shared UI Helper Functions

def create_info_card(title: str, description: Optional[str], sections_dict: dict, thumbnail_url: Optional[str] = None, accent_color: int = 3447003) -> LayoutView:
    builder = BreezeContainerBuilder(title, description, accent_color=accent_color, thumbnail_url=thumbnail_url)
    for sec_title, sec_desc in sections_dict.items():
        builder.add_section(sec_title, sec_desc)
        builder.add_separator()
    return builder.build()

def create_success_section(title: str, message: str) -> LayoutView:
    builder = BreezeSuccessContainer(title)
    builder.add_section("Information", message)
    return builder.build()

def create_warning_section(title: str, message: str) -> LayoutView:
    builder = BreezeWarningContainer(title)
    builder.add_section("Warning Detail", message)
    return builder.build()

def create_error_section(title: str, message: str) -> LayoutView:
    builder = BreezeErrorContainer(title)
    builder.add_section("Error Detail", message)
    return builder.build()

def create_user_card(member: discord.Member, sections_dict: dict) -> LayoutView:
    builder = BreezeContainerBuilder(f"👤 User Profile", f"Details for {member.mention}", accent_color=3447003, thumbnail_url=member.display_avatar.url if member.display_avatar else None)
    for sec_title, sec_desc in sections_dict.items():
        builder.add_section(sec_title, sec_desc)
        builder.add_separator()
    return builder.build()

def create_server_card(guild: discord.Guild, sections_dict: dict) -> LayoutView:
    builder = BreezeContainerBuilder(f"🏠 Server Information", f"Detailed breakdown of **{guild.name}**", accent_color=3447003, thumbnail_url=guild.icon.url if guild.icon else None)
    for sec_title, sec_desc in sections_dict.items():
        builder.add_section(sec_title, sec_desc)
        builder.add_separator()
    return builder.build()

def create_pagination_menu(title: str, pages_data: List[dict], user_id: int, accent_color: int = 3447003) -> LayoutView:
    return BreezePaginationContainer(title, pages_data, user_id, accent_color)
