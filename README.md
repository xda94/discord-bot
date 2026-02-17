Discord bot that reads the discord chat and responds to keywords with certain responses.

Written in python.

Uses SQLite for DB.

Flask is used for API HTTP calls.

The bot is managed by production process manager PM2 for Node.JS.

To run this app, create a .env file where you will add the following:

DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here

Steps to install:
install SQLite3
install python3
install node.js
npm install pm2 -g && pm2 update
pip install python-dotenv
pip install requests
pip3 install flask discord.py
pm2 start bot.py --interpreter python3 --name discord-bot
pm2 start api.py --interpreter python3 --name discord-api
