# sofabaton-x-server image. Build context is the REPOSITORY ROOT (the
# library and the server are two distributions of one repo):
#
#     docker build -f sofabaton-x-server/Dockerfile -t sofabaton-x-server .
#
# Run with host networking (mDNS, the app's UDP broadcast and the hub's
# TCP dial-back all need the host's real interface; Linux hosts only):
#
#     docker run --network host -v ./data:/data sofabaton-x-server --hub 192.168.1.50
#
# or use the docker-compose.yml next to this file.

# --- build both wheels from the repository ------------------------------
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml LICENSE ./
COPY custom_components/sofabaton_x1s/lib ./custom_components/sofabaton_x1s/lib
COPY sofabaton-x ./sofabaton-x
RUN python -m build --wheel --outdir /wheels
COPY sofabaton-x-server ./sofabaton-x-server
RUN python -m build --wheel --outdir /wheels ./sofabaton-x-server

# --- runtime --------------------------------------------------------------
FROM python:3.12-slim
LABEL org.opencontainers.image.title="sofabaton-x-server" \
      org.opencontainers.image.description="REST + WebSocket server over the sofabaton-x library for Sofabaton X1 / X1S / X2 hubs" \
      org.opencontainers.image.source="https://github.com/m3tac0de/home-assistant-sofabaton-x1s"
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
ENV SOFABATON_DATA_DIR=/data \
    SOFABATON_BIND=0.0.0.0 \
    SOFABATON_PORT=8480 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
# Host networking makes EXPOSE informational; listed for the record:
# API 8480/tcp, hub dial-back 8200/tcp, app discovery 8102/udp, mDNS 5353/udp.
EXPOSE 8480/tcp 8200/tcp 8102/udp 5353/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"SOFABATON_PORT\", \"8480\")}/api/v1/server', timeout=3)" || exit 1
ENTRYPOINT ["sofabaton-x-server"]
