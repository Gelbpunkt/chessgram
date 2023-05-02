import asyncio
import logging

import uvloop
from dotenv import load_dotenv

from .bot import run


def main() -> None:
    uvloop.install()
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    asyncio.run(run())


if __name__ == "__main__":
    main()
