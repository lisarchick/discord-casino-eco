"""
Drawing canvases for the casino bot.

Flow
  * A canvas is bought in the shop (CANVAS_PRICE coins, see bot.py). There is no limit: every
    purchase creates a NEW private thread with its own blank canvas.
  * Inside the thread the player gets an editor: a cursor that moves with arrow buttons, a pen
    (on/off), an eraser, a brush size menu, a color menu (23 colors + any hex color) and undo.
    "Go to" jumps to exact coordinates and draws a straight line when the pen is down.
  * "Finish" saves the picture to the gallery. If an admin has posted the gallery panel with
    /gallery, the finished picture is also posted in that channel.
  * /gallery (admins only) posts the gallery panel: browse all drawings with Prev/Next or pick a
    person to see only their drawings.

The picture is a GRID x GRID pixel image shown enlarged. Only NEW tables are created here
(canvases, drawings, gallery_panels). Existing tables are untouched.
"""

import asyncio
import io
import logging
import time
from functools import partial

import discord
from discord import app_commands
from PIL import Image, ImageChops, ImageDraw

log = logging.getLogger("art")

CANVAS_PRICE = 10_000
CANVAS_VALUE = "art_canvas"      # value of the canvas option in the shop dropdown

GRID = 128                       # canvas is GRID x GRID pixels
SCALE = 5                        # enlargement in the editor (640 x 640)
GALLERY_SCALE = 5
BG = (255, 255, 255)
STEPS = [1, 2, 4, 8, 16, 32]     # cursor step in pixels
SIZES = [1, 2, 3, 5, 8, 12, 16, 24]
HISTORY_LIMIT = 25
ARCHIVE_AFTER = 120              # seconds before a finished canvas thread is archived

# key: (label, emoji, rgb)
COLORS = {
    "black": ("Black", "⬛", (0, 0, 0)),
    "white": ("White", "⬜", (255, 255, 255)),
    "gray": ("Gray", None, (128, 128, 128)),
    "silver": ("Silver", None, (192, 192, 192)),
    "red": ("Red", "🟥", (230, 25, 25)),
    "maroon": ("Maroon", None, (128, 0, 0)),
    "orange": ("Orange", "🟧", (255, 140, 0)),
    "yellow": ("Yellow", "🟨", (255, 220, 0)),
    "lime": ("Lime", None, (140, 230, 30)),
    "green": ("Green", "🟩", (40, 170, 60)),
    "darkgreen": ("Dark green", None, (0, 100, 40)),
    "teal": ("Teal", None, (0, 128, 128)),
    "cyan": ("Cyan", None, (0, 200, 230)),
    "sky": ("Sky blue", None, (110, 180, 255)),
    "blue": ("Blue", "🟦", (30, 90, 230)),
    "navy": ("Navy", None, (0, 0, 128)),
    "purple": ("Purple", "🟪", (130, 50, 190)),
    "magenta": ("Magenta", None, (230, 0, 200)),
    "pink": ("Pink", None, (255, 150, 190)),
    "brown": ("Brown", "🟫", (120, 70, 30)),
    "tan": ("Tan", None, (240, 200, 160)),
    "mint": ("Mint", None, (150, 240, 190)),
    "lavender": ("Lavender", None, (200, 170, 255)),
}

db = None
_tasks: set[asyncio.Task] = set()


# ───────────────────────── Database & images ─────────────────────────

def init_db() -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS canvases ("
        "thread_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, "
        "data BLOB NOT NULL, created_at REAL NOT NULL, finished INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS drawings ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, guild_id INTEGER NOT NULL, "
        "created_at REAL NOT NULL, data BLOB NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS gallery_panels ("
        "guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL)"
    )
    db.commit()


def new_image() -> Image.Image:
    return Image.new("RGB", (GRID, GRID), BG)


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def load_png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def is_blank(img: Image.Image) -> bool:
    return ImageChops.difference(img, new_image()).getbbox() is None


def parse_hex(raw: str):
    s = str(raw).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None


# ───────────────────────── Canvas session ─────────────────────────

class Canvas:
    """State of one canvas being edited. Pixels are saved to the database after every change;
    the cursor and tool settings live in memory (they reset to defaults after a bot restart)."""

    def __init__(self, thread_id: int, user_id: int, guild_id: int, img):
        self.thread_id = thread_id
        self.user_id = user_id
        self.guild_id = guild_id
        self.img = img
        self.x = self.y = GRID // 2
        self.pen = False
        self.eraser = False
        self.color = (0, 0, 0)
        self.color_key = "black"
        self.size = 3
        self.step = 4
        self.history: list[Image.Image] = []
        self.rev = 0


sessions: dict[int, Canvas] = {}


def get_session(thread_id: int):
    c = sessions.get(thread_id)
    if c is not None:
        return c
    row = db.execute(
        "SELECT user_id, guild_id, data FROM canvases WHERE thread_id = ? AND finished = 0", (thread_id,)
    ).fetchone()
    if row is None:
        return None
    c = Canvas(thread_id, row[0], row[1], load_png(row[2]))
    sessions[thread_id] = c
    return c


def save_session(c: Canvas) -> None:
    db.execute("UPDATE canvases SET data = ? WHERE thread_id = ?", (png_bytes(c.img), c.thread_id))
    db.commit()


def push_history(c: Canvas) -> None:
    c.history.append(c.img.copy())
    del c.history[:-HISTORY_LIMIT]


def paint_color(c: Canvas):
    return BG if c.eraser else c.color


def stamp(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, fill) -> None:
    if size <= 1:
        draw.point((x, y), fill=fill)
        return
    x0, y0 = x - size // 2, y - size // 2
    draw.ellipse([x0, y0, x0 + size - 1, y0 + size - 1], fill=fill)


def stroke(img: Image.Image, p0, p1, size: int, fill) -> None:
    d = ImageDraw.Draw(img)
    if p0 != p1:
        d.line([p0, p1], fill=fill, width=max(1, size))
    stamp(d, p0[0], p0[1], size, fill)
    stamp(d, p1[0], p1[1], size, fill)


def clamp(v: int) -> int:
    return max(0, min(GRID - 1, v))


def move_to(c: Canvas, nx: int, ny: int) -> None:
    """Moves the cursor; with the pen down it draws a line from the old to the new position."""
    nx, ny = clamp(nx), clamp(ny)
    if c.pen:
        push_history(c)
        stroke(c.img, (c.x, c.y), (nx, ny), c.size, paint_color(c))
        save_session(c)
    c.x, c.y = nx, ny


# ───────────────────────── Rendering ─────────────────────────

def render(c: Canvas, cursor: bool = True) -> discord.File:
    big = c.img.resize((GRID * SCALE, GRID * SCALE), Image.NEAREST)
    if cursor:
        d = ImageDraw.Draw(big)
        h = c.size // 2
        x0, y0 = (c.x - h) * SCALE, (c.y - h) * SCALE
        x1, y1 = (c.x - h + c.size) * SCALE - 1, (c.y - h + c.size) * SCALE - 1
        color = (255, 0, 0) if c.pen else (0, 120, 255)
        d.rectangle([x0, y0, x1, y1], outline=color, width=2)
        cx, cy = c.x * SCALE + SCALE // 2, c.y * SCALE + SCALE // 2
        d.line([cx - 9, cy, cx + 9, cy], fill=color, width=1)
        d.line([cx, cy - 9, cx, cy + 9], fill=color, width=1)
    c.rev += 1
    return discord.File(io.BytesIO(png_bytes(big)), filename=f"canvas_{c.rev}.png")


HELP = (
    "⬆️ Move the cursor with the arrow buttons (**Step** sets the distance).\n"
    "✏️ Turn the **Pen** on to draw while you move. 🧽 **Eraser** erases with the same brush.\n"
    "🎯 **Go to** jumps to exact coordinates (with the pen on it draws a straight line).\n"
    "🎨 Pick a color and brush size below. When you're done press **Finish**."
)


def editor_embed(c: Canvas, filename: str, note: str | None = None) -> discord.Embed:
    e = discord.Embed(title="🎨 Canvas", description=(f"**{note}**\n\n" if note else "") + HELP,
                      color=discord.Color.blurple())
    e.set_image(url=f"attachment://{filename}")
    tool = "Eraser" if c.eraser else f"Brush: {COLORS[c.color_key][0] if c.color_key in COLORS else 'custom'}"
    e.set_footer(text=(f"Cursor ({c.x}, {c.y}) • Pen {'DOWN' if c.pen else 'up'} • {tool} • "
                       f"Size {c.size}px • Step {c.step}px"))
    return e


async def redraw(interaction: discord.Interaction, c: Canvas, note: str | None = None):
    file = render(c)
    await interaction.response.edit_message(
        embed=editor_embed(c, file.filename, note), attachments=[file], view=CanvasView(c)
    )


async def own_canvas(interaction: discord.Interaction):
    """The canvas of this thread if it belongs to the user (otherwise answers and returns None)."""
    c = get_session(interaction.channel_id)
    if c is None:
        await interaction.response.send_message("This canvas is already finished.", ephemeral=True)
        return None
    if interaction.user.id != c.user_id:
        await interaction.response.send_message("❌ This is not your canvas.", ephemeral=True)
        return None
    return c


# ───────────────────────── Editor actions ─────────────────────────

async def do_move(interaction: discord.Interaction, dx: int, dy: int):
    c = await own_canvas(interaction)
    if c is None:
        return
    move_to(c, c.x + dx * c.step, c.y + dy * c.step)
    await redraw(interaction, c)


async def do_pen(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    c.pen = not c.pen
    if c.pen:  # putting the pen down leaves a dot
        push_history(c)
        stamp(ImageDraw.Draw(c.img), c.x, c.y, c.size, paint_color(c))
        save_session(c)
    await redraw(interaction, c)


async def do_eraser(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    c.eraser = not c.eraser
    await redraw(interaction, c)


async def do_step(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    c.step = STEPS[(STEPS.index(c.step) + 1) % len(STEPS)] if c.step in STEPS else STEPS[0]
    await redraw(interaction, c)


async def do_undo(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    if not c.history:
        await redraw(interaction, c, "Nothing to undo")
        return
    c.img = c.history.pop()
    save_session(c)
    await redraw(interaction, c)


async def do_clear(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    push_history(c)
    c.img = new_image()
    save_session(c)
    await redraw(interaction, c, "Canvas cleared (Undo brings it back)")


async def do_goto(interaction: discord.Interaction):
    if await own_canvas_check(interaction):
        await interaction.response.send_modal(GotoModal())


async def own_canvas_check(interaction: discord.Interaction) -> bool:
    c = get_session(interaction.channel_id)
    if c is None:
        await interaction.response.send_message("This canvas is already finished.", ephemeral=True)
        return False
    if interaction.user.id != c.user_id:
        await interaction.response.send_message("❌ This is not your canvas.", ephemeral=True)
        return False
    return True


class GotoModal(discord.ui.Modal, title="Go to coordinates"):
    x = discord.ui.TextInput(label=f"X (0-{GRID - 1}, left to right)", max_length=3)
    y = discord.ui.TextInput(label=f"Y (0-{GRID - 1}, top to bottom)", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        c = await own_canvas(interaction)
        if c is None:
            return
        try:
            nx, ny = int(str(self.x.value).strip()), int(str(self.y.value).strip())
        except ValueError:
            await interaction.response.send_message("❌ Enter whole numbers.", ephemeral=True)
            return
        move_to(c, nx, ny)
        await redraw(interaction, c)


class HexModal(discord.ui.Modal, title="Custom color"):
    value = discord.ui.TextInput(label="Hex color", placeholder="#ff8800", min_length=3, max_length=7)

    async def on_submit(self, interaction: discord.Interaction):
        c = await own_canvas(interaction)
        if c is None:
            return
        rgb = parse_hex(self.value.value)
        if rgb is None:
            await interaction.response.send_message("❌ Use a color like #ff8800.", ephemeral=True)
            return
        c.color, c.color_key, c.eraser = rgb, "custom", False
        await redraw(interaction, c, f"Color set to #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")


class ColorSelect(discord.ui.Select):
    def __init__(self, c: Canvas):
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji,
                                 default=(key == c.color_key and not c.eraser))
            for key, (label, emoji, _rgb) in COLORS.items()
        ]
        options.append(discord.SelectOption(label="Custom hex color…", value="custom", emoji="🎨"))
        super().__init__(custom_id="art:color", placeholder="🎨 Color", options=options, row=3)

    async def callback(self, interaction: discord.Interaction):
        c = await own_canvas(interaction)
        if c is None:
            return
        key = self.values[0]
        if key == "custom":
            await interaction.response.send_modal(HexModal())
            return
        c.color, c.color_key, c.eraser = COLORS[key][2], key, False
        await redraw(interaction, c)


class BrushSelect(discord.ui.Select):
    def __init__(self, c: Canvas):
        options = [
            discord.SelectOption(label=f"Brush size: {s} px", value=str(s), default=(s == c.size))
            for s in SIZES
        ]
        super().__init__(custom_id="art:brush", placeholder="🖌️ Brush size", options=options, row=4)

    async def callback(self, interaction: discord.Interaction):
        c = await own_canvas(interaction)
        if c is None:
            return
        c.size = int(self.values[0])
        await redraw(interaction, c)


class ActionButton(discord.ui.Button):
    def __init__(self, custom_id: str, handler, **kwargs):
        super().__init__(custom_id=custom_id, **kwargs)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)


class CanvasView(discord.ui.View):
    """The editor controls. Callbacks look the canvas up by thread, so the buttons keep working
    after a bot restart (one instance with c=None is registered as a persistent view)."""

    def __init__(self, c: Canvas | None = None):
        super().__init__(timeout=None)
        c = c or Canvas(0, 0, 0, None)
        S = discord.ButtonStyle
        arrow = lambda cid, label, dx, dy, row: ActionButton(
            cid, partial(do_move, dx=dx, dy=dy), label=label, style=S.secondary, row=row)
        # row 0
        self.add_item(arrow("art:ul", "↖", -1, -1, 0))
        self.add_item(arrow("art:u", "↑", 0, -1, 0))
        self.add_item(arrow("art:ur", "↗", 1, -1, 0))
        self.add_item(ActionButton("art:pen", do_pen, label=f"Pen: {'ON' if c.pen else 'OFF'}", emoji="✏️",
                                   style=S.success if c.pen else S.secondary, row=0))
        self.add_item(ActionButton("art:eraser", do_eraser, label="Eraser", emoji="🧽",
                                   style=S.danger if c.eraser else S.secondary, row=0))
        # row 1
        self.add_item(arrow("art:l", "←", -1, 0, 1))
        self.add_item(ActionButton("art:goto", do_goto, label="Go to", emoji="🎯", style=S.primary, row=1))
        self.add_item(arrow("art:r", "→", 1, 0, 1))
        self.add_item(ActionButton("art:step", do_step, label=f"Step: {c.step}", emoji="👣",
                                   style=S.secondary, row=1))
        self.add_item(ActionButton("art:undo", do_undo, label="Undo", emoji="↩️", style=S.secondary, row=1))
        # row 2
        self.add_item(arrow("art:dl", "↙", -1, 1, 2))
        self.add_item(arrow("art:d", "↓", 0, 1, 2))
        self.add_item(arrow("art:dr", "↘", 1, 1, 2))
        self.add_item(ActionButton("art:clear", do_clear, label="Clear", emoji="🗑️", style=S.danger, row=2))
        self.add_item(ActionButton("art:finish", do_finish, label="Finish", emoji="✅", style=S.success, row=2))
        # rows 3-4
        self.add_item(ColorSelect(c))
        self.add_item(BrushSelect(c))


# ───────────────────────── Finish & gallery feed ─────────────────────────

async def archive_later(thread: discord.Thread, delay: int = ARCHIVE_AFTER):
    await asyncio.sleep(delay)
    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass


def drawing_file(data: bytes, filename: str, scale: int = GALLERY_SCALE) -> discord.File:
    img = load_png(data).resize((GRID * scale, GRID * scale), Image.NEAREST)
    return discord.File(io.BytesIO(png_bytes(img)), filename=filename)


async def do_finish(interaction: discord.Interaction):
    c = await own_canvas(interaction)
    if c is None:
        return
    await interaction.response.send_message(
        "Finish this drawing and save it to the gallery? The canvas will be closed.",
        view=ConfirmFinish(c.thread_id, interaction.message),
        ephemeral=True,
    )


class ConfirmFinish(discord.ui.View):
    def __init__(self, thread_id: int, editor_message):
        super().__init__(timeout=120)
        self.thread_id = thread_id
        self.editor_message = editor_message

    @discord.ui.button(label="Save to gallery", emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        c = get_session(self.thread_id)
        if c is None or interaction.user.id != c.user_id:
            await interaction.response.edit_message(content="This canvas is already finished.", view=None)
            return
        if is_blank(c.img):
            await interaction.response.edit_message(content="❌ The canvas is empty. Draw something first!", view=None)
            return
        data = png_bytes(c.img)
        cur = db.execute(
            "INSERT INTO drawings (user_id, guild_id, created_at, data) VALUES (?, ?, ?, ?)",
            (c.user_id, c.guild_id, time.time(), data),
        )
        db.execute("UPDATE canvases SET finished = 1, data = ? WHERE thread_id = ?", (data, c.thread_id))
        db.commit()
        sessions.pop(c.thread_id, None)
        self.stop()
        await interaction.response.edit_message(content="✅ Saved to the gallery!", view=None)

        file = render(c, cursor=False)
        e = discord.Embed(title="🖼️ Finished drawing", description=f"By <@{c.user_id}>", color=discord.Color.green())
        e.set_image(url=f"attachment://{file.filename}")
        try:
            await self.editor_message.edit(embed=e, attachments=[file], view=None)
        except discord.HTTPException:
            pass
        await post_to_gallery(interaction, c.guild_id, cur.lastrowid, data)
        if isinstance(interaction.channel, discord.Thread):
            task = asyncio.create_task(archive_later(interaction.channel))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)

    @discord.ui.button(label="Keep drawing", emoji="🖌️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Okay, keep drawing!", view=None)


async def post_to_gallery(interaction: discord.Interaction, guild_id: int, drawing_id: int, data: bytes):
    """Posts a finished drawing in the gallery channel (if an admin set it up with /gallery)."""
    row = db.execute("SELECT channel_id FROM gallery_panels WHERE guild_id = ?", (guild_id,)).fetchone()
    if row is None:
        return
    try:
        channel = interaction.client.get_channel(row[0]) or await interaction.client.fetch_channel(row[0])
        file = drawing_file(data, f"drawing_{drawing_id}.png")
        e = discord.Embed(title="🎨 New drawing", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        e.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        e.set_image(url=f"attachment://{file.filename}")
        await channel.send(embed=e, file=file, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.warning("Could not post the drawing to the gallery channel")


# ───────────────────────── Gallery viewer ─────────────────────────

def list_drawings(guild_id: int, user_id: int | None = None):
    if user_id is None:
        return db.execute(
            "SELECT id, user_id, created_at FROM drawings WHERE guild_id = ? ORDER BY id DESC", (guild_id,)
        ).fetchall()
    return db.execute(
        "SELECT id, user_id, created_at FROM drawings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
        (guild_id, user_id),
    ).fetchall()


class PersonSelect(discord.ui.UserSelect):
    def __init__(self, custom_id: str | None, row: int, handler=None):
        kwargs = {"custom_id": custom_id} if custom_id else {}
        super().__init__(placeholder="👤 Choose a person…", min_values=1, max_values=1, row=row, **kwargs)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction, self.values[0])


class GalleryViewer(discord.ui.View):
    """Personal (private) gallery browser: Prev/Next and a person filter."""

    def __init__(self, guild_id: int, user_id: int | None = None):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.filter_user = user_id
        self.index = 0
        self.items = list_drawings(guild_id, user_id)
        self.add_item(PersonSelect(None, 1, self.pick_person))
        self.sync()

    def sync(self):
        last = len(self.items) - 1
        self.first.disabled = self.prev.disabled = self.index <= 0
        self.next.disabled = self.last.disabled = self.index >= last

    def build(self):
        """Returns (embed, files list)."""
        who = f" by <@{self.filter_user}>" if self.filter_user else ""
        if not self.items:
            return discord.Embed(title="🎨 Gallery", description=f"No drawings yet{who}.",
                                 color=discord.Color.gold()), []
        did, uid, created = self.items[self.index]
        data = db.execute("SELECT data FROM drawings WHERE id = ?", (did,)).fetchone()[0]
        file = drawing_file(data, f"drawing_{did}.png")
        e = discord.Embed(title="🎨 Gallery", description=f"By <@{uid}> • <t:{int(created)}:D>",
                          color=discord.Color.gold())
        e.set_image(url=f"attachment://{file.filename}")
        e.set_footer(text=f"Drawing {self.index + 1} of {len(self.items)}"
                          + (" • filtered by person" if self.filter_user else " • all drawings"))
        return e, [file]

    async def show(self, interaction: discord.Interaction, first: bool = False):
        self.sync()
        embed, files = self.build()
        if first:
            await interaction.response.send_message(embed=embed, files=files, view=self, ephemeral=True)
        else:
            await interaction.response.edit_message(embed=embed, attachments=files, view=self)

    async def pick_person(self, interaction: discord.Interaction, user):
        self.filter_user = user.id
        self.items = list_drawings(self.guild_id, user.id)
        self.index = 0
        await self.show(interaction)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self.show(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self.show(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.items) - 1, self.index + 1)
        await self.show(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, len(self.items) - 1)
        await self.show(interaction)

    @discord.ui.button(label="Everyone", emoji="🌍", style=discord.ButtonStyle.secondary, row=0)
    async def everyone(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.filter_user = None
        self.items = list_drawings(self.guild_id)
        self.index = 0
        await self.show(interaction)


async def open_viewer(interaction: discord.Interaction, user=None):
    viewer = GalleryViewer(interaction.guild_id, user.id if user else None)
    await viewer.show(interaction, first=True)


class GalleryPanel(discord.ui.View):
    """The public gallery panel posted by /gallery."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PersonSelect("gallery:user", 1, open_viewer))

    @discord.ui.button(label="Browse all drawings", emoji="🖼️", style=discord.ButtonStyle.primary,
                       custom_id="gallery:browse", row=0)
    async def browse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_viewer(interaction)


def gallery_embed() -> discord.Embed:
    return discord.Embed(
        title="🎨 Drawing Gallery",
        description=(
            "Finished drawings from the server's canvases appear here.\n\n"
            "🖼️ **Browse all drawings** and flip through them with the arrows\n"
            "👤 or **choose a person** below to see only their drawings"
        ),
        color=discord.Color.gold(),
    )


# ───────────────────────── Buying a canvas ─────────────────────────

async def create_canvas(interaction: discord.Interaction) -> discord.Thread:
    """Creates the private canvas thread with a blank editor. Call it AFTER the payment;
    it raises (discord.Forbidden / HTTPException / TypeError) if the thread can't be made,
    so the caller can refund."""
    user = interaction.user
    parent = interaction.channel
    if isinstance(parent, discord.Thread):
        parent = parent.parent
    if not isinstance(parent, discord.TextChannel):
        raise TypeError("canvases can only be created from a normal text channel")
    number = db.execute("SELECT COUNT(*) FROM canvases WHERE user_id = ?", (user.id,)).fetchone()[0] + 1
    thread = await parent.create_thread(
        name=f"canvas-{user.display_name}-{number}"[:90],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=10080,
    )
    try:
        await thread.add_user(user)
        c = Canvas(thread.id, user.id, interaction.guild_id, new_image())
        db.execute(
            "INSERT INTO canvases (thread_id, user_id, guild_id, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (thread.id, user.id, interaction.guild_id, png_bytes(c.img), time.time()),
        )
        db.commit()
        sessions[thread.id] = c
        file = render(c)
        await thread.send(
            content=f"{user.mention}, this is your canvas! Draw something and press **Finish** when you're done.",
            embed=editor_embed(c, file.filename),
            file=file,
            view=CanvasView(c),
        )
    except Exception:
        sessions.pop(thread.id, None)
        try:
            await thread.delete()
        except discord.HTTPException:
            pass
        raise
    return thread


# ───────────────────────── Registration ─────────────────────────

def register(bot: discord.Client, database) -> None:
    """Call from setup_hook BEFORE the command tree is synced."""
    global db
    db = database
    init_db()
    bot.add_view(CanvasView())    # keeps the editor buttons working after a restart
    bot.add_view(GalleryPanel())  # ...and the gallery panel

    @bot.tree.command(name="gallery", description="Post the drawing gallery panel in this channel (admins)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def gallery(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=gallery_embed(), view=GalleryPanel())
        message = await interaction.original_response()
        old = db.execute(
            "SELECT channel_id, message_id FROM gallery_panels WHERE guild_id = ?", (interaction.guild_id,)
        ).fetchone()
        if old and old[1] != message.id:  # one gallery panel per server: remove the previous one
            try:
                old_channel = bot.get_channel(old[0]) or await bot.fetch_channel(old[0])
                await (await old_channel.fetch_message(old[1])).delete()
            except discord.HTTPException:
                pass
        db.execute(
            "INSERT OR REPLACE INTO gallery_panels (guild_id, channel_id, message_id) VALUES (?, ?, ?)",
            (interaction.guild_id, message.channel.id, message.id),
        )
        db.commit()
