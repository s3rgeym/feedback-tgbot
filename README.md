## Feedback Telegram Bot

Бот для обратной связи.

Демо: https://t.me/feedback_s3rgeym_bot

### Как создать Telegram бота через BotFather:

1. Найдите и откройте чат с [@BotFather](https://t.me/BotFather) в Telegram.
2. Используйте команду `/newbot` и следуйте инструкциям для создания нового бота.
3. Получите и сохраните API токен, который BotFather предоставит вам после создания бота.

```
<censored>, [8/22/24 11:50 PM]
/newbot

BotFather, [8/22/24 11:50 PM]
Alright, a new bot. How are we going to call it? Please choose a name for your bot.

<censored>, [8/22/24 11:50 PM]
Бот для обратной связи

BotFather, [8/22/24 11:50 PM]
Good. Now let's choose a username for your bot. It must end in `bot`. Like this, for example: TetrisBot or tetris_bot.

<censored>, [8/22/24 11:52 PM]
feedback_s3rgeym_bot

BotFather, [8/22/24 11:52 PM]
Sorry, the username must end in 'bot'. E.g. 'Tetris_bot' or 'Tetrisbot'

<censored>, [8/22/24 11:52 PM]
feedback_s3rgeym_bot

BotFather, [8/22/24 11:52 PM]
Done! Congratulations on your new bot. You will find it at t.me/feedback_s3rgeym_bot. You can now add a description, about section and profile picture for your bot, see /help for a list of commands. By the way, when you've finished creating your cool bot, ping our Bot Support if you want a better username for it. Just make sure the bot is fully operational before you do this.

Use this token to access the HTTP API:
<censored>
Keep your token secure and store it safely, it can be used by anyone to control your bot.

For a description of the Bot API, see this page: https://core.telegram.org/bots/api
```

### Запуск

```bash
git clone https://github.com/s3rgeym/feedback-tgbot/
cd feedback-tgbot
```

Создади `.env` файл:

```bash
cp .env{.example,}
```

Пропишите в `.env` данные от бота и id своего аккаунта:

```bash
nano .env
```

Свой айди можно узнать, например, через [@myidbot](https://t.me/myidbot).

Проще всего бота запустить через dockr-compose:

```bash
docker compose up -d
```

### Разработка

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install --upgrade pip
```
