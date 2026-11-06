"""
Server pets (Tamagotchi-style) for the casino bot.

* Every user has ONE pet that lives in its own PRIVATE THREAD, exactly like the casino rooms.
  All pet screens and notifications (card, level-ups, evolutions, feeding) appear ONLY there.
* An admin posts the adoption panel once with /pet_setup. Players press "Adopt a pet", name it,
  and the bot creates their private pet room.
* The pet never needs feeding and never dies. It only grows.
* XP sources: a chat message (+10), one finished game/match (+50), a bag of food bought in
  the shop (+500, price 2000 coins).
* The pet's look depends on its level (see STAGES). Pictures live in the "pets" folder.
* Leaderboards (admin commands, auto-refreshing like the arena ones):
  /leaderboard_pet_level and /leaderboard_coins.

Only NEW tables are created here (pets, pet_rooms, pet_lb_messages). Existing tables are untouched.
"""

import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger("pets")

BASE_DIR = Path(__file__).resolve().parent
PETS_DIR = BASE_DIR / "pets"

# ───────────────────────── Settings ─────────────────────────

XP_MESSAGE = 10          # per chat message
XP_GAME = 50             # per finished game / match (a BO7 series counts as one)
XP_FOOD = 500            # per bag of food
FOOD_PRICE = 2000        # coins, used by the shop in bot.py
FOOD_VALUE = "pet_food"  # value of the food option in the shop dropdown

# Anti-spam: a message gives XP at most once per this many seconds per user.
# Set PET_MESSAGE_COOLDOWN=0 in .env to give XP for literally every message.
MESSAGE_COOLDOWN = int(os.getenv("PET_MESSAGE_COOLDOWN", "5"))

MAX_LEVEL = 100
NAME_MAX = 20
LEADERBOARD_REFRESH_MINUTES = 30
COIN = "🪙"

# (first level, last level, XP needed to go up one level, title, emoji, picture, color)
STAGES = [
    (1, 4, 100, "Red Bird", "🐦", "stage1.jpg", discord.Color.red()),
    (5, 9, 200, "Green Bird", "🐤", "stage2.jpg", discord.Color.green()),
    (10, 29, 500, "Cyborg Bird", "🤖", "stage3.jpg", discord.Color.light_grey()),
    (30, 49, 700, "Superhero Bird", "🦸", "stage4.jpg", discord.Color.blue()),
    (50, 100, 1000, "Fluffy Bird", "🧸", "stage5.jpg", discord.Color.dark_gold()),
]

db = None
client = None


# ───────────────────────── Level maths ─────────────────────────

def stage_of(level: int) -> int:
    """Index in STAGES for a level."""
    for i, (lo, hi, *_rest) in enumerate(STAGES):
        if lo <= level <= hi:
            return i
    return len(STAGES) - 1


def xp_for_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return STAGES[stage_of(level)][2]


MAX_XP = sum(xp_for_level(lv) for lv in range(1, MAX_LEVEL))


def level_info(total_xp: int) -> tuple[int, int, int | None]:
    """Returns (level, xp inside the level, xp needed for the next level or None at max)."""
    level, left = 1, max(0, total_xp)
    while level < MAX_LEVEL:
        need = xp_for_level(level)
        if left < need:
            return level, left, need
        left -= need
        level += 1
    return MAX_LEVEL, 0, None


# ───────────────────────── Database ─────────────────────────

def init_db() -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS pets ("
        "user_id INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "total_xp INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL DEFAULT 0)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS pet_rooms ("
        "user_id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS pet_lb_messages ("
        "guild_id INTEGER NOT NULL, board TEXT NOT NULL, "
        "channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
        "PRIMARY KEY (guild_id, board))"
    )
    db.commit()


def get_pet(uid: int):
    """Returns (name, total_xp) or None."""
    row = db.execute("SELECT name, total_xp FROM pets WHERE user_id = ?", (uid,)).fetchone()
    return (row[0], row[1]) if row else None


def create_pet(uid: int, name: str) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO pets (user_id, name, total_xp, created_at) VALUES (?, ?, 0, ?)",
        (uid, name, time.time()),
    )
    db.commit()
    return cur.rowcount == 1


def rename_pet(uid: int, name: str) -> None:
    db.execute("UPDATE pets SET name = ? WHERE user_id = ?", (name, uid))
    db.commit()


def is_maxed(uid: int) -> bool:
    pet = get_pet(uid)
    return bool(pet) and pet[1] >= MAX_XP


def grant_xp(uid: int, amount: int):
    """Adds XP. Returns (old_level, new_level, new_total_xp) or None if the user has no pet."""
    pet = get_pet(uid)
    if pet is None:
        return None
    old_total = pet[1]
    new_total = min(MAX_XP, old_total + amount)
    db.execute("UPDATE pets SET total_xp = ? WHERE user_id = ?", (new_total, uid))
    db.commit()
    return level_info(old_total)[0], level_info(new_total)[0], new_total


def room_thread_id(uid: int) -> int | None:
    row = db.execute("SELECT thread_id FROM pet_rooms WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None


def room_owner(thread_id: int) -> int | None:
    row = db.execute("SELECT user_id FROM pet_rooms WHERE thread_id = ?", (thread_id,)).fetchone()
    return row[0] if row else None


def save_room(uid: int, thread_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO pet_rooms (user_id, thread_id) VALUES (?, ?)", (uid, thread_id))
    db.commit()


# ───────────────────────── Look ─────────────────────────

def progress_bar(into: int, need: int | None, size: int = 10) -> str:
    if need is None:
        return "▰" * size
    filled = int(size * into / need)
    return "▰" * filled + "▱" * (size - filled)


def image_for(stage: int):
    """Returns (discord.File | None, attachment url | None)."""
    filename = STAGES[stage][5]
    path = PETS_DIR / filename
    if not path.is_file():
        return None, None
    return discord.File(path, filename=filename), f"attachment://{filename}"


def pet_embed(uid: int):
    """Returns (embed, file_or_None) for the user's pet card."""
    name, total = get_pet(uid)
    level, into, need = level_info(total)
    stage = stage_of(level)
    _lo, _hi, _per, title, emoji, _img, color = STAGES[stage]
    e = discord.Embed(title=f"{emoji} {name}", description=f"<@{uid}>'s pet", color=color)
    e.add_field(name="Level", value=f"**{level}** / {MAX_LEVEL}", inline=True)
    e.add_field(name="Form", value=title, inline=True)
    if need is None:
        e.add_field(name="XP", value=f"{progress_bar(0, None)}\n**Max level!**", inline=False)
    else:
        e.add_field(
            name="XP",
            value=f"{progress_bar(into, need)}\n**{into}** / {need} to level {level + 1}",
            inline=False,
        )
    nxt = next((s for s in STAGES if s[0] > level), None)
    if nxt:
        e.add_field(name="Next evolution", value=f"level **{nxt[0]}** — {nxt[3]}", inline=False)
    e.set_footer(text=f"Total XP: {total:,}")
    file, url = image_for(stage)
    if url:
        e.set_image(url=url)
    return e, file


def card_kwargs(uid: int, title_prefix: str = "") -> dict:
    """Keyword arguments (embed [+ file]) for sending the pet card."""
    embed, file = pet_embed(uid)
    if title_prefix:
        embed.title = f"{title_prefix}{embed.title}"
    kwargs = {"embed": embed}
    if file:
        kwargs["file"] = file
    return kwargs


# ───────────────────────── Private room (thread) ─────────────────────────

async def room_channel(uid: int):
    """The pet's private thread, or None if it has none / it can't be reached."""
    tid = room_thread_id(uid)
    if not tid or client is None:
        return None
    try:
        return client.get_channel(tid) or await client.fetch_channel(tid)
    except discord.HTTPException:
        return None


def in_own_room(interaction: discord.Interaction) -> bool:
    ch = interaction.channel
    return isinstance(ch, discord.Thread) and room_owner(ch.id) == interaction.user.id


async def get_room(guild: discord.Guild, user: discord.abc.User, parent):
    """Finds the pet's private thread or creates it. Returns None if it can't be created here.
    May raise discord.Forbidden when the bot lacks thread permissions."""
    thread = None
    tid = room_thread_id(user.id)
    if tid:
        thread = guild.get_thread(tid)
        if thread is None:
            try:
                thread = await guild.fetch_channel(tid)
            except discord.HTTPException:
                thread = None
    if thread is not None:
        if thread.archived:
            try:
                await thread.edit(archived=False)
            except discord.HTTPException:
                pass
        try:
            await thread.add_user(user)  # in case the user left the thread
        except discord.HTTPException:
            pass
        return thread

    if isinstance(parent, discord.Thread):
        parent = parent.parent
    if not isinstance(parent, discord.TextChannel):
        return None
    thread = await parent.create_thread(
        name=f"pet-{user.display_name}"[:90],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=10080,
    )
    save_room(user.id, thread.id)
    await thread.add_user(user)
    await thread.send(
        content=f"Welcome, {user.mention}! This is your private pet room. "
                "All news about your pet's growth will appear here.",
        view=PetRoomView(),
        **card_kwargs(user.id),
    )
    return thread


async def open_room(interaction: discord.Interaction):
    """Defers the interaction and returns the user's pet thread (reports problems itself)."""
    await interaction.response.defer(ephemeral=True)
    try:
        thread = await get_room(interaction.guild, interaction.user, interaction.channel)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I can't create private threads here. Please give me these permissions: "
            "Create Private Threads, Send Messages in Threads, Manage Threads.",
            ephemeral=True,
        )
        return None
    except discord.HTTPException:
        log.exception("Could not create the pet thread")
        await interaction.followup.send("❌ I couldn't create your pet room.", ephemeral=True)
        return None
    if thread is None:
        await interaction.followup.send("❌ Use this in a normal server text channel.", ephemeral=True)
    return thread


async def send_to_room(uid: int, **kwargs):
    """Sends something to the pet's private thread only. Returns the thread or None."""
    thread = await room_channel(uid)
    if thread is None:
        return None
    try:
        await thread.send(**kwargs)
    except discord.HTTPException:
        return None
    return thread


# ───────────────────────── XP ─────────────────────────

async def announce_level_up(uid: int, old_level: int, new_level: int):
    """Level-up / evolution news. They go to the pet's private thread and nowhere else."""
    if new_level <= old_level or get_pet(uid) is None:
        return
    name = get_pet(uid)[0]
    if stage_of(new_level) != stage_of(old_level):
        await send_to_room(
            uid,
            content=f"🎉 <@{uid}>, your pet **{name}** has evolved!",
            **card_kwargs(uid, "✨ Evolution! "),
        )
    else:
        await send_to_room(uid, content=f"⬆️ <@{uid}>, **{name}** reached level **{new_level}**!")


async def post_feed_message(uid: int, old_level: int, new_level: int):
    """After the shop food purchase: tells the pet's thread about the meal. Returns the thread."""
    name = get_pet(uid)[0]
    thread = await send_to_room(
        uid,
        content=f"🍖 <@{uid}>, **{name}** ate and got **+{XP_FOOD} XP**! Level: **{new_level}**.",
    )
    await announce_level_up(uid, old_level, new_level)
    return thread


async def award_xp(uid: int, amount: int, channel=None) -> bool:
    """Gives XP to the user's pet (if they have one). `channel` is ignored: news only go to the pet room."""
    try:
        result = grant_xp(uid, amount)
    except Exception:
        log.exception("Could not give pet XP")
        return False
    if result is None:
        return False
    await announce_level_up(uid, result[0], result[1])
    return True


async def award_game(uids, channel=None) -> None:
    """One finished game (or PvP match) = +XP_GAME for each listed player."""
    for uid in uids:
        await award_xp(uid, XP_GAME, channel)


_last_message_xp: dict[int, float] = {}


async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return
    uid = message.author.id
    now = time.time()
    if MESSAGE_COOLDOWN and now - _last_message_xp.get(uid, 0) < MESSAGE_COOLDOWN:
        return
    if get_pet(uid) is None:
        return
    _last_message_xp[uid] = now
    await award_xp(uid, XP_MESSAGE)


# ───────────────────────── Screens ─────────────────────────

def clean_name(raw: str) -> str:
    return " ".join(str(raw).split())[:NAME_MAX]


class AdoptModal(discord.ui.Modal, title="Name your pet"):
    pet_name = discord.ui.TextInput(
        label="Pet name", placeholder="For example: Chirpy", min_length=1, max_length=NAME_MAX
    )

    async def on_submit(self, interaction: discord.Interaction):
        name = clean_name(self.pet_name.value)
        if not name:
            await interaction.response.send_message("❌ The name can't be empty.", ephemeral=True)
            return
        if not create_pet(interaction.user.id, name):
            await interaction.response.send_message("You already have a pet 🐦", ephemeral=True)
            return
        thread = await open_room(interaction)  # creates the private thread
        if thread is None:
            await interaction.followup.send(
                f"🥚 **{name}** was created, but I couldn't make the room. "
                "Press \"My pet\" again once I have the thread permissions.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"🥚 **{name}** was born! Your private pet room: {thread.mention}", ephemeral=True
        )


class RenameModal(discord.ui.Modal, title="Rename your pet"):
    pet_name = discord.ui.TextInput(label="Pet name", min_length=1, max_length=NAME_MAX)

    def __init__(self, current: str):
        super().__init__()
        self.pet_name.default = current

    async def on_submit(self, interaction: discord.Interaction):
        name = clean_name(self.pet_name.value)
        if not name or get_pet(interaction.user.id) is None:
            await interaction.response.send_message("❌ I couldn't rename your pet.", ephemeral=True)
            return
        rename_pet(interaction.user.id, name)
        # inside the pet room it's a normal message, anywhere else it stays private
        await interaction.response.send_message(
            f"✅ Your pet is now called **{name}**.", ephemeral=not in_own_room(interaction)
        )


async def show_pet(interaction: discord.Interaction):
    """Shows the pet card in the user's private room (like the casino screens)."""
    if get_pet(interaction.user.id) is None:
        await interaction.response.send_message(
            "You don't have a pet yet. Press \"Adopt a pet\" on the pet panel!", ephemeral=True
        )
        return
    if in_own_room(interaction):
        await interaction.response.send_message(**card_kwargs(interaction.user.id))
        return
    thread = await open_room(interaction)
    if thread is None:
        return
    await thread.send(**card_kwargs(interaction.user.id))
    await interaction.followup.send(f"🐾 Your private pet room: {thread.mention}", ephemeral=True)


class PetPanel(discord.ui.View):
    """The public panel posted by /pet_setup."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Adopt a pet", emoji="🥚", style=discord.ButtonStyle.success,
                       custom_id="pet:adopt", row=0)
    async def adopt(self, interaction: discord.Interaction, button: discord.ui.Button):
        if get_pet(interaction.user.id):
            await show_pet(interaction)  # already has one: just take them to their room
            return
        await interaction.response.send_modal(AdoptModal())

    @discord.ui.button(label="My pet", emoji="🐾", style=discord.ButtonStyle.primary,
                       custom_id="pet:view", row=0)
    async def view_pet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_pet(interaction)


class PetRoomView(discord.ui.View):
    """Buttons inside the pet's private thread."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pet card", emoji="🐾", style=discord.ButtonStyle.primary,
                       custom_id="pet_room:card")
    async def card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_pet(interaction)

    @discord.ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary,
                       custom_id="pet_room:rename")
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        pet = get_pet(interaction.user.id)
        if pet is None:
            await interaction.response.send_message("You don't have a pet yet.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal(pet[0]))


def panel_embed() -> discord.Embed:
    stages = "\n".join(f"{s[4]} **{s[3]}**: levels {s[0]}–{s[1]}" for s in STAGES)
    return discord.Embed(
        title="🐦 Server Pets",
        description=(
            "Adopt a pet, name it and watch it grow! Every pet lives in its own private room "
            "that only you can see. No feeding needed, it never dies.\n\n"
            "**How to earn XP:**\n"
            f"💬 chat message: **+{XP_MESSAGE}**\n"
            f"🎮 finished game or match: **+{XP_GAME}**\n"
            f"🍖 food from the shop ({FOOD_PRICE:,} {COIN}): **+{XP_FOOD}**\n\n"
            f"**Evolutions:**\n{stages}"
        ),
        color=discord.Color.gold(),
    )


# ───────────────────────── Leaderboards ─────────────────────────

MEDALS = ["🥇", "🥈", "🥉"]


def _footer() -> str:
    return (
        f"\n\nUpdated <t:{int(time.time())}:R> • refreshes every "
        f"{LEADERBOARD_REFRESH_MINUTES} minutes"
    )


def pet_board_embed() -> discord.Embed:
    rows = db.execute(
        "SELECT user_id, name, total_xp FROM pets ORDER BY total_xp DESC, created_at ASC LIMIT 10"
    ).fetchall()
    lines = []
    for i, (uid, name, total) in enumerate(rows):
        level = level_info(total)[0]
        place = MEDALS[i] if i < 3 else f"`#{i + 1}`"
        emoji = STAGES[stage_of(level)][4]
        lines.append(f"{place} <@{uid}> — {emoji} **{name}** • level **{level}**")
    body = "\n".join(lines) or "Nobody has a pet yet. Be the first!"
    return discord.Embed(title="🏆 🐦 Pet Level Leaderboard", description=body + _footer(),
                         color=discord.Color.gold())


def coins_board_embed() -> discord.Embed:
    rows = db.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10").fetchall()
    lines = []
    for i, (uid, coins) in enumerate(rows):
        place = MEDALS[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{place} <@{uid}> — **{coins:,}** {COIN}")
    body = "\n".join(lines) or "Nobody here yet."
    return discord.Embed(title=f"🏆 {COIN} Coins Leaderboard", description=body + _footer(),
                         color=discord.Color.gold())


BOARDS = {"pet_level": pet_board_embed, "coins": coins_board_embed}


@tasks.loop(minutes=LEADERBOARD_REFRESH_MINUTES)
async def refresh_boards():
    rows = db.execute("SELECT guild_id, board, channel_id, message_id FROM pet_lb_messages").fetchall()
    for guild_id, board, channel_id, message_id in rows:
        if board not in BOARDS:
            continue
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(embed=BOARDS[board]())
        except discord.NotFound:
            db.execute("DELETE FROM pet_lb_messages WHERE guild_id = ? AND board = ?", (guild_id, board))
            db.commit()
        except discord.HTTPException:
            log.warning("Could not refresh the %s leaderboard", board)
        except Exception:
            log.exception("Leaderboard refresh failed for %s", board)


@refresh_boards.before_loop
async def _before_refresh():
    await client.wait_until_ready()


# ───────────────────────── Registration ─────────────────────────

def register(bot: discord.Client, database) -> None:
    """Call from setup_hook BEFORE the command tree is synced."""
    global db, client
    db = database
    client = bot
    init_db()
    bot.add_view(PetPanel())     # keeps the panel buttons working after a restart
    bot.add_view(PetRoomView())  # ...and the buttons inside the pet rooms

    @bot.tree.command(name="pet_setup", description="Post the pet panel in this channel (admins)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def pet_setup(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admins only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=panel_embed(), view=PetPanel())

    @bot.tree.command(name="pet", description="Open your private pet room")
    @app_commands.guild_only()
    async def pet(interaction: discord.Interaction):
        await show_pet(interaction)

    def make_board_command(board: str, command_name: str, description: str):
        @bot.tree.command(name=command_name, description=description)
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.guild_only()
        async def board_command(interaction: discord.Interaction):
            await interaction.response.send_message("✅ Leaderboard posted.", ephemeral=True)
            message = await interaction.channel.send(embed=BOARDS[board]())
            old = db.execute(
                "SELECT channel_id, message_id FROM pet_lb_messages WHERE guild_id = ? AND board = ?",
                (interaction.guild_id, board),
            ).fetchone()
            if old:  # one live board per type: remove the previous one
                try:
                    old_channel = bot.get_channel(old[0]) or await bot.fetch_channel(old[0])
                    await (await old_channel.fetch_message(old[1])).delete()
                except discord.HTTPException:
                    pass
            db.execute(
                "INSERT OR REPLACE INTO pet_lb_messages (guild_id, board, channel_id, message_id) "
                "VALUES (?, ?, ?, ?)",
                (interaction.guild_id, board, message.channel.id, message.id),
            )
            db.commit()

    make_board_command("pet_level", "leaderboard_pet_level",
                       "Post the auto-updating pet level leaderboard (admins)")
    make_board_command("coins", "leaderboard_coins",
                       "Post the auto-updating coins leaderboard (admins)")

    if not refresh_boards.is_running():
        refresh_boards.start()
