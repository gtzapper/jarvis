FROM python:3.14-slim

# /config holds everything Jarvis saves: config.yaml, .env, data/ and its SSH key (~/.ssh).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/config \
    TZ=UTC \
    JARVIS_GENERATE_PASSWORD=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jarvis ./jarvis
COPY config.example.yaml .env.example README.md ./

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin jarvis \
    && mkdir -p /config \
    && chown 1000:1000 /config

USER 1000:1000
WORKDIR /config
VOLUME /config
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/login', timeout=5)" || exit 1

CMD ["python", "-m", "jarvis", "web", "--host", "0.0.0.0", "--port", "8765"]
