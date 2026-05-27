# 🤖 Discord Keyword Responder Bot

A lightweight, Python-powered Discord bot that monitors chat and triggers automated responses based on specific keywords. It features a Flask-based REST API for external management, configurable mood-based tease messages, reminders, daily jokes, web scraping for price tracking, and uses PM2 for "set-it-and-forget-it" stability.

---

## 🛠 Tech Stack

* **Language:** [Python 3.x](https://www.python.org/)
* **Library:** [discord.py](https://github.com/Rapptz/discord.py)
* **Database:** [SQLite3](https://www.sqlite.org/index.html)
* **API:** [Flask](https://flask.palletsprojects.com/)
* **Process Manager:** [PM2](https://pm2.keymetrics.io/)

---

## 🚀 Getting Started

### 1. Prerequisites
Ensure you have the following installed on your system:
* **SQLite3**
* **Python 3.x**
* **Node.js & NPM** (Required for PM2)

### 2. Installation
First, install the global process manager:
```bash
npm install pm2 -g && pm2 update
```
Now install the python dependencies:
```bash
pip3 install -r requirements.txt or pip3 install --break-system-packages -r requirements.txt
```

### 3. Configuration 
Create a `.env` file in the root folder and add your credentials:
```env
DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
```

---

## 🏃 Execution
This project runs as two separate processes (the bot and the API). Use PM2 to keep them running in the background.

**Starting the Bot**
```bash
pm2 start bot.py --interpreter python3 --name discord-bot
```
**Starting the API**
```bash
pm2 start api.py --interpreter python3 --name discord-api
```

**Useful PM2 Commands**

* `pm2 status` — Check if the bot and API are online.
* `pm2 logs` — View real-time logs and errors.
* `pm2 restart all` — Restart both processes.
* `pm2 stop all` — Stop the bot and API.

---

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/add <keyword> <response>` | Add a keyword-response pair. Multiple responses per keyword are supported — the bot picks one at random. |
| `/remind <when> <who> <what>` | Set a timed reminder. Format: `30m`, `2h`, `1d`. |
| `/topkeywords [user]` | Show the most triggered keywords in the server, optionally filtered by user. |
| `/mood <mood>` | Set the bot's tease mood. Available moods are loaded dynamically from `moods.py`, plus `random`. |
| `/joke <text>` | Add a joke to the daily joke rotation. |
| `/joke_activation <time>` | Activate the daily joke in the current channel at a given time (e.g. `14:00`). |
| `/stats` | Show system hardware stats (CPU, RAM, disk, temperature, network, uptime). |
| `/scrape-item <url>` | Add a link to track price and stock. Checked every 12 hours. |
| `/scrape-show` | Show your tracked items and their current prices. |
| `/scrape-item-delete <url>` | Remove a link and its history from tracking. |
| `/help` | Show the help message. |

---

## 📁 Project Structure

| File | Description |
|---|---|
| `bot.py` | Core Discord client — event handlers, slash commands, and background tasks. |
| `api.py` | Flask REST API for managing responses, reminders, and jokes externally. |
| `db.py` | Database layer — all SQLite queries and a shared connection context manager. |
| `moods.py` | Mood/tease message definitions and bot constants (`COOLDOWN`, `TEASE_MOODS`, etc.). |
| `logger.py` | Shared logging setup used by both the bot and the API. |
| `requirements.txt` | Python dependencies. |
| `.env` | Environment variables (ignored by git). |
| `responses.db` | SQLite database file (auto-created on first run). |
