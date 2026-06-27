import discord
from discord.ui import LayoutView, Container, TextDisplay, Separator, Section, ActionRow, Button
from typing import Optional, List

class CynexCloudContainerBuilder:
    """Universal CynexCloud UI Builder using Components V2 Layouts."""
    def __init__(self, title: str, description: Optional[str] = None, accent_color: int = 3447003):
        self.layout = LayoutView()
        self.container = Container(accent_color=accent_color)
        self.layout.add_item(self.container)
        
        header_text = f"🛡 **CynexCloud** | **{title}**"
        if description:
            header_text += f"\n{description}"
        self.container.add_item(TextDisplay(header_text))

    def add_section(self, title: str, content: str, accessory = None):
        """Adds a labeled section to the container."""
        if accessory is not None:
            self.container.add_item(Section(title, content, accessory=accessory))
        else:
            self.container.add_item(TextDisplay(f"**{title}**\n{content}"))
        return self

    def add_text(self, text: str):
        """Adds a plain text display section."""
        self.container.add_item(TextDisplay(text))
        return self

    def add_separator(self):
        """Adds a separator line."""
        self.container.add_item(Separator())
        return self

    def add_buttons(self, *buttons: Button):
        """Adds an action row containing buttons."""
        self.container.add_item(ActionRow(*buttons))
        return self

    def build(self) -> LayoutView:
        return self.layout

class CynexCloudSuccessContainer(CynexCloudContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"✅ {title}", description, accent_color=3066993) # Green

class CynexCloudErrorContainer(CynexCloudContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"❌ {title}", description, accent_color=15158332) # Red

class CynexCloudWarningContainer(CynexCloudContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"⚠️ {title}", description, accent_color=15844367) # Yellow/Orange

class CynexCloudInfoContainer(CynexCloudContainerBuilder):
    def __init__(self, title: str, description: Optional[str] = None):
        super().__init__(f"ℹ️ {title}", description, accent_color=3447003) # Blue

class CynexCloudPaginationContainer(LayoutView):
    """Component V2 pagination view displaying pages of content."""
    def __init__(self, title: str, pages: List[str], user_id: int, accent_color: int = 3447003):
        super().__init__(timeout=180)
        self.title = title
        self.pages = pages
        self.user_id = user_id
        self.current_page = 0
        self.accent_color = accent_color
        self.update_layout()

    def update_layout(self):
        self.clear_items()
        container = Container(accent_color=self.accent_color)
        self.add_item(container)
        
        container.add_item(TextDisplay(
            f"🛡 **CynexCloud** | **{self.title}**\n"
            f"*Page {self.current_page + 1} of {len(self.pages)}*"
        ))
        container.add_item(Separator())
        container.add_item(TextDisplay(self.pages[self.current_page]))
        container.add_item(Separator())
        
        prev_btn = Button(
            label="◀ Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="cynexcloud:paginate:prev",
            disabled=(self.current_page == 0)
        )
        next_btn = Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id="cynexcloud:paginate:next",
            disabled=(self.current_page == len(self.pages) - 1)
        )
        
        async def prev_callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ You are not authorized to paginate this view.", ephemeral=True)
                return
            await interaction.response.defer()
            self.current_page -= 1
            self.update_layout()
            await interaction.message.edit(view=self)

        async def next_callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ You are not authorized to paginate this view.", ephemeral=True)
                return
            await interaction.response.defer()
            self.current_page += 1
            self.update_layout()
            await interaction.message.edit(view=self)

        prev_btn.callback = prev_callback
        next_btn.callback = next_callback
        
        container.add_item(ActionRow(prev_btn, next_btn))
