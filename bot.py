"""
Joke casino bot for Discord (virtual coins only, no real money).
Games: Slots, Mines, Rocket, Blackjack. Plus a profile, free coins every hour
and sending coins to other players.
Every user plays in their own private thread, so chat histories never mix.
"""

import asyncio
import logging
import math
import os
import random
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

import arena
import art
import pets

# Everything (.env, casino.db, pet pictures) is looked up next to this file, so the bot
# behaves the same no matter which folder the terminal was opened in.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip().strip('"').strip("'")
GUILD_ID = os.getenv("GUILD_ID", "").strip()
START_BALANCE = int(os.getenv("START_BALANCE", "1000"))
CLAIM_AMOUNT = int(os.getenv("CLAIM_AMOUNT", "100"))
CLAIM_COOLDOWN = int(os.getenv("CLAIM_COOLDOWN", "3600"))  # seconds
DB_PATH = os.getenv("DB_PATH") or "casino.db"
if not os.path.isabs(DB_PATH):
    DB_PATH = str(BASE_DIR / DB_PATH)
def _parse_int_env(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.isdigit() else default


def _parse_role_shop(raw: str) -> dict[int, int]:
    """Format: ROLE_SHOP=<roleId>:<price>,<roleId>:<price>,... (no spaces needed)."""
    shop: dict[int, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        role_id, _, price = part.partition(":")
        role_id, price = role_id.strip(), price.strip()
        if role_id.isdigit() and price.isdigit():
            shop[int(role_id)] = int(price)
    return shop


# Role handed out automatically to every new member (0 = disabled).
AUTO_JOIN_ROLE_ID = _parse_int_env("AUTO_JOIN_ROLE_ID", 0)
# Roles players can buy with coins, set in .env.
ROLE_SHOP = _parse_role_shop(os.getenv("ROLE_SHOP", ""))

COIN = "🪙"
GOLD = discord.Color.gold()
GREEN = discord.Color.green()
RED = discord.Color.red()
BLURPLE = discord.Color.blurple()

# ───────────────────────── Database ─────────────────────────

db = sqlite3.connect(DB_PATH)
db.execute(
    "CREATE TABLE IF NOT EXISTS users ("
    "user_id INTEGER PRIMARY KEY, "
    "coins INTEGER NOT NULL, "
    "last_claim REAL NOT NULL DEFAULT 0)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS rooms ("
    "user_id INTEGER PRIMARY KEY, "
    "thread_id INTEGER NOT NULL)"
)
db.commit()


def room_thread_id(uid: int) -> int | None:
    row = db.execute("SELECT thread_id FROM rooms WHERE user_id = ?", (uid,)).fetchone()
    return row[0] if row else None


def room_owner(thread_id: int) -> int | None:
    row = db.execute("SELECT user_id FROM rooms WHERE thread_id = ?", (thread_id,)).fetchone()
    return row[0] if row else None


def save_room(uid: int, thread_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO rooms (user_id, thread_id) VALUES (?, ?)", (uid, thread_id))
    db.commit()


def fmt(n: int) -> str:
    return f"{n:,}"


def ensure_user(uid: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, coins, last_claim) VALUES (?, ?, 0)",
        (uid, START_BALANCE),
    )
    db.commit()


def get_balance(uid: int) -> int:
    ensure_user(uid)
    return db.execute("SELECT coins FROM users WHERE user_id = ?", (uid,)).fetchone()[0]


def add_coins(uid: int, amount: int) -> None:
    ensure_user(uid)
    db.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, uid))
    db.commit()


def take_coins(uid: int, amount: int) -> bool:
    if amount <= 0 or get_balance(uid) < amount:
        return False
    add_coins(uid, -amount)
    return True


def try_claim(uid: int) -> tuple[bool, int]:
    """Returns (success, seconds left to wait)."""
    ensure_user(uid)
    last = db.execute("SELECT last_claim FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    now = time.time()
    left = CLAIM_COOLDOWN - (now - last)
    if left > 0:
        return False, int(left)
    db.execute(
        "UPDATE users SET coins = coins + ?, last_claim = ? WHERE user_id = ?",
        (CLAIM_AMOUNT, now, uid),
    )
    db.commit()
    return True, 0


def fmt_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m} min {s} sec" if m else f"{s} sec"


# ───────────────────────── Bet selection ─────────────────────────

async def take_bet(interaction: discord.Interaction, bet: int) -> bool:
    """Takes the bet. If there are not enough coins, says so and returns False."""
    if not take_coins(interaction.user.id, bet):
        bal = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"❌ Not enough coins. You have {fmt(bal)} {COIN}.", ephemeral=True
        )
        return False
    return True


class BetModal(discord.ui.Modal):
    def __init__(self, on_bet, title: str = "Custom bet",
                 label: str = "How many coins do you want to bet?"):
        super().__init__(title=title)
        self.on_bet = on_bet
        self.amount = discord.ui.TextInput(
            label=label, placeholder="For example: 250", max_length=12
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.amount.value).strip().replace(" ", "").replace(",", "")
        if not raw.isdigit() or int(raw) <= 0:
            await interaction.response.send_message(
                "❌ Enter a whole number greater than 0.", ephemeral=True
            )
            return
        await self.on_bet(interaction, int(raw))


class BetButton(discord.ui.Button):
    def __init__(self, amount: int):
        super().__init__(label=fmt(amount), emoji=COIN, style=discord.ButtonStyle.secondary, row=0)
        self.amount = amount

    async def callback(self, interaction: discord.Interaction):
        await self.view.on_bet(interaction, self.amount)


class BetView(discord.ui.View):
    """Bet buttons. on_bet(interaction, bet) is called after a bet is chosen."""

    def __init__(self, on_bet, noun: str = "bet", allow_all_in: bool = True,
                 prompt: str = "How many coins do you want to bet?"):
        super().__init__(timeout=300)
        self.on_bet = on_bet
        self.noun = noun
        self.prompt = prompt
        for amt in (10, 50, 100, 500):
            self.add_item(BetButton(amt))
        self.custom.label = f"Custom {noun}"
        if not allow_all_in:
            self.remove_item(self.all_in)

    @discord.ui.button(label="Custom bet", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            BetModal(self.on_bet, title=f"Custom {self.noun}", label=self.prompt)
        )

    @discord.ui.button(label="All in", emoji="💰", style=discord.ButtonStyle.danger, row=1)
    async def all_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        bal = get_balance(interaction.user.id)
        if bal <= 0:
            await interaction.response.send_message("❌ You have no coins.", ephemeral=True)
            return
        await self.on_bet(interaction, bal)


def bet_embed(title: str, text: str, uid: int) -> discord.Embed:
    e = discord.Embed(title=title, description=text, color=BLURPLE)
    e.set_footer(text=f"Balance: {fmt(get_balance(uid))} {COIN}")
    return e


# ───────────────────────── Slots ─────────────────────────

SYMBOLS = ["🍒", "🍋", "🍉", "🔔", "⭐", "💎", "7️⃣"]
WEIGHTS = [30, 25, 20, 12, 7, 4, 2]
TRIPLE = {"🍒": 3, "🍋": 4, "🍉": 6, "🔔": 10, "⭐": 20, "💎": 50, "7️⃣": 100}
PAIR_MULT = 1.5

SLOTS_RULES = (
    "Three of a kind:\n"
    + "  ".join(f"{s} x{m}" for s, m in TRIPLE.items())
    + f"\nTwo of a kind: x{PAIR_MULT}\n\nChoose your bet:"
)


def slots_embed(bet, reels, title, text="", color=GOLD, uid=None) -> discord.Embed:
    e = discord.Embed(title=title, color=color)
    e.description = f"# {'  '.join(reels)}\n{text}"
    e.add_field(name="Bet", value=f"{fmt(bet)} {COIN}")
    if uid is not None:
        e.set_footer(text=f"Balance: {fmt(get_balance(uid))} {COIN}")
    return e


def slots_result(reels: list[str]) -> float:
    counts = {s: reels.count(s) for s in set(reels)}
    top_symbol, top_count = max(counts.items(), key=lambda x: x[1])
    if top_count == 3:
        return TRIPLE[top_symbol]
    if top_count == 2:
        return PAIR_MULT
    return 0


async def run_slots(interaction: discord.Interaction, bet: int):
    uid = interaction.user.id
    rnd = lambda: random.choices(SYMBOLS, WEIGHTS)[0]
    final = random.choices(SYMBOLS, WEIGHTS, k=3)

    await interaction.response.edit_message(
        embed=slots_embed(bet, ["🎰"] * 3, "🎰 Spinning..."), view=None
    )
    frames = [
        [rnd(), rnd(), rnd()],
        [rnd(), rnd(), rnd()],
        [final[0], rnd(), rnd()],
        [final[0], final[1], rnd()],
    ]
    for frame in frames:
        await asyncio.sleep(0.7)
        await interaction.edit_original_response(
            embed=slots_embed(bet, frame, "🎰 Spinning...")
        )
    await asyncio.sleep(0.7)

    mult = slots_result(final)
    payout = int(bet * mult)
    if payout:
        add_coins(uid, payout)

    if mult >= 10:
        title, color = "🎉 JACKPOT!", GOLD
    elif payout > bet:
        title, color = "✅ You won!", GREEN
    elif payout:
        title, color = "😐 Bet returned", BLURPLE
    else:
        title, color = "❌ No luck", RED

    if payout:
        text = f"Multiplier **x{mult}**\nWon: **{fmt(payout)}** {COIN}"
    else:
        text = f"Lost: **{fmt(bet)}** {COIN}"

    await interaction.edit_original_response(
        embed=slots_embed(bet, final, title, text, color, uid),
        view=SlotsAgainView(bet),
    )
    await pets.award_game([uid], interaction.channel)  # pet XP: one finished game


async def slots_bet(interaction: discord.Interaction, bet: int):
    if not await take_bet(interaction, bet):
        return
    await run_slots(interaction, bet)


class SlotsAgainView(discord.ui.View):
    def __init__(self, bet: int):
        super().__init__(timeout=300)
        self.bet = bet
        self.again.label = f"Spin again ({fmt(bet)})"

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await slots_bet(interaction, self.bet)

    @discord.ui.button(label="Change bet", emoji="💰", style=discord.ButtonStyle.secondary)
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=bet_embed("🎰 Slots", SLOTS_RULES, interaction.user.id),
            view=BetView(slots_bet),
        )


# ───────────────────────── Mines ─────────────────────────

ROWS, COLS = 4, 5
N = ROWS * COLS  # 20 cells (the 5th button row is used by "Cash out")
HOUSE_EDGE = 0.97


def mines_multiplier(mines: int, opened: int) -> float:
    if opened == 0:
        return 1.0
    m = HOUSE_EDGE
    for i in range(opened):
        m *= (N - i) / (N - mines - i)
    return m


class MineCell(discord.ui.Button):
    def __init__(self, idx: int):
        super().__init__(style=discord.ButtonStyle.secondary, emoji="❔", row=idx // COLS)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        v: MinesView = self.view
        if v.finished:
            await interaction.response.defer()
            return

        if self.idx in v.mine_cells:
            v.finished = True
            v.reveal(hit=self.idx)
            v.stop()
            await interaction.response.edit_message(embed=v.embed("lose"), view=v)
            await pets.award_game([v.uid], interaction.channel)  # pet XP: one finished game
            return

        v.opened.add(self.idx)
        self.style = discord.ButtonStyle.success
        self.emoji = "💎"
        self.disabled = True

        if len(v.opened) == N - v.mines:  # all safe cells opened
            await v.cash_out(interaction)
            return

        v.update_cashout()
        await interaction.response.edit_message(embed=v.embed(), view=v)


class CashoutButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Cash out", emoji="💸", style=discord.ButtonStyle.success, row=4, disabled=True
        )

    async def callback(self, interaction: discord.Interaction):
        v: MinesView = self.view
        if v.finished:
            await interaction.response.defer()
            return
        await v.cash_out(interaction)


class MinesView(discord.ui.View):
    def __init__(self, origin: discord.Interaction, bet: int, mines: int):
        super().__init__(timeout=120)
        self.origin = origin
        self.uid = origin.user.id
        self.bet = bet
        self.mines = mines
        self.mine_cells = set(random.sample(range(N), mines))
        self.opened: set[int] = set()
        self.finished = False
        for i in range(N):
            self.add_item(MineCell(i))
        self.cashout = CashoutButton()
        self.add_item(self.cashout)

    def current_payout(self) -> int:
        return int(self.bet * mines_multiplier(self.mines, len(self.opened)))

    def update_cashout(self):
        self.cashout.disabled = len(self.opened) == 0
        self.cashout.label = f"Cash out {fmt(self.current_payout())}"

    def reveal(self, hit: int | None = None):
        for item in self.children:
            item.disabled = True
            if isinstance(item, MineCell):
                if item.idx in self.mine_cells:
                    item.style = discord.ButtonStyle.danger
                    item.emoji = "💥" if item.idx == hit else "💣"
                elif item.idx not in self.opened:
                    item.style = discord.ButtonStyle.secondary
                    item.emoji = "💎"

    async def cash_out(self, interaction: discord.Interaction | None):
        self.finished = True
        payout = self.current_payout()
        add_coins(self.uid, payout)
        played = len(self.opened) > 0  # an untouched board that timed out is not a game
        self.reveal()
        self.stop()
        embed = self.embed("win")
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await self.origin.edit_original_response(embed=embed, view=self)
        if played:
            await pets.award_game([self.uid], self.origin.channel)  # pet XP: one finished game

    async def on_timeout(self):
        if self.finished:
            return
        try:
            await self.cash_out(None)  # time ran out, cash out automatically
        except discord.HTTPException:
            pass

    def embed(self, state: str = "play") -> discord.Embed:
        opened = len(self.opened)
        mult = mines_multiplier(self.mines, opened)
        payout = int(self.bet * mult)

        if state == "lose":
            e = discord.Embed(
                title="💥 BOOM! You hit a mine",
                description=f"Lost: **{fmt(self.bet)}** {COIN}",
                color=RED,
            )
        elif state == "win":
            e = discord.Embed(
                title="✅ You cashed out!",
                description=(
                    f"Multiplier **x{mult:.2f}**\n"
                    f"Won: **{fmt(payout)}** {COIN} "
                    f"({'+' if payout >= self.bet else ''}{fmt(payout - self.bet)})"
                ),
                color=GREEN,
            )
        else:
            text = (
                f"Bet: **{fmt(self.bet)}** {COIN}  |  Mines: **{self.mines}**\n"
                f"Now: **x{mult:.2f}** → **{fmt(payout)}** {COIN}\n"
            )
            if opened < N - self.mines:
                nxt = mines_multiplier(self.mines, opened + 1)
                text += f"Next cell: x{nxt:.2f}"
            e = discord.Embed(title="💣 Mines", description=text, color=GOLD)
        e.set_footer(text=f"Balance: {fmt(get_balance(self.uid))} {COIN}")
        return e


class MinesSelectView(discord.ui.View):
    """Choose the number of mines."""

    def __init__(self):
        super().__init__(timeout=300)
        for m in (1, 3, 5, 10):
            self.add_item(MinesCountButton(m))


class MinesCountButton(discord.ui.Button):
    def __init__(self, mines: int):
        super().__init__(label=f"{mines} {'mine' if mines == 1 else 'mines'}", emoji="💣",
                         style=discord.ButtonStyle.secondary)
        self.mines = mines

    async def callback(self, interaction: discord.Interaction):
        mines = self.mines

        async def on_bet(i: discord.Interaction, bet: int):
            if not await take_bet(i, bet):
                return
            view = MinesView(i, bet, mines)
            await i.response.edit_message(embed=view.embed(), view=view)

        embed = bet_embed(
            f"💣 Mines ({mines})",
            f"Mines on the field: **{mines}** out of {N} cells.\nChoose your bet:",
            interaction.user.id,
        )
        await interaction.response.edit_message(embed=embed, view=BetView(on_bet))


# ───────────────────────── Blackjack ─────────────────────────

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]

BLACKJACK_RULES = (
    "Beat the dealer without going over 21.\n"
    "Blackjack pays 3:2. Dealer stands on 17.\n"
    "Double Down: double your bet and get exactly one more card.\n\n"
    "Choose your bet:"
)


def hand_value(hand: list[tuple[str, str]]) -> int:
    total = 0
    aces = 0
    for rank, _ in hand:
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def fmt_hand(hand: list[tuple[str, str]], hide_hole: bool = False) -> str:
    cards = [f"`{r}{su}`" for r, su in hand]
    if hide_hole:
        cards[1:] = ["`??`"] * (len(cards) - 1)
    return " ".join(cards)


class BlackjackView(discord.ui.View):
    def __init__(self, origin: discord.Interaction, bet: int):
        super().__init__(timeout=120)
        self.origin = origin
        self.uid = origin.user.id
        self.base_bet = bet
        self.bet = bet
        self.deck = [(r, su) for r in RANKS for su in SUITS]
        random.shuffle(self.deck)
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.player_bj = hand_value(self.player) == 21
        self.dealer_bj = hand_value(self.dealer) == 21
        self.finished = False

    def outcome(self) -> tuple[int, str, discord.Color]:
        pv, dv = hand_value(self.player), hand_value(self.dealer)
        if self.player_bj and self.dealer_bj:
            return self.bet, "🤝 Push: both have Blackjack", BLURPLE
        if self.player_bj:
            return int(self.bet * 2.5), "🎉 Blackjack! Pays 3:2", GOLD
        if self.dealer_bj:
            return 0, "❌ Dealer has Blackjack", RED
        if pv > 21:
            return 0, "💥 Bust! You went over 21", RED
        if dv > 21:
            return self.bet * 2, "✅ Dealer busts, you win!", GREEN
        if pv > dv:
            return self.bet * 2, "✅ You win!", GREEN
        if pv == dv:
            return self.bet, "🤝 Push: your bet is returned", BLURPLE
        return 0, "❌ Dealer wins", RED

    def embed(self, result_text: str = "", color=GOLD) -> discord.Embed:
        hide = not self.finished
        dealer_total = "?" if hide else hand_value(self.dealer)
        desc = (
            f"**Dealer** ({dealer_total})\n{fmt_hand(self.dealer, hide)}\n\n"
            f"**You** ({hand_value(self.player)})\n{fmt_hand(self.player)}"
        )
        if result_text:
            desc += f"\n\n{result_text}"
        e = discord.Embed(title="🃏 Blackjack", description=desc, color=color)
        e.add_field(name="Bet", value=f"{fmt(self.bet)} {COIN}")
        e.set_footer(text=f"Balance: {fmt(get_balance(self.uid))} {COIN}")
        return e

    async def settle(self, interaction: discord.Interaction | None):
        """Dealer plays, the result is paid out and the round ends."""
        self.finished = True
        if not (self.player_bj or self.dealer_bj or hand_value(self.player) > 21):
            while hand_value(self.dealer) < 17:
                self.dealer.append(self.deck.pop())
        payout, title, color = self.outcome()
        if payout:
            add_coins(self.uid, payout)
        if payout > 0:
            text = f"{title}\nPayout: **{fmt(payout)}** {COIN}"
        else:
            text = f"{title}\nLost: **{fmt(self.bet)}** {COIN}"
        self.stop()
        embed = self.embed(text, color)
        view = BlackjackAgainView(self.base_bet)
        if interaction is not None:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await self.origin.edit_original_response(embed=embed, view=view)
        await pets.award_game([self.uid], self.origin.channel)  # pet XP: one finished game

    async def on_timeout(self):
        if self.finished:
            return
        try:
            await self.settle(None)  # time ran out, stand automatically
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.defer()
            return
        self.player.append(self.deck.pop())
        self.double.disabled = True
        if hand_value(self.player) >= 21:
            await self.settle(interaction)
            return
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.defer()
            return
        await self.settle(interaction)

    @discord.ui.button(label="Double Down", emoji="✖️", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.finished:
            await interaction.response.defer()
            return
        if not take_coins(self.uid, self.bet):
            await interaction.response.send_message(
                f"❌ Not enough coins to double. You have {fmt(get_balance(self.uid))} {COIN}.",
                ephemeral=True,
            )
            return
        self.bet *= 2
        self.player.append(self.deck.pop())
        await self.settle(interaction)


class BlackjackAgainView(discord.ui.View):
    def __init__(self, bet: int):
        super().__init__(timeout=300)
        self.bet = bet
        self.again.label = f"Deal again ({fmt(bet)})"

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await blackjack_bet(interaction, self.bet)

    @discord.ui.button(label="Change bet", emoji="💰", style=discord.ButtonStyle.secondary)
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=bet_embed("🃏 Blackjack", BLACKJACK_RULES, interaction.user.id),
            view=BetView(blackjack_bet),
        )


async def blackjack_bet(interaction: discord.Interaction, bet: int):
    if not await take_bet(interaction, bet):
        return
    view = BlackjackView(interaction, bet)
    if view.player_bj or view.dealer_bj:  # natural blackjack ends the round at once
        await view.settle(interaction)
        return
    await interaction.response.edit_message(embed=view.embed(), view=view)


# ───────────────────────── Rocket ─────────────────────────

TICK = 1.0  # seconds between updates
MAX_CRASH = 100.0


def rocket_mult(n: int) -> float:
    return round(1 + 0.08 * n + 0.012 * n * n, 2)


def crash_point() -> float:
    u = random.random()
    return min(MAX_CRASH, max(1.0, math.floor(HOUSE_EDGE / (1 - u) * 100) / 100))


class RocketView(discord.ui.View):
    def __init__(self, origin: discord.Interaction, bet: int):
        super().__init__(timeout=None)
        self.origin = origin
        self.uid = origin.user.id
        self.bet = bet
        self.crash = crash_point()
        self.mult = 1.0
        self.n = 0
        self.done = False
        self.lock = asyncio.Lock()
        self.cashout.label = "Cash out"

    def embed(self, state: str = "fly") -> discord.Embed:
        payout = int(self.bet * self.mult)
        if state == "crash":
            e = discord.Embed(
                title="💥 The rocket exploded!",
                description=(
                    f"# x{self.crash:.2f}\n"
                    f"Lost: **{fmt(self.bet)}** {COIN}"
                ),
                color=RED,
            )
        elif state == "win":
            e = discord.Embed(
                title="✅ You cashed out in time!",
                description=(
                    f"# x{self.mult:.2f}\n"
                    f"Won: **{fmt(payout)}** {COIN} "
                    f"({'+' if payout >= self.bet else ''}{fmt(payout - self.bet)})"
                ),
                color=GREEN,
            )
        else:
            height = min(self.n, 12)
            e = discord.Embed(
                title="🚀 The rocket is flying!",
                description=(
                    f"# x{self.mult:.2f}\n"
                    f"{'▫️' * (12 - height)}🚀{'🔥' * min(height, 6)}\n"
                    f"Bet: **{fmt(self.bet)}** {COIN}\n"
                    f"Cash out now: **{fmt(payout)}** {COIN}"
                ),
                color=GOLD,
            )
        e.set_footer(text=f"Balance: {fmt(get_balance(self.uid))} {COIN}")
        return e

    async def run(self):
        try:
            while True:
                await asyncio.sleep(TICK)
                async with self.lock:
                    if self.done:
                        return
                    self.n += 1
                    nxt = rocket_mult(self.n)
                    if nxt >= self.crash:
                        self.done = True
                        self.mult = self.crash
                        await self.origin.edit_original_response(
                            embed=self.embed("crash"), view=None
                        )
                        await pets.award_game([self.uid], self.origin.channel)  # pet XP
                        return
                    self.mult = nxt
                    await self.origin.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            self.done = True

    @discord.ui.button(emoji="💸", style=discord.ButtonStyle.success)
    async def cashout(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.done:
                await interaction.response.defer()
                return
            self.done = True
            if self.mult >= self.crash:  # instant crash at x1.00
                await interaction.response.edit_message(embed=self.embed("crash"), view=None)
                await pets.award_game([self.uid], interaction.channel)  # pet XP
                return
            add_coins(self.uid, int(self.bet * self.mult))
            await interaction.response.edit_message(embed=self.embed("win"), view=None)
            await pets.award_game([self.uid], interaction.channel)  # pet XP


async def rocket_bet(interaction: discord.Interaction, bet: int):
    if not await take_bet(interaction, bet):
        return
    view = RocketView(interaction, bet)
    await interaction.response.edit_message(embed=view.embed(), view=view)
    asyncio.create_task(view.run())


# ───────────────────────── Private rooms ─────────────────────────
# Every user gets their own private thread. Games are played there, so each
# user's chat history contains only their own games.

def in_own_room(interaction: discord.Interaction) -> bool:
    ch = interaction.channel
    return isinstance(ch, discord.Thread) and room_owner(ch.id) == interaction.user.id


async def get_room(interaction: discord.Interaction) -> discord.Thread | None:
    """Finds the user's private thread, or creates it. Returns None if impossible."""
    user = interaction.user
    guild = interaction.guild
    if guild is None:
        return None

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

    parent = interaction.channel
    if not isinstance(parent, discord.TextChannel):
        return None
    thread = await parent.create_thread(
        name=f"casino-{user.display_name}"[:90],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=10080,
    )
    save_room(user.id, thread.id)
    await thread.add_user(user)
    await thread.send(
        content=f"Welcome, {user.mention}! This is your private casino room. "
                "Only you can see your games here.",
        embed=menu_embed(),
        view=MainMenu(),
    )
    return thread


async def open_room(interaction: discord.Interaction) -> discord.Thread | None:
    """Defers the interaction and returns the user's room (reports errors itself)."""
    await interaction.response.defer(ephemeral=True)
    try:
        thread = await get_room(interaction)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I can't create private threads here. Please give me these permissions: "
            "Create Private Threads, Send Messages in Threads, Manage Threads.",
            ephemeral=True,
        )
        return None
    if thread is None:
        await interaction.followup.send(
            "❌ Use this in a normal server text channel.", ephemeral=True
        )
    return thread


async def send_screen(interaction: discord.Interaction, **kwargs):
    """Shows a game screen in the user's private room (not in the shared channel)."""
    if in_own_room(interaction):
        await interaction.response.send_message(**kwargs)
        return
    thread = await open_room(interaction)
    if thread is None:
        return
    await thread.send(**kwargs)
    await interaction.followup.send(f"🎰 Your private room: {thread.mention}", ephemeral=True)


async def respond(interaction: discord.Interaction, **kwargs):
    """Normal message inside the user's room, private (ephemeral) message elsewhere."""
    await interaction.response.send_message(ephemeral=not in_own_room(interaction), **kwargs)


# ───────────────────────── Share money ─────────────────────────

async def notify_room(guild: discord.Guild | None, uid: int, text: str):
    """Best effort: drops a message into the receiver's private room."""
    tid = room_thread_id(uid)
    if not tid or guild is None:
        return
    try:
        thread = guild.get_thread(tid) or await guild.fetch_channel(tid)
        await thread.send(text)
    except discord.HTTPException:
        pass


class UserPickView(discord.ui.View):
    """Dropdown to choose another player."""

    def __init__(self, on_pick):
        super().__init__(timeout=300)
        self.on_pick = on_pick

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Choose a player...",
                       min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await self.on_pick(interaction, select.values[0])


async def share_pick(interaction: discord.Interaction, target: discord.abc.User):
    sender = interaction.user
    if target.bot:
        await interaction.response.send_message("❌ You can't send coins to a bot.", ephemeral=True)
        return
    if target.id == sender.id:
        await interaction.response.send_message("❌ You can't send coins to yourself.", ephemeral=True)
        return

    async def on_amount(i: discord.Interaction, amount: int):
        if not await take_bet(i, amount):
            return
        add_coins(target.id, amount)
        e = discord.Embed(
            title="🎁 Coins sent!",
            description=f"You sent **{fmt(amount)}** {COIN} to {target.mention}.",
            color=GREEN,
        )
        e.set_footer(text=f"Balance: {fmt(get_balance(sender.id))} {COIN}")
        await i.response.edit_message(embed=e, view=None)
        await notify_room(
            i.guild, target.id, f"🎁 {target.mention}, {sender.mention} sent you **{fmt(amount)}** {COIN}!"
        )

    embed = bet_embed(
        "🎁 Share Money",
        f"Sending coins to {target.mention}.\nChoose the amount:",
        sender.id,
    )
    await interaction.response.edit_message(
        embed=embed,
        view=BetView(on_amount, noun="amount", allow_all_in=False,
                     prompt="How many coins do you want to send?"),
    )


async def admin_pick(interaction: discord.Interaction, target: discord.abc.User):
    admin = interaction.user
    if target.bot:
        await interaction.response.send_message("❌ You can't give coins to a bot.", ephemeral=True)
        return

    async def on_amount(i: discord.Interaction, amount: int):
        add_coins(target.id, amount)  # created out of thin air, nothing is taken from the admin
        e = discord.Embed(
            title="🛠️ Coins given!",
            description=f"Gave **{fmt(amount)}** {COIN} to {target.mention}.\n"
                        f"Their balance: **{fmt(get_balance(target.id))}** {COIN}",
            color=GREEN,
        )
        await i.response.edit_message(embed=e, view=None)
        await notify_room(
            i.guild, target.id, f"🎁 {target.mention}, an admin gave you **{fmt(amount)}** {COIN}!"
        )

    embed = discord.Embed(
        title="🛠️ Admin: Give Money",
        description=f"Giving coins to {target.mention} (free, unlimited).\nChoose the amount:",
        color=BLURPLE,
    )
    await interaction.response.edit_message(
        embed=embed,
        view=BetView(on_amount, noun="amount", allow_all_in=False,
                     prompt="How many coins do you want to give?"),
    )


# ───────────────────────── Role shop ─────────────────────────

class RoleShopSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=f"Role {str(role_id)[-4:]} — {fmt(price)} coins",
                value=str(role_id),
                description=f"Purchase this role for {fmt(price)} coins",
            )
            for role_id, price in ROLE_SHOP.items()
        ]
        options.append(
            discord.SelectOption(
                label=f"🍖 Pet food — {fmt(pets.FOOD_PRICE)} coins",
                value=pets.FOOD_VALUE,
                description=f"+{pets.XP_FOOD} XP for your pet",
            )
        )
        options.append(
            discord.SelectOption(
                label=f"🎨 Canvas — {fmt(art.CANVAS_PRICE)} coins",
                value=art.CANVAS_VALUE,
                description="A private drawing room. Buy as many as you like",
            )
        )
        super().__init__(
            placeholder="Choose a role to buy…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="role_shop:select",
        )

    async def buy_food(self, interaction: discord.Interaction):
        uid = interaction.user.id
        price = pets.FOOD_PRICE
        if pets.get_pet(uid) is None:
            await interaction.response.send_message(
                "❌ Adopt a pet first (pet panel → \"Adopt a pet\"). No coins were taken.", ephemeral=True
            )
            return
        if pets.is_maxed(uid):
            await interaction.response.send_message(
                "✅ Your pet is already at the maximum level. No coins were taken.", ephemeral=True
            )
            return
        if get_balance(uid) < price:
            await interaction.response.send_message(
                f"❌ Not enough coins. Pet food costs **{fmt(price)}** {COIN}; "
                f"your balance is **{fmt(get_balance(uid))}** {COIN}.",
                ephemeral=True,
            )
            return
        if not take_coins(uid, price):
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True)
            return
        result = pets.grant_xp(uid, pets.XP_FOOD)
        if result is None:  # should not happen, but never keep the coins for nothing
            add_coins(uid, price)
            await interaction.response.send_message("❌ I couldn't feed your pet, your coins were refunded.", ephemeral=True)
            return
        old_level, new_level, _total = result
        await interaction.response.defer(ephemeral=True)
        # the details (meal, level-up, evolution) go to the pet's private room
        thread = await pets.post_feed_message(uid, old_level, new_level)
        name = pets.get_pet(uid)[0]
        if thread is not None:
            text = f"🍖 Food bought for **{fmt(price)}** {COIN}! See your pet room: {thread.mention}\n"
        else:
            text = (f"🍖 **{name}** ate and got **+{pets.XP_FOOD} XP**! Level: **{new_level}**.\n"
                    "Press \"My pet\" on the pet panel to open your pet room.\n")
        await interaction.followup.send(
            text + f"Remaining balance: **{fmt(get_balance(uid))}** {COIN}.", ephemeral=True
        )

    async def buy_canvas(self, interaction: discord.Interaction):
        uid = interaction.user.id
        price = art.CANVAS_PRICE
        if get_balance(uid) < price:
            await interaction.response.send_message(
                f"❌ Not enough coins. A canvas costs **{fmt(price)}** {COIN}; "
                f"your balance is **{fmt(get_balance(uid))}** {COIN}.",
                ephemeral=True,
            )
            return
        if not take_coins(uid, price):
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            thread = await art.create_canvas(interaction)
        except Exception as exc:  # charge first, create the room, refund if anything fails
            add_coins(uid, price)
            logging.getLogger("art").exception("Could not create a canvas")
            hint = (" I need these permissions: Create Private Threads, Send Messages in Threads, "
                    "Manage Threads." if isinstance(exc, (discord.Forbidden, TypeError)) else "")
            await interaction.followup.send(
                f"❌ I couldn't create your canvas room, so your coins were refunded.{hint}", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"🎨 Canvas bought for **{fmt(price)}** {COIN}! Your private canvas: {thread.mention}\n"
            f"Remaining balance: **{fmt(get_balance(uid))}** {COIN}.",
            ephemeral=True,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This shop can only be used in a server.", ephemeral=True)
            return

        if self.values[0] == pets.FOOD_VALUE:
            await self.buy_food(interaction)
            return
        if self.values[0] == art.CANVAS_VALUE:
            await self.buy_canvas(interaction)
            return

        role_id = int(self.values[0])
        price = ROLE_SHOP.get(role_id)
        if price is None:
            await interaction.response.send_message(
                "❌ This role is no longer in the shop. Please tell an administrator.", ephemeral=True
            )
            return
        guild = interaction.guild
        member = interaction.user
        role = guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                f"❌ Role `{role_id}` was not found on this server. Please tell an administrator.",
                ephemeral=True,
            )
            return
        if role in member.roles:
            await interaction.response.send_message("✅ You already own this role.", ephemeral=True)
            return
        if not member.guild_permissions.administrator and role >= guild.me.top_role:
            await interaction.response.send_message(
                "❌ I cannot assign this role. An administrator must move my bot role above it.",
                ephemeral=True,
            )
            return
        if get_balance(member.id) < price:
            await interaction.response.send_message(
                f"❌ Not enough coins. This role costs **{fmt(price)}** {COIN}; "
                f"your balance is **{fmt(get_balance(member.id))}** {COIN}.",
                ephemeral=True,
            )
            return

        # Charge first, then grant the role. If Discord rejects the role assignment, refund.
        if not take_coins(member.id, price):
            await interaction.response.send_message("❌ Not enough coins.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Role shop purchase")
        except (discord.Forbidden, discord.HTTPException):
            add_coins(member.id, price)
            await interaction.response.send_message(
                "❌ I couldn't assign that role, so your coins were refunded. Check my role hierarchy and permissions.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Purchased {role.mention} for **{fmt(price)}** {COIN}!\n"
            f"Remaining balance: **{fmt(get_balance(member.id))}** {COIN}.",
            ephemeral=True,
        )


class RoleShopView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleShopSelect())


def role_shop_embed() -> discord.Embed:
    lines = [f"{i}. <@&{role_id}> — **{fmt(price)}** {COIN}" for i, (role_id, price) in enumerate(ROLE_SHOP.items(), 1)]
    embed = discord.Embed(
        title="🛍️ Shop",
        description=(
            "Choose an item in the menu below. Coins are charged only after the balance check.\n\n"
            + "\n".join(lines)
            + f"\n\n🍖 **Pet food** — **{fmt(pets.FOOD_PRICE)}** {COIN} (+{pets.XP_FOOD} XP for your pet)"
            + f"\n🎨 **Canvas** — **{fmt(art.CANVAS_PRICE)}** {COIN} (a private drawing room, buy as many as you like)"
        ),
        color=GOLD,
    )
    embed.set_footer(text="If you already own a role, buying it again won't charge you.")
    return embed


# ───────────────────────── Main menu ─────────────────────────

def menu_embed() -> discord.Embed:
    return discord.Embed(
        title="🎰 Joke Casino",
        description=(
            "We play with **virtual** coins only, no real money here.\n\n"
            "🎰 **Spin Slots**: spin the reels\n"
            "💣 **Mines**: open cells and avoid the mines\n"
            "🚀 **Rocket**: cash out before it explodes\n"
            "🃏 **Blackjack**: beat the dealer to 21\n"
            "👤 **Profile**: check your balance\n"
            "🎁 **Share Money**: send coins to another player\n"
            f"💰 **Get Money**: {CLAIM_AMOUNT} {COIN} every {CLAIM_COOLDOWN // 3600 or 1} hour(s)"
        ),
        color=GOLD,
    )


class MainMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Spin Slots", emoji="🎰", style=discord.ButtonStyle.primary,
                       custom_id="casino:slots", row=0)
    async def slots(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_screen(
            interaction,
            embed=bet_embed("🎰 Slots", SLOTS_RULES, interaction.user.id),
            view=BetView(slots_bet),
        )

    @discord.ui.button(label="Mines", emoji="💣", style=discord.ButtonStyle.primary,
                       custom_id="casino:mines", row=0)
    async def mines(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_screen(
            interaction,
            embed=bet_embed(
                "💣 Mines",
                "Choose the number of mines.\nMore mines means the multiplier grows "
                "faster, but the risk is higher.",
                interaction.user.id,
            ),
            view=MinesSelectView(),
        )

    @discord.ui.button(label="Rocket", emoji="🚀", style=discord.ButtonStyle.primary,
                       custom_id="casino:rocket", row=0)
    async def rocket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_screen(
            interaction,
            embed=bet_embed(
                "🚀 Rocket",
                "The multiplier keeps growing, but the rocket can explode at any moment.\n"
                "Hit \"Cash out\" in time!\n\nChoose your bet:",
                interaction.user.id,
            ),
            view=BetView(rocket_bet),
        )

    @discord.ui.button(label="Blackjack", emoji="🃏", style=discord.ButtonStyle.primary,
                       custom_id="casino:blackjack", row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_screen(
            interaction,
            embed=bet_embed("🃏 Blackjack", BLACKJACK_RULES, interaction.user.id),
            view=BetView(blackjack_bet),
        )

    @discord.ui.button(label="Share Money", emoji="🎁", style=discord.ButtonStyle.secondary,
                       custom_id="casino:share", row=1)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_screen(
            interaction,
            embed=bet_embed(
                "🎁 Share Money",
                "Choose the player you want to send coins to:",
                interaction.user.id,
            ),
            view=UserPickView(share_pick),
        )

    @discord.ui.button(label="Profile", emoji="👤", style=discord.ButtonStyle.secondary,
                       custom_id="casino:profile", row=1)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        e = discord.Embed(title=f"👤 {user.display_name}'s profile", color=BLURPLE)
        e.add_field(name="Coins", value=f"**{fmt(get_balance(user.id))}** {COIN}")
        e.set_thumbnail(url=user.display_avatar.url)
        await respond(interaction, embed=e)

    @discord.ui.button(label="Get Money", emoji="💰", style=discord.ButtonStyle.success,
                       custom_id="casino:claim", row=1)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, left = try_claim(interaction.user.id)
        if ok:
            msg = (f"💰 You got **{CLAIM_AMOUNT}** {COIN}!\n"
                   f"Balance: **{fmt(get_balance(interaction.user.id))}** {COIN}")
        else:
            msg = f"⏳ You can claim again in **{fmt_time(left)}**."
        await respond(interaction, content=msg)


# ───────────────────────── Bot startup ─────────────────────────

class CasinoBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(MainMenu())  # keeps menu buttons working after a restart
        self.add_view(RoleShopView())  # persistent role shop dropdown
        arena.register(self, db)  # PvP arena: menu, queues, ELO, leaderboards
        pets.register(self, db)  # pets: panel, rooms, XP, leaderboards
        art.register(self, db)  # drawing canvases and the gallery
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Bot is running as {self.user} (id: {self.user.id})")

    async def on_message(self, message: discord.Message):
        try:
            await pets.on_message(message)  # +XP for the author's pet
        except Exception:
            logging.getLogger("pets").exception("on_message failed")

    async def on_member_join(self, member: discord.Member):
        if AUTO_JOIN_ROLE_ID == 0:  # no auto-join role configured in .env
            return
        role = member.guild.get_role(AUTO_JOIN_ROLE_ID)
        if role is None:
            print(f"[WARN] Auto-join role {AUTO_JOIN_ROLE_ID} was not found in guild {member.guild.id}.")
            return
        try:
            await member.add_roles(role, reason="Automatic role for new server member")
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[WARN] Could not assign auto-join role to {member.id}: {exc}")


bot = CasinoBot()


@bot.tree.command(name="menu", description="Post the casino menu in this channel (admins)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def menu(interaction: discord.Interaction):
    await interaction.response.send_message(embed=menu_embed(), view=MainMenu())


@bot.tree.command(name="shop", description="Post the shop (roles, pet food, canvas) in this channel (admins)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def shop(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can post the shop.", ephemeral=True)
        return
    await interaction.response.send_message(embed=role_shop_embed(), view=RoleShopView())


@bot.tree.command(name="room", description="Open your private casino room")
@app_commands.guild_only()
async def room(interaction: discord.Interaction):
    if in_own_room(interaction):  # already inside: just re-post the menu
        await interaction.response.send_message(embed=menu_embed(), view=MainMenu())
        return
    thread = await open_room(interaction)
    if thread is not None:
        await interaction.followup.send(f"🎰 Your private room: {thread.mention}", ephemeral=True)


@bot.tree.command(name="admin_give", description="Admin panel: give coins to a player (unlimited)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def admin_give(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admins only.", ephemeral=True)
        return
    e = discord.Embed(
        title="🛠️ Admin: Give Money",
        description="Choose the player who will receive the coins.\n"
                    "The coins are created for free, nothing is taken from you.",
        color=BLURPLE,
    )
    await interaction.response.send_message(embed=e, view=UserPickView(admin_pick), ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not found. Create a .env file (see .env.example).")
    try:
        bot.run(TOKEN, log_level=logging.DEBUG if os.getenv("DISCORD_DEBUG") == "1" else logging.INFO)
    except discord.LoginFailure:
        raise SystemExit("Discord rejected the token. Reset it in the Developer Portal (Bot -> Reset Token) "
                         "and paste the new one into .env (DISCORD_TOKEN=...) without quotes or spaces.")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Turn on Server Members Intent: Developer Portal -> Bot -> Privileged Gateway Intents.")
