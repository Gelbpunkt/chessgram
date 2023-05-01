import asyncio
import logging

from dotenv import load_dotenv

from .bot import run


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    asyncio.run(run())


if __name__ == "__main__":
    main()
