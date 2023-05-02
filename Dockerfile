FROM docker.io/gelbpunkt/python:latest

WORKDIR /bot

COPY pyproject.toml poetry.lock ./

# Due to https://github.com/python-poetry/issues/6925 we have to use a patched version
RUN set -ex && \
    apk add --no-cache --virtual .deps gcc musl-dev libffi-dev git findutils && \
    pip install --no-cache-dir git+https://github.com/GeorgeSaussy/poetry@master && \
    poetry source add idle https://packages.travitia.xyz/root/idle/+simple/ && \
    poetry add --lock aiogram==3.0.0b8 asyncpg==0.28.0.dev0 && \
    poetry update --lock --only main && \
    poetry install --only main && \
    poetry cache list | xargs -L 1 poetry cache clear --all && \
    apk del .deps && \
    apk add --no-cache libgcc

COPY . .

CMD ["poetry", "run", "bot"]
