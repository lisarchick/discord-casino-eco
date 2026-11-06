"""
PvP Arena for the casino bot (player vs player, no coins involved).

Games : Blackjack (BO7), Tic-Tac-Toe (BO3), Rock Paper Scissors (BO7)
Rating: separate ELO per game, +40 for a series win, -40 for a series loss
Flow  : queue -> private match thread -> Check In -> game -> ELO update

How a series works
  * Every round ends with a winner, a draw (the round is replayed) or a
    double loss (nobody acted in time, both players lose the round).
  * A player who reaches the loss limit (4 for BO7, 2 for BO3) loses the series.
  * If both players reach the limit at the same moment, both get -40 ELO.
"""

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import tasks

import pets

log = logging.getLogger("arena")

START_ELO = int(os.getenv("ARENA_START_ELO", "1000"))
ELO_STEP = 40                     # +40 for a win, -40 for a loss
ACTION_TIME = 20                  # seconds per action
CHECKIN_TIME = 60                 # seconds to press "Check In"
QUEUE_EXPIRE = 600                # seconds before a waiting player is dropped from the queue
ROUND_PAUSE = 4                   # pause between rounds
LEADERBOARD_REFRESH_MINUTES = 30
ARCHIVE_AFTER = 300               # seconds before a finished match thread is archived

GOLD = discord.Color.gold()
GREEN = discord.Color.green()
RED = discord.Color.red()
BLURPLE = discord.Color.blurple()

GAMES = {
    "blackjack": {"name": "Blackjack", "emoji": "🃏", "bo": 7, "need": 4},
    "tictactoe": {"name": "Tic-Tac-Toe", "emoji": "❌", "bo": 3, "need": 2},
    "rps": {"name": "Rock Paper Scissors", "emoji": "✊", "bo": 7, "need": 4},
}

db = None       # sqlite connection (shared with the main bot), set in register()
client = None   # the bot client, set in register()


# ───────────────────────── Database / ELO ─────────────────────────

def init_db() -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ratings ("
        "user_id INTEGER NOT NULL, game TEXT NOT NULL, "
        "elo INTEGER NOT NULL, "
        "series_won INTEGER NOT NULL DEFAULT 0, "
        "series_lost INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (user_id, game))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS lb_messages ("
        "guild_id INTEGER NOT NULL, game TEXT NOT NULL, "
        "channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
        "PRIMARY KEY (guild_id, game))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS active_matches ("
        "thread_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, game TEXT NOT NULL, "
        "p1 INTEGER NOT NULL, p2 INTEGER NOT NULL, "
        "w1 INTEGER NOT NULL, w2 INTEGER NOT NULL, l1 INTEGER NOT NULL, l2 INTEGER NOT NULL, "
        "round_no INTEGER NOT NULL, ttt_flip INTEGER NOT NULL)"
    )
    db.commit()


def get_stats(uid: int, game: str) -> tuple[int, int, int]:
    """Returns (elo, series_won, series_lost)."""
    row = db.execute(
        "SELECT elo, series_won, series_lost FROM ratings WHERE user_id = ? AND game = ?",
        (uid, game),
    ).fetchone()
    return tuple(row) if row else (START_ELO, 0, 0)


def change_elo(uid: int, game: str, delta: int, won: bool) -> None:
    db.execute(
        "INSERT OR IGNORE INTO ratings (user_id, game, elo) VALUES (?, ?, ?)",
        (uid, game, START_ELO),
    )
    db.execute(
        "UPDATE ratings SET elo = MAX(0, elo + ?), "
        "series_won = series_won + ?, series_lost = series_lost + ? "
        "WHERE user_id = ? AND game = ?",
        (delta, 1 if won else 0, 0 if won else 1, uid, game),
    )
    db.commit()


def top_players(game: str, limit: int = 10):
    return db.execute(
        "SELECT user_id, elo, series_won, series_lost FROM ratings WHERE game = ? "
        "ORDER BY elo DESC, series_won DESC LIMIT ?",
        (game, limit),
    ).fetchall()


# ───────────────────────── Embeds ─────────────────────────

def leaderboard_embed(game: str) -> discord.Embed:
    info = GAMES[game]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, elo, won, lost) in enumerate(top_players(game)):
        place = medals[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{place} <@{uid}> — **{elo}** ELO ({won}W / {lost}L)")
    body = "\n".join(lines) or "No ranked players yet. Be the first!"
    body += (
        f"\n\nUpdated <t:{int(time.time())}:R> • refreshes every "
        f"{LEADERBOARD_REFRESH_MINUTES} minutes"
    )
    return discord.Embed(
        title=f"🏆 {info['emoji']} {info['name']} Leaderboard", description=body, color=GOLD
    )


def profile_embed(user: discord.abc.User) -> discord.Embed:
    e = discord.Embed(title=f"👤 {user.display_name}'s arena profile", color=BLURPLE)
    for game, info in GAMES.items():
        elo = get_stats(user.id, game)[0]
        e.add_field(name=f"{info['emoji']} {info['name']}", value=f"**{elo}** ELO", inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    return e


def arena_embed() -> discord.Embed:
    return discord.Embed(
        title="⚔️ PvP Arena",
        description=(
            "Play 1v1 against other players and climb the leaderboards!\n\n"
            "🃏 **Blackjack**: Best of 7\n"
            "❌ **Tic-Tac-Toe**: Best of 3\n"
            "✊ **Rock Paper Scissors**: Best of 7\n"
            "👤 **Profile**: your ELO in every game\n\n"
            f"Win a series: **+{ELO_STEP} ELO** • Lose a series: **−{ELO_STEP} ELO**\n"
            f"You have **{ACTION_TIME} seconds** for every action."
        ),
        color=GOLD,
    )


# ───────────────────────── Queue ─────────────────────────

@dataclass
class QueueEntry:
    member: discord.Member
    interaction: discord.Interaction
    joined: float


queues: dict[str, list[QueueEntry]] = {g: [] for g in GAMES}
busy: dict[int, str] = {}  # user id -> what they are doing ("in the X queue" / "in a match")
_tasks: set[asyncio.Task] = set()


def purge_queue(game: str) -> None:
    now = time.time()
    keep = []
    for entry in queues[game]:
        if now - entry.joined > QUEUE_EXPIRE:
            busy.pop(entry.member.id, None)
        else:
            keep.append(entry)
    queues[game] = keep


def queue_embed(game: str, uid: int) -> discord.Embed:
    purge_queue(game)
    info = GAMES[game]
    queue = queues[game]
    names = "\n".join(f"• {e.member.mention}" for e in queue) or "Nobody yet"
    e = discord.Embed(
        title=f"{info['emoji']} {info['name']}",
        description=(
            f"Best of {info['bo']} • 1v1 • {ACTION_TIME}s per action\n"
            f"Win: **+{ELO_STEP} ELO** • Loss: **−{ELO_STEP} ELO**\n"
            "A match starts as soon as 2 players are in the queue."
        ),
        color=BLURPLE,
    )
    e.add_field(name=f"In queue ({len(queue)}/2)", value=names, inline=False)
    e.set_footer(text=f"Your ELO: {get_stats(uid, game)[0]}")
    return e


class QueueView(discord.ui.View):
    def __init__(self, game: str):
        super().__init__(timeout=600)
        self.game = game

    @discord.ui.button(label="Join Queue", emoji="✅", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        purge_queue(self.game)
        state = busy.get(uid)
        if state:
            await interaction.response.send_message(f"❌ You are already {state}.", ephemeral=True)
            return
        queues[self.game].append(QueueEntry(interaction.user, interaction, time.time()))
        busy[uid] = f"in the {GAMES[self.game]['name']} queue"

        if len(queues[self.game]) < 2:
            await interaction.response.edit_message(
                embed=queue_embed(self.game, uid), view=self
            )
            return

        first = queues[self.game].pop(0)
        second = queues[self.game].pop(0)
        await interaction.response.defer()
        await start_match(self.game, interaction, first, second)

    @discord.ui.button(label="Leave Queue", emoji="🚪", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        before = len(queues[self.game])
        queues[self.game] = [e for e in queues[self.game] if e.member.id != uid]
        if len(queues[self.game]) < before:
            busy.pop(uid, None)
        await interaction.response.edit_message(embed=queue_embed(self.game, uid), view=self)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=queue_embed(self.game, interaction.user.id), view=self
        )

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary, row=1)
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=leaderboard_embed(self.game), ephemeral=True)


async def start_match(game: str, interaction: discord.Interaction,
                      first: QueueEntry, second: QueueEntry):
    members = [first.member, second.member]
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        channel = channel.parent

    async def tell_both(embed: discord.Embed):
        for entry in (first, second):
            try:
                await entry.interaction.edit_original_response(embed=embed, view=None)
            except discord.HTTPException:
                pass

    try:
        if not isinstance(channel, discord.TextChannel):
            raise TypeError("matches can only be created in normal text channels")
        name = f"{game}-{first.member.display_name}-vs-{second.member.display_name}"
        thread = await channel.create_thread(
            name=name[:90],
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
        )
        for m in members:
            await thread.add_user(m)
    except (discord.Forbidden, discord.HTTPException, TypeError):
        for m in members:
            busy.pop(m.id, None)
        await tell_both(discord.Embed(
            description="❌ I couldn't create the match thread. I need these permissions: "
                        "Create Private Threads, Send Messages in Threads, Manage Threads.",
            color=RED,
        ))
        return

    for m in members:
        busy[m.id] = "in a match"
    await tell_both(discord.Embed(
        title="✅ Match found!", description=f"Your game room: {thread.mention}", color=GREEN
    ))
    match = Match(game, thread, members[0], members[1])
    task = asyncio.create_task(match.run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ───────────────────────── Match ─────────────────────────

def ts(seconds: int) -> int:
    return int(time.time()) + seconds


async def archive_later(thread: discord.Thread, delay: int = ARCHIVE_AFTER):
    await asyncio.sleep(delay)
    try:
        await thread.edit(archived=True, locked=True)
    except discord.HTTPException:
        pass


class CheckInView(discord.ui.View):
    def __init__(self, match: "Match"):
        super().__init__(timeout=None)
        self.match = match
        self.checked: set[int] = set()
        self.ready = asyncio.Event()

    @discord.ui.button(label="Check In", emoji="✅", style=discord.ButtonStyle.success)
    async def check_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.match.players:
            await interaction.response.send_message("❌ This is not your match.", ephemeral=True)
            return
        if uid in self.checked:
            await interaction.response.send_message("✅ You already checked in.", ephemeral=True)
            return
        self.checked.add(uid)
        await interaction.response.edit_message(embed=self.match.checkin_embed(self.checked), view=self)
        if len(self.checked) == 2:
            self.ready.set()


class Match:
    def __init__(self, game: str, thread: discord.Thread,
                 a: discord.Member, b: discord.Member):
        self.game = game
        self.info = GAMES[game]
        self.thread = thread
        self.players = {a.id: a, b.id: b}
        self.order = [a.id, b.id]
        self.need = self.info["need"]
        self.wins = {a.id: 0, b.id: 0}
        self.losses = {a.id: 0, b.id: 0}
        self.round_no = 0
        self.msg: discord.Message | None = None
        self.ttt_flip = False
        self.deadline_check_in = 0
        self.resumed = False

    def save(self):
        """Stores the series state so the match can continue after a bot restart."""
        a, b = self.order
        db.execute(
            "INSERT OR REPLACE INTO active_matches "
            "(thread_id, guild_id, game, p1, p2, w1, w2, l1, l2, round_no, ttt_flip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.thread.id, self.thread.guild.id, self.game, a, b,
             self.wins[a], self.wins[b], self.losses[a], self.losses[b],
             self.round_no, int(self.ttt_flip)),
        )
        db.commit()

    def forget(self):
        db.execute("DELETE FROM active_matches WHERE thread_id = ?", (self.thread.id,))
        db.commit()

    # ----- helpers -----

    def name(self, pid: int) -> str:
        return self.players[pid].display_name

    def mention(self, pid: int) -> str:
        return self.players[pid].mention

    def other(self, pid: int) -> int:
        return self.order[1] if pid == self.order[0] else self.order[0]

    def embed(self, body: str, color=GOLD) -> discord.Embed:
        e = discord.Embed(
            title=f"{self.info['emoji']} {self.info['name']} • Best of {self.info['bo']}",
            description=body,
            color=color,
        )
        score = "\n".join(
            f"{self.mention(p)}: 🏆 **{self.wins[p]}**  ❌ **{self.losses[p]}**/{self.need}"
            for p in self.order
        )
        e.add_field(name="Score (🏆 wins • ❌ lost rounds/limit)", value=score, inline=False)
        e.set_footer(text=f"Round {self.round_no} • Win +{ELO_STEP} / Loss −{ELO_STEP} ELO")
        return e

    async def show(self, embed: discord.Embed, view: discord.ui.View | None = None):
        if self.msg is None:
            self.msg = await self.thread.send(embed=embed, view=view)
        else:
            await self.msg.edit(embed=embed, view=view)

    async def ping(self, text: str, seconds: int = ACTION_TIME):
        try:
            await self.thread.send(text, delete_after=seconds)
        except discord.HTTPException:
            pass

    def checkin_embed(self, checked: set[int]) -> discord.Embed:
        lines = "\n".join(
            f"{'✅' if p in checked else '⏳'} {self.mention(p)}" for p in self.order
        )
        restart_note = (
            "🔄 The bot was restarted. Your score is saved, press **Check In** to continue.\n\n"
            if self.resumed else ""
        )
        e = discord.Embed(
            title=f"{self.info['emoji']} {self.info['name']} • Best of {self.info['bo']}",
            description=(
                f"{restart_note}"
                f"Both players must press **Check In** <t:{self.deadline_check_in}:R>.\n\n{lines}\n\n"
                f"You have **{ACTION_TIME} seconds** for every action. If you miss it, "
                "you lose that round."
            ),
            color=BLURPLE,
        )
        return e

    # ----- lifecycle -----

    async def run(self, resumed: bool = False):
        self.resumed = resumed
        shutting_down = False
        try:
            if not await self.check_in():
                return
            self.save()
            while True:
                self.round_no += 1
                kind, winner, text, final_view = await self.play_round()
                self.apply_round(kind, winner)
                self.save()
                over, series_winner = self.series_state()

                if kind == "win":
                    head, color = f"🏁 Round point for {self.mention(winner)}!", GREEN
                elif kind == "both":
                    head, color = "💀 Both players lose this round!", RED
                else:
                    head, color = "🤝 Draw!", BLURPLE
                await self.show(self.embed(f"{head}\n\n{text}", color), final_view)

                if over:
                    await self.finish(series_winner)
                    return
                await asyncio.sleep(ROUND_PAUSE)
        except asyncio.CancelledError:
            shutting_down = True  # the bot is stopping: keep the saved state for the restart
            raise
        except Exception:
            log.exception("Match crashed")
            try:
                await self.thread.send("⚠️ Something went wrong. The match was cancelled, no ELO changed.")
            except discord.HTTPException:
                pass
        finally:
            for pid in self.order:
                busy.pop(pid, None)
            if not shutting_down:
                self.forget()
                t = asyncio.create_task(archive_later(self.thread))
                _tasks.add(t)
                t.add_done_callback(_tasks.discard)

    async def check_in(self) -> bool:
        view = CheckInView(self)
        self.deadline_check_in = ts(CHECKIN_TIME)
        await self.show(self.checkin_embed(set()), view)
        await self.ping(
            f"{self.mention(self.order[0])} {self.mention(self.order[1])} your match is ready! "
            "Press **Check In**.",
            CHECKIN_TIME,
        )
        try:
            await asyncio.wait_for(view.ready.wait(), CHECKIN_TIME)
        except asyncio.TimeoutError:
            await self.show(
                discord.Embed(
                    title="⌛ Match cancelled",
                    description="Not everyone checked in on time. No ELO was changed.",
                    color=RED,
                ),
                None,
            )
            return False
        return True

    def apply_round(self, kind: str, winner: int | None):
        if kind == "win":
            loser = self.other(winner)
            self.wins[winner] += 1
            self.losses[loser] += 1
        elif kind == "both":
            for p in self.order:
                self.losses[p] += 1

    def series_state(self) -> tuple[bool, int | None]:
        a, b = self.order
        la, lb = self.losses[a], self.losses[b]
        if la >= self.need and lb >= self.need:
            return True, None
        if lb >= self.need:
            return True, a
        if la >= self.need:
            return True, b
        return False, None

    async def finish(self, winner: int | None):
        game = self.game
        if winner is None:
            for p in self.order:
                change_elo(p, game, -ELO_STEP, False)
            lines = "\n".join(
                f"{self.mention(p)}: −{ELO_STEP} ELO → **{get_stats(p, game)[0]}**" for p in self.order
            )
            e = discord.Embed(
                title="💀 Series over: nobody wins",
                description=f"Both players reached the loss limit at the same time.\n\n{lines}",
                color=RED,
            )
        else:
            loser = self.other(winner)
            change_elo(winner, game, +ELO_STEP, True)
            change_elo(loser, game, -ELO_STEP, False)
            e = discord.Embed(
                title=f"🏆 {self.name(winner)} wins the series!",
                description=(
                    f"Final score: **{self.wins[winner]} – {self.wins[loser]}**\n\n"
                    f"{self.mention(winner)}: +{ELO_STEP} ELO → **{get_stats(winner, game)[0]}**\n"
                    f"{self.mention(loser)}: −{ELO_STEP} ELO → **{get_stats(loser, game)[0]}**"
                ),
                color=GOLD,
            )
        e.set_footer(text="This room will be archived in a few minutes.")
        await self.thread.send(embed=e)
        try:  # pet XP: a finished match (any BO) counts as one game for both players
            await pets.award_game(self.order, self.thread)
        except Exception:
            log.exception("Could not give pet XP for the match")

    async def play_round(self):
        """Returns (kind, winner_id, text, final_view). kind: win / draw / both."""
        if self.game == "rps":
            return await self.round_rps()
        if self.game == "blackjack":
            return await self.round_blackjack()
        return await self.round_ttt()

    # ───── Rock Paper Scissors ─────

    async def round_rps(self):
        view = RPSView(self)
        await self.show(
            self.embed(
                f"Pick your move <t:{ts(ACTION_TIME)}:R>!\n"
                "Both players choose at the same time."
            ),
            view,
        )
        await self.ping(
            f"{self.mention(self.order[0])} {self.mention(self.order[1])} choose your move!"
        )
        try:
            await asyncio.wait_for(view.done.wait(), ACTION_TIME)
        except asyncio.TimeoutError:
            pass
        view.stop()

        a, b = self.order
        ca, cb = view.choices.get(a), view.choices.get(b)
        if ca is None and cb is None:
            return "both", None, "⏰ Nobody picked in time. Both players lose the round.", None
        if ca is None:
            return "win", b, f"⏰ {self.name(a)} didn't pick in time.", None
        if cb is None:
            return "win", a, f"⏰ {self.name(b)} didn't pick in time.", None

        line = f"{self.name(a)}: {RPS[ca][0]} {RPS[ca][1]}\n{self.name(b)}: {RPS[cb][0]} {RPS[cb][1]}"
        if ca == cb:
            return "draw", None, f"{line}\n\nSame choice! The round is replayed.", None
        if RPS_BEATS[ca] == cb:
            return "win", a, line, None
        return "win", b, line, None

    # ───── Blackjack (player vs player, simultaneous) ─────

    def bj_embed(self, hands, state, deadline: int | None = None) -> discord.Embed:
        icons = {"playing": "🤔 deciding", "stand": "✋ stands", "bust": "💥 bust"}
        blocks = []
        for p in self.order:
            blocks.append(
                f"{self.mention(p)}: **{hand_value(hands[p])}** ({icons[state[p]]})\n"
                f"{fmt_cards(hands[p])}"
            )
        body = "\n\n".join(blocks)
        if deadline:
            body += f"\n\nChoose **Hit** or **Stand** <t:{deadline}:R>"
        return self.embed(body)

    def bj_summary(self, hands) -> str:
        return "\n".join(
            f"{self.name(p)}: {fmt_cards(hands[p])} = **{hand_value(hands[p])}**" for p in self.order
        )

    async def round_blackjack(self):
        deck = [(r, s) for r in RANKS for s in SUITS]
        random.shuffle(deck)
        a, b = self.order
        hands = {a: [deck.pop(), deck.pop()], b: [deck.pop(), deck.pop()]}
        state = {p: ("stand" if hand_value(hands[p]) == 21 else "playing") for p in self.order}

        while any(state[p] == "playing" for p in self.order):
            active = [p for p in self.order if state[p] == "playing"]
            view = BJView(self, active)
            await self.show(self.bj_embed(hands, state, ts(ACTION_TIME)), view)
            await self.ping(f"{' '.join(self.mention(p) for p in active)} Hit or Stand?")
            try:
                await asyncio.wait_for(view.done.wait(), ACTION_TIME)
            except asyncio.TimeoutError:
                pass
            view.stop()

            missing = [p for p in active if p not in view.choices]
            if len(missing) == 2:
                return "both", None, "⏰ Nobody acted in time. Both players lose the round.", None
            if len(missing) == 1:
                return "win", self.other(missing[0]), f"⏰ {self.name(missing[0])} didn't act in time.", None

            for p in active:
                if view.choices[p] == "hit":
                    hands[p].append(deck.pop())
                    value = hand_value(hands[p])
                    if value > 21:
                        state[p] = "bust"
                    elif value == 21:
                        state[p] = "stand"
                else:
                    state[p] = "stand"
            if any(state[p] == "bust" for p in self.order):
                break

        summary = self.bj_summary(hands)
        bust_a, bust_b = state[a] == "bust", state[b] == "bust"
        if bust_a and bust_b:
            return "draw", None, f"{summary}\n\n💥 Both players bust! The round is replayed.", None
        if bust_a:
            return "win", b, f"{summary}\n\n💥 {self.name(a)} busts!", None
        if bust_b:
            return "win", a, f"{summary}\n\n💥 {self.name(b)} busts!", None
        va, vb = hand_value(hands[a]), hand_value(hands[b])
        if va > vb:
            return "win", a, summary, None
        if vb > va:
            return "win", b, summary, None
        return "draw", None, f"{summary}\n\nSame total! The round is replayed.", None

    # ───── Tic-Tac-Toe ─────

    async def round_ttt(self):
        self.ttt_flip = not self.ttt_flip  # who plays X (and starts) alternates every round
        x_id = self.order[0] if self.ttt_flip else self.order[1]
        o_id = self.other(x_id)
        marks = {x_id: "X", o_id: "O"}
        board: list[str | None] = [None] * 9
        turn = x_id

        def header(extra: str) -> str:
            return f"❌ **X**: {self.mention(x_id)}\n⭕ **O**: {self.mention(o_id)}\n\n{extra}"

        while True:
            view = TTTView(self, board, turn)
            mark_icon = "❌" if marks[turn] == "X" else "⭕"
            await self.show(
                self.embed(header(f"{mark_icon} {self.mention(turn)}'s move <t:{ts(ACTION_TIME)}:R>")),
                view,
            )
            await self.ping(f"{self.mention(turn)}, it's your turn!")
            try:
                await asyncio.wait_for(view.moved.wait(), ACTION_TIME)
            except asyncio.TimeoutError:
                view.stop()
                locked = TTTView(self, board, turn, locked=True)
                return "win", self.other(turn), f"⏰ {self.name(turn)} ran out of time.", locked
            view.stop()

            board[view.cell] = marks[turn]
            locked = TTTView(self, board, turn, locked=True)
            if ttt_winner(board):
                return "win", turn, f"{self.name(turn)} got three in a row!", locked
            if all(board):
                return "draw", None, "The board is full. The round is replayed.", locked
            turn = self.other(turn)


async def resume_matches():
    """After a bot restart: continue every unfinished series from its saved score."""
    await client.wait_until_ready()
    rows = db.execute(
        "SELECT thread_id, guild_id, game, p1, p2, w1, w2, l1, l2, round_no, ttt_flip "
        "FROM active_matches"
    ).fetchall()
    for tid, gid, game, p1, p2, w1, w2, l1, l2, round_no, flip in rows:
        try:
            guild = client.get_guild(gid) or await client.fetch_guild(gid)
            thread = guild.get_thread(tid) or await client.fetch_channel(tid)
            a = guild.get_member(p1) or await guild.fetch_member(p1)
            b = guild.get_member(p2) or await guild.fetch_member(p2)
            if thread.archived:
                await thread.edit(archived=False)
        except discord.HTTPException:
            db.execute("DELETE FROM active_matches WHERE thread_id = ?", (tid,))
            db.commit()
            continue
        match = Match(game, thread, a, b)
        match.wins = {p1: w1, p2: w2}
        match.losses = {p1: l1, p2: l2}
        match.round_no = round_no
        match.ttt_flip = bool(flip)
        busy[p1] = busy[p2] = "in a match"
        task = asyncio.create_task(match.run(resumed=True))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


# ───────────────────────── Rock Paper Scissors UI ─────────────────────────

RPS = {"rock": ("🪨", "Rock"), "paper": ("📄", "Paper"), "scissors": ("✂️", "Scissors")}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


class RPSButton(discord.ui.Button):
    def __init__(self, key: str):
        emoji, label = RPS[key]
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        v: RPSView = self.view
        uid = interaction.user.id
        if uid not in v.match.players:
            await interaction.response.send_message("❌ This is not your match.", ephemeral=True)
            return
        if uid in v.choices:
            await interaction.response.send_message("You already picked.", ephemeral=True)
            return
        v.choices[uid] = self.key
        emoji, label = RPS[self.key]
        await interaction.response.send_message(f"You picked {emoji} **{label}**. Waiting...", ephemeral=True)
        if len(v.choices) == 2:
            v.done.set()


class RPSView(discord.ui.View):
    def __init__(self, match: Match):
        super().__init__(timeout=None)
        self.match = match
        self.choices: dict[int, str] = {}
        self.done = asyncio.Event()
        for key in RPS:
            self.add_item(RPSButton(key))


# ───────────────────────── Blackjack UI ─────────────────────────

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]


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


def fmt_cards(hand: list[tuple[str, str]]) -> str:
    return " ".join(f"`{r}{s}`" for r, s in hand)


class BJView(discord.ui.View):
    def __init__(self, match: Match, active: list[int]):
        super().__init__(timeout=None)
        self.match = match
        self.active = active
        self.choices: dict[int, str] = {}
        self.done = asyncio.Event()

    async def choose(self, interaction: discord.Interaction, choice: str):
        uid = interaction.user.id
        if uid not in self.match.players:
            await interaction.response.send_message("❌ This is not your match.", ephemeral=True)
            return
        if uid not in self.active:
            await interaction.response.send_message(
                "You are not deciding this turn. Wait for the other player.", ephemeral=True
            )
            return
        if uid in self.choices:
            await interaction.response.send_message("You already chose.", ephemeral=True)
            return
        self.choices[uid] = choice
        await interaction.response.send_message(
            f"You chose **{'Hit' if choice == 'hit' else 'Stand'}**. Waiting...", ephemeral=True
        )
        if all(p in self.choices for p in self.active):
            self.done.set()

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.choose(interaction, "hit")

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.choose(interaction, "stand")


# ───────────────────────── Tic-Tac-Toe UI ─────────────────────────

LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def ttt_winner(board: list[str | None]) -> str | None:
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


class TTTButton(discord.ui.Button):
    def __init__(self, idx: int, mark: str | None, disabled: bool):
        if mark == "X":
            style, emoji = discord.ButtonStyle.danger, "❌"
        elif mark == "O":
            style, emoji = discord.ButtonStyle.primary, "⭕"
        else:
            style, emoji = discord.ButtonStyle.secondary, "⬜"
        super().__init__(style=style, emoji=emoji, row=idx // 3, disabled=disabled)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        v: TTTView = self.view
        uid = interaction.user.id
        if uid not in v.match.players:
            await interaction.response.send_message("❌ This is not your match.", ephemeral=True)
            return
        if uid != v.turn_id:
            await interaction.response.send_message("⏳ It's not your turn!", ephemeral=True)
            return
        if v.cell is None:
            v.cell = self.idx
            v.moved.set()
        await interaction.response.defer()


class TTTView(discord.ui.View):
    def __init__(self, match: Match, board, turn_id: int, locked: bool = False):
        super().__init__(timeout=None)
        self.match = match
        self.turn_id = turn_id
        self.cell: int | None = None
        self.moved = asyncio.Event()
        for i in range(9):
            self.add_item(TTTButton(i, board[i], locked or board[i] is not None))


# ───────────────────────── Arena menu ─────────────────────────

async def open_queue(interaction: discord.Interaction, game: str):
    await interaction.response.send_message(
        embed=queue_embed(game, interaction.user.id), view=QueueView(game), ephemeral=True
    )


class ArenaMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Blackjack", emoji="🃏", style=discord.ButtonStyle.primary,
                       custom_id="arena:blackjack", row=0)
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_queue(interaction, "blackjack")

    @discord.ui.button(label="Tic-Tac-Toe", emoji="❌", style=discord.ButtonStyle.primary,
                       custom_id="arena:tictactoe", row=0)
    async def tictactoe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_queue(interaction, "tictactoe")

    @discord.ui.button(label="Rock Paper Scissors", emoji="✊", style=discord.ButtonStyle.primary,
                       custom_id="arena:rps", row=0)
    async def rps(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_queue(interaction, "rps")

    @discord.ui.button(label="Profile", emoji="👤", style=discord.ButtonStyle.secondary,
                       custom_id="arena:profile", row=1)
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=profile_embed(interaction.user), ephemeral=True)


# ───────────────────────── Leaderboards (auto refresh) ─────────────────────────

@tasks.loop(minutes=LEADERBOARD_REFRESH_MINUTES)
async def refresh_leaderboards():
    rows = db.execute("SELECT guild_id, game, channel_id, message_id FROM lb_messages").fetchall()
    for guild_id, game, channel_id, message_id in rows:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.edit(embed=leaderboard_embed(game))
        except discord.NotFound:
            db.execute("DELETE FROM lb_messages WHERE guild_id = ? AND game = ?", (guild_id, game))
            db.commit()
        except discord.HTTPException:
            log.warning("Could not refresh the %s leaderboard", game)


@refresh_leaderboards.before_loop
async def _before_refresh():
    await client.wait_until_ready()


# ───────────────────────── Registration ─────────────────────────

def register(bot: discord.Client, database) -> None:
    """Call this from the bot's setup_hook BEFORE syncing the command tree."""
    global db, client
    db = database
    client = bot
    init_db()

    bot.add_view(ArenaMenu())  # keeps the arena buttons working after a restart

    @bot.tree.command(name="arena", description="Post the PvP arena menu in this channel (admins)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def arena(interaction: discord.Interaction):
        await interaction.response.send_message(embed=arena_embed(), view=ArenaMenu())

    def make_leaderboard_command(game: str, command_name: str):
        @bot.tree.command(
            name=command_name,
            description=f"Post the auto-updating {GAMES[game]['name']} leaderboard (admins)",
        )
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.guild_only()
        async def leaderboard_command(interaction: discord.Interaction):
            await interaction.response.send_message("✅ Leaderboard posted.", ephemeral=True)
            message = await interaction.channel.send(embed=leaderboard_embed(game))

            old = db.execute(
                "SELECT channel_id, message_id FROM lb_messages WHERE guild_id = ? AND game = ?",
                (interaction.guild_id, game),
            ).fetchone()
            if old:  # one live leaderboard per game: remove the previous one
                try:
                    old_channel = bot.get_channel(old[0]) or await bot.fetch_channel(old[0])
                    await (await old_channel.fetch_message(old[1])).delete()
                except discord.HTTPException:
                    pass
            db.execute(
                "INSERT OR REPLACE INTO lb_messages (guild_id, game, channel_id, message_id) "
                "VALUES (?, ?, ?, ?)",
                (interaction.guild_id, game, message.channel.id, message.id),
            )
            db.commit()

    make_leaderboard_command("blackjack", "leaderboard_blackjack")
    make_leaderboard_command("tictactoe", "leaderboard_tictactoe")
    make_leaderboard_command("rps", "leaderboard_rps")

    if not refresh_leaderboards.is_running():
        refresh_leaderboards.start()

    task = asyncio.create_task(resume_matches())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
