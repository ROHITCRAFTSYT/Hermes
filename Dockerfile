# Hermes bridge in a container: the voice pipeline (STT -> agent -> TTS) and web app.
# PC-control tools (open apps, media keys, SAPI voice) need Windows and are no-ops here;
# run the bridge natively on Windows for those. mDNS needs --network host.
#
#   docker build -t hermes-bridge .
#   docker run --rm -p 8787:8787 --env-file bridge/.env -e PLAY_ON_PC=0 hermes-bridge

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOG_FORMAT=json \
    PLAY_ON_PC=0 \
    PORT=8787

WORKDIR /app
COPY bridge/requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 hermes
COPY --chown=hermes:hermes bridge/ .
# devices/memory/timers/conversation JSON live next to the code; never bake secrets in
RUN rm -f .env devices.json memory.json timers.json conversation.json spotify_token.json
USER hermes

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8787\")}/health', timeout=4)"

CMD ["python", "hermes_bridge.py"]
