import asyncio
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

import aiohttp
import asyncpg
import chess.engine
import orjson
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .game import ChessGame, ProtocolAdapter
from .user_cache import UserCache
from .utils import Pool, random_id, router, wait_for_any_button


class MessageListener:
    def __init__(self) -> None:
        self.callbacks: dict[
            str, tuple[Callable[[Message], Awaitable[bool]], asyncio.Future[Message]]
        ] = {}

    async def wait_for(
        self, check: Callable[[Message], Awaitable[bool]], timeout: int | None = 30
    ) -> Message:
        id = random_id()
        fut: asyncio.Future[Message] = asyncio.Future()
        self.callbacks[id] = (check, fut)

        try:
            async with asyncio.timeout(timeout):
                return await fut
        except Exception as e:
            # Timeout
            del self.callbacks[id]
            raise e

    async def process(self, msg: Message) -> None:
        for id, (check, fut) in list(self.callbacks.items()):
            if await check(msg):
                fut.set_result(msg)
                del self.callbacks[id]


class MatchTracker:
    def __init__(self) -> None:
        self.matches: set[int] = set()

    @asynccontextmanager
    async def lock(self, id: int) -> AsyncIterator[None]:
        if id in self.matches:
            raise ValueError("Match in progress!")

        self.matches.add(id)

        try:
            yield
        finally:
            self.matches.remove(id)


@router.message(Command(commands=["start", "register"]))
async def register(message: Message, pool: Pool, user_cache: UserCache) -> None:
    """
    Register an account for playing ELO rated games.
    """
    if message.from_user is None:
        return

    await user_cache.update(message.from_user)

    async with pool.acquire() as conn:
        if await conn.fetchrow(
            'SELECT * FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
            message.from_user.id,
            message.chat.id,
        ):
            await message.answer("You are already registered.")
            return

        await conn.execute(
            'INSERT INTO chess_players ("user",  "chat") VALUES ($1, $2);',
            message.from_user.id,
            message.chat.id,
        )

    await message.answer(
        "You have been registered with an ELO of 1000 as a default. Play matches against other registered users to increase it!"
    )


@router.message(Command(commands=["elo"]))
async def elo(message: Message, pool: Pool, user_cache: UserCache) -> None:
    """
    Shows your ELO and the best chess players' ELO rating.
    """
    if message.from_user is None:
        return

    await user_cache.update(message.from_user)

    async with pool.acquire() as conn:
        player = await conn.fetchrow(
            'SELECT * FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
            message.from_user.id,
            message.chat.id,
        )
        top_players = await conn.fetch(
            'SELECT * FROM chess_players WHERE "chat"=$1 ORDER BY "elo" DESC LIMIT 15;',
            message.chat.id,
        )
        text = "<b>Chess ELOs</b>\n\n<u>Top 15</u>\n"
        for idx, row in enumerate(top_players):
            user = await message.chat.get_member(row["user"])
            user_name = user.user.full_name if user else "Unknown Player"
            user_text = f"<b>{user_name}</b> with ELO <b>{row['elo']}</b>"
            text = f"{text}{idx + 1}. {user_text}\n"

        if player:
            player_pos = await conn.fetchval(
                "SELECT position FROM (SELECT chess_players.*, ROW_NUMBER()"
                ' OVER(ORDER BY chess_players."elo" DESC) AS position FROM'
                ' chess_players WHERE chess_players."chat"=$1) s WHERE s."user"=$2 LIMIT 1;',
                message.chat.id,
                message.from_user.id,
            )
            text = f"{text}\n<u>Your position</u>\n{player_pos}. <b>{message.from_user.full_name}</b> with ELO <b>{player['elo']}</b>"

    await message.answer(text)


@router.message(Command(commands=["play"]))
async def play(
    message: Message,
    command: CommandObject,
    pool: Pool,
    user_cache: UserCache,
    matches: MatchTracker,
    engine: chess.engine.UciProtocol,
    session: aiohttp.ClientSession,
    messages: MessageListener,
) -> None:
    """
    Starts a game of chess.
    """
    if message.from_user is None:
        return

    await user_cache.update(message.from_user)

    # Arguments are either a difficulty *or* an enemy username
    if command.args is not None:
        if command.args.isnumeric():
            difficulty = int(command.args)
            enemy = None
        else:
            user_name = (
                command.args[1:] if command.args.startswith("@") else command.args
            )
            enemy_id = await user_cache.by_name(user_name)

            if enemy_id:
                enemy_info = await message.chat.get_member(enemy_id)
            else:
                enemy_info = None

            if enemy_info is None:
                await message.reply(
                    "Sorry, I do not know who that opponent is. Have they sent a message yet?"
                )
                return
            else:
                enemy = enemy_info.user

            difficulty = None
    else:
        difficulty = 3
        enemy = None

    if enemy is not None and enemy.id == message.from_user.id:
        await message.reply("You cannot play against yourself.")
        return

    if difficulty is not None and (difficulty < 1 or difficulty > 10):
        await message.reply("Difficulty may be 1-10.")
        return

    buttons = [
        InlineKeyboardButton(text="White", callback_data=random_id()),
        InlineKeyboardButton(text="Black", callback_data=random_id()),
    ]

    colour_msg = await message.reply(
        "Please choose the colour you want to take.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[buttons],
        ),
    )

    side = await wait_for_any_button(buttons, user=message.from_user)

    if side is None:
        await colour_msg.reply("You took too long to choose a side.")

    player_side = chess.WHITE if side == "White" else chess.BLACK

    await colour_msg.delete()

    rated = False

    if enemy is not None:
        async with pool.acquire() as conn:
            player_elo = await conn.fetchval(
                'SELECT elo FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
                message.from_user.id,
                message.chat.id,
            )
            enemy_elo = await conn.fetchval(
                'SELECT elo FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
                enemy.id,
                message.chat.id,
            )

        if player_elo is not None and enemy_elo is not None:
            buttons = [
                InlineKeyboardButton(text="Yes", callback_data=random_id()),
                InlineKeyboardButton(text="No", callback_data=random_id()),
            ]

            rated_msg = await message.reply(
                f"Would you like to play an ELO-rated match? Your elo is {player_elo}, their elo is {enemy_elo}.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[buttons],
                ),
            )

            rated = (
                await wait_for_any_button(buttons, user=message.from_user)
            ) == "Yes"

            await rated_msg.delete()

            confirm_text = f"{enemy.mention_html()}, you have been challenged by {message.from_user.mention_html()}. They will be {side}. Do you accept?"
            if rated:
                confirm_text = f"{confirm_text} <b>The match will be ELO rated.</b>"

            buttons = [
                InlineKeyboardButton(text="Yes", callback_data=random_id()),
                InlineKeyboardButton(text="No", callback_data=random_id()),
            ]

            confirm_msg = await message.reply(
                confirm_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
            )

            accepted = (await wait_for_any_button(buttons, user=enemy)) == "Yes"

            await confirm_msg.delete()

            if not accepted:
                await message.reply(
                    f"{enemy.mention_html()} rejected the chess challenge."
                )
                return

    try:
        async with matches.lock(message.chat.id):
            game = ChessGame(
                engine,
                session,
                pool,
                messages,
                message,
                message.from_user,
                player_side,
                enemy,
                difficulty,
                rated,
            )

            await game.run()
    except ValueError:
        await message.reply("There is already an ongoing game in this chat!")


@router.message()
async def message_handler(
    message: Message, user_cache: UserCache, messages: MessageListener
) -> None:
    if message.from_user is not None:
        await user_cache.update(message.from_user)
    await messages.process(message)


async def run() -> None:
    TOKEN = os.environ["TOKEN"]
    DB = os.environ["DB"]
    UCI_HOST = os.environ["UCI_HOST"]
    UCI_PORT = int(os.environ["UCI_PORT"])
    OKAPI_TOKEN = os.environ["OKAPI_TOKEN"]

    bot = Bot(TOKEN, parse_mode="HTML")
    pool = await asyncpg.create_pool(DB)

    # https://github.com/bryanforbes/asyncpg-stubs/issues/120
    if pool is None:
        raise RuntimeError("pool is None")

    _, adapter = await asyncio.get_running_loop().create_connection(
        lambda: ProtocolAdapter(chess.engine.UciProtocol()), UCI_HOST, UCI_PORT
    )
    engine = adapter.protocol
    await engine.initialize()

    session = aiohttp.ClientSession(
        headers={"Authorization": OKAPI_TOKEN}, json_serialize=orjson.dumps  # type: ignore
    )

    dp = Dispatcher()
    dp["pool"] = pool
    dp["engine"] = engine
    dp["session"] = session
    dp["user_cache"] = UserCache(pool)
    dp["matches"] = MatchTracker()
    dp["messages"] = MessageListener()
    dp.include_router(router)

    try:
        exc = None
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except Exception as e:
        exc = e
    finally:
        await pool.close()
        await session.close()

    if exc:
        raise exc
