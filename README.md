Discord bot that reads the discord chat and responds to keywords with certain responses.

Written in python.

Uses SQLite for DB.

Flask is used for API HTTP calls.

The bot is managed by production process manager PM2 for Node.JS.

To run this app, create a .env file where you will add the following:

DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
