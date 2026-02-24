Here's a Discord bot that reads chat and responds to keywords.

It's written in Python and uses SQLite for the database.

Flask handles the API HTTP calls.

PM2, a Node.js process manager, keeps the bot running.

To get it going, create a `.env` file and add these:

```
DISCORD_TOKEN=your_token_here
HOST=your_host_here
PORT=your_port_here
```

Here's how to install it:
Install SQLite3
Install Python 3
Install Node.js
`npm install pm2 -g && pm2 update`
`pip install python-dotenv`
`pip install requests`
`pip3 install flask discord.py`
`pm2 start bot.py --interpreter python3 --name discord-bot`
`pm2 start api.py --interpreter python3 --name discord-api`
