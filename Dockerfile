FROM debian:13-slim

ARG NEXUS_UID=1000
ARG NEXUS_GID=1000
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 python3-venv python3-pip \
    bubblewrap socat ripgrep nodejs npm libgomp1 ffmpeg \
    libreoffice-writer libreoffice-calc libreoffice-impress poppler-utils \
    fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$NEXUS_GID" nexus \
    && useradd --uid "$NEXUS_UID" --gid "$NEXUS_GID" --create-home --shell /bin/bash nexus \
    && mkdir -p /data /home/nexus/.claude /home/nexus/.codex /home/nexus/.local/bin \
    && chown -R nexus:nexus /data /home/nexus

ENV HOME=/home/nexus \
    PATH=/home/nexus/.local/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    NPM_CONFIG_PREFIX=/home/nexus/.local \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    NEXUS_DATA=/data NEXUS_HOST=0.0.0.0 NEXUS_PORT=8080
WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt
# Only allowlisted source enters the image; never COPY the working directory.
COPY *.py ./
COPY static/ ./static/
COPY tools/soffice ./tools/soffice
COPY docker/ ./docker/
USER nexus:nexus
EXPOSE 8080
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["serve"]
