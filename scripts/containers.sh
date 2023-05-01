#!/bin/bash
podman pull docker://docker.io/library/postgres:15-alpine
podman pull quay.io/gelbpunkt/stockfish:latest

cat <<EOF > start.sh
createdb chess
psql chess -c "CREATE ROLE bot WITH PASSWORD 'owo';"
psql chess -c "ALTER ROLE bot WITH LOGIN;"

psql chess <<'DONE'
CREATE TABLE public.chess_players (
    "id" SERIAL PRIMARY KEY NOT NULL,
    "user" bigint NOT NULL,
    "chat" bigint NOT NULL,
    elo bigint DEFAULT 1000 NOT NULL
);

CREATE INDEX public.chess_players_user_chat_idx ON public.chess_players("user", "chat");

CREATE TABLE public.chess_matches (
    "id" SERIAL PRIMARY KEY NOT NULL,
    white bigint REFERENCES chess_players("id"),
    black bigint REFERENCES chess_players("id"),
    result character varying(7) NOT NULL,
    pgn text NOT NULL,
    winner bigint REFERENCES chess_players("id")
);

CREATE TABLE public.user_lookups (
    "id" bigint UNIQUE NOT NULL,
    "name" character varying(32) UNIQUE NOT NULL
);

ALTER SCHEMA public OWNER TO bot;
ALTER TABLE public.chess_players OWNER TO bot;
ALTER TABLE public.chess_matches OWNER TO bot;
ALTER TABLE public.user_lookups OWNER TO bot;
DONE
EOF

chmod 777 start.sh
podman run --rm -it --net host -d --name postgres -e POSTGRES_PASSWORD="test" -v $(pwd)/start.sh:/docker-entrypoint-initdb.d/init.sh:Z postgres:15-alpine
sleep 15
rm start.sh
podman run --rm -it --net host -d --name stockfish gelbpunkt/stockfish:latest
