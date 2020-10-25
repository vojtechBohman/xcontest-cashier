from llconfig import Config
from pathlib import Path

config = Config()

config.init("TELEGRAM_BOT_TOKEN", str, None)
config.init("TELEGRAM_CHAT_ID", int, None)

config.init("SENTRY_DSN", str, None)
config.init("SENTRY_ENVIRONMENT", str, "production")

# https://crontab.guru/
config.init("TRANSACTION_WATCH_CRON", str, "0 * * * *")  # each hour
config.init("FLIGHT_WATCH_CRON", str, "0 20 * * *")  # every day at 20:00

config.init("LIVENESS", Path, Path("/tmp/liveness"))
config.init("LIVENESS_SLEEP", int, 10)  # seconds

config.init("FIO_API_TOKEN", str, None)

config.init("MONGO_CONNECTION_STRING", str)

config.load()
