"""
The IdleRPG Discord Bot
Copyright (C) 2018-2021 Diniboy and Gelbpunkt

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
from __future__ import annotations

import asyncio
import datetime
import random
import re
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

import aiogram
import aiohttp
import asyncpg
import chess
import chess.engine
import chess.pgn
import chess.svg

from .utils import Pool, random_id, wait_for_any_button

if TYPE_CHECKING:
    from .bot import MessageListener

Move = Literal["draw"] | Literal["resign"] | Literal["timeout"] | chess.Move
PotentiallyIllegalMove = Move | Literal[False]


# https://github.com/niklasf/python-chess/issues/492
class ProtocolAdapter(asyncio.Protocol):
    def __init__(self, protocol: chess.engine.UciProtocol) -> None:
        self.protocol = protocol

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = TransportAdapter(transport)  # type: ignore
        self.protocol.connection_made(self.transport)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport.alive = False
        self.protocol.connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        self.protocol.pipe_data_received(1, data)


class TransportAdapter(
    asyncio.SubprocessTransport, asyncio.ReadTransport, asyncio.WriteTransport
):
    def __init__(self, transport: asyncio.Transport):
        self.alive = True
        self.transport = transport

    def get_pipe_transport(self, fd: int) -> TransportAdapter:
        return self

    def write(self, data: bytes | bytearray | memoryview) -> None:
        self.transport.write(data)

    def get_returncode(self) -> int | None:
        return None if self.alive else 0

    def get_pid(self) -> int:
        raise NotImplementedError()

    def close(self) -> None:
        self.transport.close()

    # Unimplemented: kill(), send_signal(signal), terminate(), and various flow
    # control methods.


class GameStatus(Enum):
    Initialized = auto()
    Playing = auto()
    WhiteResigned = auto()
    BlackResigned = auto()
    Draw = auto()

    @classmethod
    def resigned(cls, color: chess.Color) -> GameStatus:
        if color == chess.WHITE:
            return GameStatus.WhiteResigned
        else:
            return GameStatus.BlackResigned

    @property
    def is_resigned(self) -> bool:
        return self == GameStatus.WhiteResigned or self == GameStatus.BlackResigned


class ChessGame:
    def __init__(
        self,
        engine: chess.engine.UciProtocol,
        session: aiohttp.ClientSession,
        pool: Pool,
        messages: MessageListener,
        message: aiogram.types.Message,
        player: aiogram.types.User,
        player_color: chess.Color = chess.WHITE,
        enemy: aiogram.types.User | None = None,
        difficulty: int | None = None,
        rated: bool = False,
    ):
        self.player = player
        self.enemy = enemy
        self.message = message
        self.board = chess.Board()
        self.engine = engine
        self.session = session
        self.pool = pool
        self.messages = messages
        self.history: list[list[chess.Move]] = []
        self.move_no = 0
        self.status = GameStatus.Initialized
        self.colors = {
            player: player_color,
            enemy: chess.BLACK if player_color == chess.WHITE else chess.WHITE,
        }
        self.rated = rated
        self.msg: aiogram.types.Message | None = None

        if self.enemy is None:
            self.limit = chess.engine.Limit(depth=difficulty)

    def pretty_moves(self) -> list[str]:
        history = chess.Board().variation_san(self.board.move_stack)
        splitted = re.split(r"\s?\d+\.\s", history)[1:]
        return splitted

    def parse_move(self, move: str, color: chess.Color) -> PotentiallyIllegalMove:
        if move == "0-0":
            if color == chess.WHITE:
                move = "e1g1"
            else:
                move = "e8g8"
        elif move == "0-0-0":
            if color == chess.WHITE:
                move = "e1c1"
            else:
                move = "e8c8"
        elif move == "resign":
            return "resign"
        elif move == "draw":
            return "draw"
        try:
            parsed_move = self.board.parse_san(move)
        except ValueError:
            parsed_move = chess.Move.from_uci(move)
        if parsed_move not in self.board.legal_moves:
            return False
        return parsed_move

    async def get_board(self) -> aiogram.types.URLInputFile:
        svg = chess.svg.board(
            board=self.board,
            flipped=self.board.turn == chess.BLACK,
            lastmove=self.board.peek() if self.board.move_stack else None,
            check=self.board.king(self.board.turn) if self.board.is_check() else None,
        )
        async with self.session.post(
            "https://okapi.idlerpg.xyz/api/genchess",
            json={"xml": svg},
        ) as r:
            image = await r.text()

        return aiogram.types.URLInputFile(image, filename="board.png")

    async def get_move_from(self, player: aiogram.types.User | None) -> Move:
        if player is None:
            return await self.get_ai_move()

        image = await self.get_board()

        self.msg = await self.message.answer_photo(
            photo=image,
            caption=f"""
<b>Move {self.move_no}: {player.mention_html()}'s turn</b>
Simply type your move. You have 2 minutes to enter a valid move.
I accept normal notation as well as <code>resign</code> or <code>draw</code>.

Examples: <code>g1f3</code>, <code>Nf3</code>, <code>0-0</code> or <code>xe3</code>.
Moves are case-sensitive! Pieces uppercase: <code>N</code>, <code>Q</code> or <code>B</code>, fields
lowercase: <code>a</code>, <code>b</code> or <code>h</code>. Castling is <code>0-0</code> or <code>0-0-0</code>.""",
        )

        def check(msg: aiogram.types.Message) -> bool:
            if not (
                msg.text is not None
                and player is not None  # mypy can't infer this
                and msg.from_user is not None
                and msg.from_user.id == player.id
                and msg.chat.id == self.message.chat.id
            ):
                return False
            try:
                return bool(self.parse_move(msg.text, self.colors[player]))
            except ValueError:
                return False

        try:
            move = self.parse_move(
                (await self.messages.wait_for(check, timeout=120)).text or "",
                self.colors[player],
            )
        except asyncio.TimeoutError:
            self.status = GameStatus.resigned(self.colors[player])
            await self.message.answer("You entered no valid move! You lost!")
            move = "timeout"

        if self.enemy is not None or self.colors[self.player] == chess.BLACK:
            await self.msg.delete()
            self.msg = None

        # Move is checked for validity in the check function
        assert move is not False

        return move

    async def get_ai_move(self) -> Move:
        text = f"<b>Move {self.move_no}</b>\nLet me think... This might take up to 2 minutes"

        if self.msg:
            await self.msg.delete()

        self.msg = await self.message.answer(text)

        try:
            async with asyncio.timeout(120):
                played = await self.engine.play(self.board, self.limit)
        except asyncio.TimeoutError:
            move = random.choice(list(self.board.legal_moves))
            await self.msg.delete()
            return move

        await self.msg.delete()

        if played.draw_offered:
            return "draw"
        elif played.resigned:
            return "resign"
        elif played.move:
            return played.move
        else:
            return random.choice(list(self.board.legal_moves))

    def make_move(self, move: chess.Move) -> None:
        self.board.push(move)

    async def get_ai_draw_response(self) -> bool:
        msg = await self.message.answer("Waiting for AI draw response...")

        try:
            async with asyncio.timeout(120):
                move = await self.engine.play(self.board, self.limit, draw_offered=True)
        except asyncio.TimeoutError:
            await msg.delete()
            return False

        await msg.delete()
        return move.draw_offered

    async def get_player_draw_response(self, player: aiogram.types.User) -> bool:
        buttons = [
            aiogram.types.InlineKeyboardButton(text="Yes", callback_data=random_id()),
            aiogram.types.InlineKeyboardButton(text="No", callback_data=random_id()),
        ]

        confirm_msg = await self.message.answer(
            f"Your enemy has proposed a draw, {player.mention_html()}. Do you agree?",
            reply_markup=aiogram.types.InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

        player_response = await wait_for_any_button(buttons, user=player)

        await confirm_msg.delete()

        # mypy can't infer returning player_response == "Yes"
        if player_response == "Yes":
            return True
        else:
            return False

    async def update_player_elos(
        self,
        white: aiogram.types.User,
        black: aiogram.types.User,
        outcome: str,
        pgn: str,
    ) -> None:
        """
        Updates Chess elos for 2 players after a match based on
        their old elos and the match outcome.
        Score shall be 1 if player 1 won, 0.5 if it's a tie and 0
        if player 2 won.

        newrating = oldrating + K ⋅(score − expectedscore)

        Here K is the K-factor, which is the weight of the game. This is determined as following:

        - It is 40 if the number of played games is smaller than 30.
        - It is 20 if the number of played games is greater than 30 and the rating is less than 2400.
        - It is 10 if the number of played games is greater than 30 and the rating is more than 2400.
        - It is 40 if rating is less than 2300 and the age of the player is less than 18. We assume that everyone is 18+.

        The expected score is:
        Ea = 1 / (1 + 10 ^ ((Rb - Ra) / 400))
        """
        async with self.pool.acquire() as conn:
            white_player = await conn.fetchrow(
                'SELECT * FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
                white.id,
                self.message.chat.id,
            )
            black_player = await conn.fetchrow(
                'SELECT * FROM chess_players WHERE "user"=$1 AND "chat"=$2;',
                black.id,
                self.message.chat.id,
            )

            if white_player is None or black_player is None:
                return

            num_matches_white = await conn.fetchval(
                'SELECT COUNT(*) FROM chess_matches WHERE "white"=$1 OR "black"=$1;',
                white_player["id"],
            )
            num_matches_black = await conn.fetchval(
                'SELECT COUNT(*) FROM chess_matches WHERE "white"=$1 OR "black"=$1;',
                black_player["id"],
            )

            if num_matches_white < 30:
                k_1 = 40
            elif white_player["elo"] < 2400:
                k_1 = 20
            else:
                k_1 = 10

            if num_matches_black < 30:
                k_2 = 40
            elif black_player["elo"] < 2400:
                k_2 = 20
            else:
                k_2 = 10

            if outcome == "1-0":
                score = 1.0
                winner_id = white_player["id"]
            elif outcome == "1/2-1/2":
                score = 0.5
                winner_id = None
            else:
                score = 0.0
                winner_id = black_player["id"]

            expected_score_white = 1 / (
                1 + 10 ** ((black_player["elo"] - white_player["elo"]) / 400)
            )
            expected_score_black = 1 / (
                1 + 10 ** ((white_player["elo"] - black_player["elo"]) / 400)
            )

            new_rating_white = round(
                white_player["elo"] + k_1 * (score - expected_score_white)
            )
            new_rating_black = round(
                black_player["elo"] + k_2 * (1 - score - expected_score_black)
            )

            await conn.execute(
                'UPDATE chess_players SET "elo"=$1 WHERE "id"=$2;',
                new_rating_white,
                white_player["id"],
            )
            await conn.execute(
                'UPDATE chess_players SET "elo"=$1 WHERE "id"=$2;',
                new_rating_black,
                black_player["id"],
            )

            await self.pool.execute(
                'INSERT INTO chess_matches ("white", "black", "result", "pgn", "winner") VALUES ($1, $2, $3, $4, $5);',
                white_player["id"],
                black_player["id"],
                outcome,
                pgn,
                winner_id,
            )

    async def run(self) -> None:
        self.status = GameStatus.Playing
        white, black = reversed(sorted(self.colors, key=lambda x: self.colors[x]))
        current = white

        while not self.board.is_game_over() and self.status == GameStatus.Playing:
            self.move_no += 1
            move = await self.get_move_from(current)
            if move == "resign":
                self.status = GameStatus.resigned(self.colors[current])
                break
            elif move == "timeout":
                break
            elif move == "draw":
                if self.enemy is None and current is not None:  # player offered AI
                    draw_accepted = await self.get_ai_draw_response()
                else:  # AI offered player or player offered player
                    opponent_player = black if current == white else white
                    assert opponent_player is not None
                    draw_accepted = await self.get_player_draw_response(opponent_player)
                if draw_accepted:
                    self.status = GameStatus.Draw
                else:
                    if self.msg and self.enemy is None and current is not None:
                        await self.msg.delete()
                    await self.message.answer("The draw was rejected.")
                    self.move_no -= 1
                    continue
            else:
                self.make_move(move)
                if self.history and len(self.history[-1]) == 1:
                    self.history[-1].append(move)
                else:
                    self.history.append([move])

            # swap current player
            current = black if current == white else white

        next_ = black if current == white else white
        if self.status == GameStatus.Draw:
            result = "1/2-1/2"
        elif self.status.is_resigned:
            result = "1-0" if next_ == white else "0-1"
        else:
            result = self.board.result()
        image = await self.get_board()
        game = chess.pgn.Game.from_board(self.board)
        game.headers["Event"] = "ChessGram"
        game.headers["Site"] = self.message.chat.full_name
        game.headers["Date"] = datetime.date.today().isoformat()
        game.headers["Round"] = "1"
        game.headers["White"] = str(white) if white is not None else "AI"
        game.headers["Black"] = str(black) if black is not None else "AI"
        game.headers["Result"] = result
        pgn = f"{game}\n\n"

        if self.rated:
            assert white is not None and black is not None
            await self.update_player_elos(white, black, result, pgn)

        if self.board.is_checkmate():
            await self.message.answer_photo(
                image, caption=f"<b>Checkmate! {result}</b>"
            )
        elif self.board.is_stalemate():
            await self.message.answer_photo(
                image, caption=f"<b>Stalemate! {result}</b>"
            )
        elif self.board.is_insufficient_material():
            await self.message.answer_photo(
                image, caption=f"<b>Insufficient material! {result}</b>"
            )
        elif self.status.is_resigned:
            if self.status == GameStatus.BlackResigned:
                outcome = "Black resigned"
            else:
                outcome = "White resigned"
            await self.message.answer_photo(
                image, caption=f"<b>{outcome}! {result}</b>"
            )
        elif self.status == GameStatus.Draw:
            await self.message.answer_photo(
                image, caption=f"<b>Draw accepted! {result}</b>"
            )

        pgn_file = aiogram.types.BufferedInputFile(pgn.encode(), filename="match.pgn")
        await self.message.answer_document(pgn_file, caption="For the nerds.")
