import asyncpg
from aiogram.types import User
from bidict import bidict

from .utils import Pool


class UserCache:
    def __init__(self, pool: Pool) -> None:
        self.pool = pool
        self.cache: bidict[int, str] = bidict()

    async def by_name(self, name: str) -> int | None:
        if (id := self.cache.inverse.get(name)) is not None:
            return id

        id = await self.pool.fetchval(
            'SELECT "id" FROM user_lookups WHERE "name"=$1;', name
        )

        return id

    async def by_id(self, id: int) -> str | None:
        if (name := self.cache.get(id)) is not None:
            return name

        name = await self.pool.fetchval(
            'SELECT "name" FROM user_lookups WHERE "id"=$1;', id
        )

        return name

    async def update(self, user: User) -> None:
        username = user.username or user.full_name

        if (name := self.cache.get(user.id)) is not None and name == username:
            return

        self.cache[user.id] = username

        async with self.pool.acquire() as conn:
            old_user = await conn.fetchrow(
                'SELECT * FROM user_lookups WHERE "id"=$1;', user.id
            )

            if old_user is not None and old_user["name"] == username:
                return

            async with conn.transaction():
                await conn.execute(
                    'DELETE FROM user_lookups WHERE "name"=$1;', username
                )

                if old_user:
                    await conn.execute(
                        'UPDATE user_lookups SET "name"=$1 WHERE "id"=$2;',
                        username,
                        user.id,
                    )
                else:
                    await conn.execute(
                        'INSERT INTO user_lookups ("id", "name") VALUES ($1, $2);',
                        user.id,
                        username,
                    )
