# Discord Casino & Community Ecosystem

A full game economy for Discord communities: four casino games, virtual coins, a role shop, tamagotchi-style pets with evolution, a PvP arena with matchmaking and ELO, and a collaborative drawing mode — every player gets their own private thread, so chats never mix. Virtual coins only, no real money. Built with **discord.py 2** (slash commands, persistent views, modals), ~3,800 lines.

## Games

| Game | Mechanics |
|---|---|
| 🎰 Slots | bet → spin, animated result |
| 💣 Mines | pick safe cells, cash out any time |
| 🚀 Rocket | crash-style multiplier, cash out before the boom |
| 🃏 Blackjack | hit / stand / double down |

## Economy & engagement

- Hourly coin claim with cooldown, player-to-player transfers, admin give panel
- **Role shop**: buy server roles with coins (roles & prices configured in `.env`)
- Auto-role for new members
- **Pets**: adopt in a private thread, gain XP from chat activity and matches, 5 evolution stages with artwork, feeding, per-pet rooms, leaderboards
- **PvP Arena**: queue → private match thread → check-in → game → ELO rating updates; queue entries expire
- **Canvas**: buy a canvas, draw right in a private thread (cursor, pen/eraser, 23 colors + custom hex, 1–24px brush, undo, line jumps, gallery)

## Tech stack

Python 3.10+ · discord.py 2 · SQLite · Pillow

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # DISCORD_TOKEN, GUILD_ID, economy + role shop config
python bot.py
```

Discord Developer Portal → Bot → enable **Server Members Intent**. Bot role must sit above the roles it hands out.

## Structure

```
bot.py     economy, games, shop, rooms, leaderboard
arena.py   PvP matchmaking, check-in, ELO
pets.py    pet system: XP, stages, private threads
art.py     canvas drawing + gallery
```
