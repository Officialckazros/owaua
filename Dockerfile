FROM python:3.12-slim

LABEL org.opencontainers.image.title="Owaua Discord bot"
LABEL org.opencontainers.image.description="Persona-driven Discord hangout bot"
LABEL org.opencontainers.image.source="https://github.com/Officialckazros/owaua"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src/owaua

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY src/owaua ./src/owaua
COPY personas ./personas
COPY pfps ./pfps
COPY banners ./banners

RUN useradd --create-home --shell /usr/sbin/nologin owaua \
    && mkdir -p /app/data \
    && chown -R owaua:owaua /app

USER owaua

VOLUME ["/app/data"]
ENTRYPOINT ["python", "src/owaua/bot.py"]
