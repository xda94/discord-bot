# 🤖 Discord Keyword Responder Bot

A lightweight, Python-powered Discord bot that monitors chat and triggers automated responses based on specific keywords. It features a Flask-based API for external management and uses PM2 for "set-it-and-forget-it" stability.

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
Now install the python dependencies
```bash
pip3 install -r requirements.txt
```

### 3. Configuration 
Create a .env file in the root folder and add your credentials:
```env
DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
```

## 🏃 Execution
​This project runs as two separate processes (the bot and the API). Use PM2 to keep them running in the background.

​Starting the Bot
```bash
pm2 start bot.py --interpreter python3 --name discord-bot
```
Starting the API
```bash
pm2 start api.py --interpreter python3 --name discord-api
```

Useful PM2 Commands
​* pm2 status — Check if the bot and API are online.
* ​pm2 logs — View real-time logs and errors.
* ​pm2 restart all — Restart both processes.
* ​pm2 stop all — Stop the bot and API.


## ​📁 Project Structure
* ​bot.py — The core Discord client logic.
* ​api.py — Flask application for API handling.
* ​requirements.txt — List of Python dependencies.
* ​.env — Environment variables (ignored by git).
* ​database.db — SQLite database file.
