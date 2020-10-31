import asyncio
import functools
import logging
from datetime import timedelta, date
from typing import Optional

import pymongo
import sentry_sdk
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import CommandStart, CommandHelp
from aiohttp import ClientSession, DummyCookieJar, ClientTimeout
from fiobank import FioBank
from motor.core import AgnosticCollection as MongoCollection
from motor.motor_asyncio import AsyncIOMotorClient
from sentry_sdk.integrations.aiohttp import AioHttpIntegration

from cashier.config import config
from cashier.telegram_bot.const import CMD_PAIR, CMD_COMMENT
from cashier.telegram_bot.models import TransactionStorage, Membership, Transaction
from cashier.telegram_bot.views import new_transaction_msg, help_msg, start_msg, offending_flight_msg
from cashier.util import cron_task
from cashier.xcontest import Takeoff, get_flights, Pilot, Flight

# telemetry
log = logging.getLogger(__name__)
sentry_sdk.init(**config.get_namespace("SENTRY_"), integrations=[AioHttpIntegration()])

# Telegram
CHAT_ID = config["TELEGRAM_CHAT_ID"]  # TODO setup a protection for bot to reply only to this CHAT_ID
bot = Bot(token=config["TELEGRAM_BOT_TOKEN"])
dispatcher = Dispatcher(bot)

# Mongo
mongo_client: Optional[AsyncIOMotorClient] = None
db: Optional[MongoCollection] = None


# FIXME
def get_db():
    global mongo_client, db
    if db is None:
        mongo_client = AsyncIOMotorClient(config["MONGO_CONNECTION_STRING"])
        db = mongo_client.default
    return db


cron_task = functools.partial(cron_task, run_after_startup=config["RUN_TASKS_AFTER_STARTUP"])


# Step 1
# Get a transaction from the bank account
@cron_task(config["TRANSACTION_WATCH_CRON"])
async def watch_transactions(trans_storage: TransactionStorage):
    for trans in await trans_storage.get_new_transactions():
        asyncio.create_task(process_transaction(trans_storage, trans))


# Step 2
# Backup a transaction to DB and request operators to pair a transaction.
async def process_transaction(trans_storage: TransactionStorage, trans: Transaction):
    """Process a single transaction which happened on the bank account."""
    log.info(f"Processing transaction {trans}")
    await trans_storage.store_transaction(trans)

    try:
        membership = Membership.from_amount(trans.amount)
    except ValueError:
        membership = None

    msg = new_transaction_msg(trans, membership)
    await bot.send_message(CHAT_ID, msg, parse_mode="HTML")


async def _parse_pair_msg(message: types.Message):
    log.info("Parsing a pair command")
    # TODO make nicer
    parts = message.text.strip().split(" ")
    if len(parts) != 4:
        raise ValueError(f"Expected 3 arguments, got {len(parts) - 1}")
    trans_id, membership, username = parts[1].strip(), parts[2].strip(), parts[3].strip()

    if not trans_id.isnumeric():
        raise ValueError("Transaction ID must be numeric")

    membership = Membership.from_str(membership)

    pilot = Pilot(username=username)
    # TODO reuse session?
    async with ClientSession(
        timeout=ClientTimeout(total=10),
        raise_for_status=True,
        cookie_jar=DummyCookieJar(),
        headers={"User-Agent": config["USER_AGENT"]},
    ) as session:
        await pilot.load_id(session)
    log.debug(f"Fetched ID for {pilot}")

    return trans_id, membership, pilot


# Step 3
# Pair a transaction (= create a membership)
@dispatcher.message_handler(commands=[CMD_PAIR])
async def pair(message: types.Message):
    try:
        transaction_id, membership, pilot = await _parse_pair_msg(message)
    except ValueError as e:
        return await message.answer(f"{str(e)}. Please see /help")
    log.info(f"Pairing {transaction_id=} {membership} to {pilot}")

    if existing := await get_db().membership.find_one({"transaction_id": transaction_id}):
        await message.answer(
            f"This transaction is already paired as {existing['type']} for pilot {existing['pilot']['username']}."
        )
        return

    await get_db().membership.insert_one({
        "transaction_id": transaction_id,
        "type": membership.value,
        "pilot": pilot.as_dict(),
        "date_paired": date.today().isoformat(),
    })

    await message.answer("Okay, paired")


@cron_task(config["FLIGHT_WATCH_CRON"])
async def watch_flights():
    async with ClientSession(
        timeout=ClientTimeout(total=10),
        raise_for_status=True,
        cookie_jar=DummyCookieJar(),
        headers={"User-Agent": config["USER_AGENT"]},
    ) as session:
        takeoff = Takeoff.DOUBRAVA
        day = date.today() - timedelta(days=config["FLIGHT_WATCH_DAYS_BACK"])
        log.debug(f"Downloading flights from {day} for {takeoff}")
        flights = get_flights(session, takeoff, day)

        num = 0
        async for flight in flights:
            asyncio.create_task(process_flight(flight))
            num += 1

        log.info(f"Downloaded {num} flights")


async def process_flight(flight: Flight):
    log.info(f"Processing flight {flight}")

    # TODO can possibly be reduced to write only one time after processing
    existing_flight = await get_db().flights.find_one({"id": flight.id})
    if not existing_flight:
        log.debug(f"Storing flight {flight.id} into DB")
        await get_db().flights.insert_one(flight.as_dict())
    elif existing_flight["processed"]:
        log.debug(f"Skipping flight {flight.id} as it is already processed")
        return

    pilot_username = flight.pilot.username
    flight_date = flight.datetime.date()
    # TODO use more filters:
    # - yearly first (then daily)
    # - not used daily
    # - used yearly only for current year
    membership = await get_db().membership.find_one({
            "pilot.username": pilot_username,
            "$or": [
                {"type": Membership.daily.value, "used_for": flight_date.isoformat()},
                {"type": Membership.yearly.value, "used_for": flight_date.year},
                {"used_for": None},
            ],
    })
    # TODO maybe we somehow want to use date_paired:?
    if not membership:
        log.debug(f"No membership found for flight {flight.id}, reporting")
        msg = offending_flight_msg(flight)
        asyncio.create_task(bot.send_message(CHAT_ID, msg, parse_mode="HTML"))
    else:
        log.debug(f"Found valid membership for flight {flight.id}: {membership}")
        membership_type = Membership(membership["type"])
        # following updates are idempotent, therefore
        if membership_type == Membership.yearly:
            get_db().membership.update_one({"_id": membership["_id"]}, {"$set": {"used_for": flight_date.year}})
        elif membership_type == Membership.daily:
            get_db().membership.update_one({"_id": membership["_id"]}, {"$set": {"used_for": flight_date.isoformat()}})

    log.debug(f"Setting flight {flight.id} as processed")
    get_db().flights.update_one({"id": flight.id}, {"$set": {"processed": True}})


@dispatcher.message_handler(CommandStart())
async def start(message: types.Message):
    await message.answer(start_msg(), parse_mode="HTML")


@dispatcher.message_handler(CommandHelp())
async def help_(message: types.Message):
    await message.answer(help_msg(), parse_mode="HTML")


@dispatcher.message_handler(commands=[CMD_COMMENT])
async def comment(message: types.Message):
    # TODO
    await message.answer("Not implemented yet")


async def touch_liveness_probe():
    log.info("Starting liveness touch loop")
    while True:
        config["LIVENESS"].touch()
        await asyncio.sleep(config["LIVENESS_SLEEP"])


async def handle_telegram():
    # startup message + cleanup copied from aiogram.executor
    user = await dispatcher.bot.me
    log.info(f"Starting Telegram bot: {user.full_name} [@{user.username}]")

    # this call blocks
    await dispatcher.start_polling(reset_webhook=True)

    await dispatcher.storage.close()
    await dispatcher.storage.wait_closed()
    await dispatcher.bot.close()


def handle_exception(loop, context):
    if "exception" in context:
        log.exception("Unhandled exception", exc_info=context["exception"])
    else:
        log.error("Unhandled exception: %s", context["message"])

    if loop.is_closed():
        return

    log.info("Shutting down all running tasks")
    for task in asyncio.all_tasks():
        task.cancel()


async def _smoke_test_mongo():
    log.info(f"Smoke testing connection to Mongo")
    await get_db().transactions.find_one()


async def setup_mongo_indices():
    await asyncio.gather(
        get_db().flights.create_index([("id", pymongo.DESCENDING)], unique=True),
        get_db().transactions.create_index([("id", pymongo.DESCENDING)], unique=True),
        get_db().membership.create_index([("transaction_id", pymongo.DESCENDING)], unique=True),
    )


async def main():
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(handle_exception)

    await _smoke_test_mongo()
    await setup_mongo_indices()

    bank = FioBank(config["FIO_API_TOKEN"])
    trans_storage = TransactionStorage(bank, db.transactions)

    asyncio.create_task(touch_liveness_probe(), name="touch_liveness_probe")
    asyncio.create_task(watch_transactions(trans_storage), name="watch_transactions")
    asyncio.create_task(watch_flights(), name="watch_flights")
    await handle_telegram()
