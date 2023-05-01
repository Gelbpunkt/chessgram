import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg
from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, User

router = Router()

if TYPE_CHECKING:
    # Probably a mypy bug, this is correct syntax
    Pool = asyncpg.Pool[asyncpg.Record]  # type: ignore
else:
    Pool = asyncpg.Pool


# aiogram can't hash User
def hash_user(user: User) -> int:
    return hash(user.id)


User.__hash__ = hash_user  # type: ignore


def random_id() -> str:
    return str(uuid4())


async def wait_for_any_button(
    buttons: list[InlineKeyboardButton], user: User, timeout: int = 30
) -> str | None:
    completed: asyncio.Future[str] = asyncio.Future()

    async def button_handler(callback_query: CallbackQuery) -> None:
        await callback_query.answer()

        if callback_query.from_user != user:
            return

        for button in buttons:
            if button.callback_data == callback_query.data:
                completed.set_result(button.text)

    button_handler.__cg_id__ = random_id()  # type: ignore

    router.callback_query.register(button_handler)

    try:
        async with asyncio.timeout(timeout):
            result = await completed
    except asyncio.TimeoutError:
        result = None
    finally:
        for handler in router.callback_query.handlers:
            if handler.callback.__cg_id__ == button_handler.__cg_id__:  # type: ignore
                router.callback_query.handlers.remove(handler)

        return result
