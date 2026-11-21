FROM python:3.14-slim

# /app    = Jarvis's code: the host's git checkout is mounted here, so Jarvis can update itself with git pull.
#           (A copy is baked into the image too, so it still runs without the mount, just without self-update.)
# /config = everything Jarvis saves: config.yaml, .env, data/, its SSH key (~/.ssh) and extra packages (.pydeps).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PYTHONUSERBASE=/config/.pydeps \
    HOME=/config \
    TZ=UTC \
    JARVIS_APP_DIR=/app \
    JARVIS_IN_DOCKER=1 \
    JARVIS_GENERATE_PASSWORD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && sha256sum requirements.txt | cut -d' ' -f1 > /opt/requirements.sha

COPY docker/entrypoint.sh /usr/local/bin/jarvis-entrypoint
RUN chmod 755 /usr/local/bin/jarvis-entrypoint

COPY jarvis ./jarvis
COPY config.example.yaml .env.example README.md ./

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin jarvis \
    && mkdir -p /config \
    && chown 1000:1000 /config

USER 1000:1000
WORKDIR /config
VOLUME /config
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/login', timeout=5)" || exit 1

CMD ["jarvis-entrypoint"]
