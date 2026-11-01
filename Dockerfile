FROM python:3.12-slim

# Proton's `proton-drive` CLI is deliberately NOT in this image. Mount the Linux binary at
# /opt/proton/proton-drive (read-only) from a host folder, so the image contains nothing of
# Proton's, can be public, and the CLI can be updated without a rebuild. The CLI is a standalone
# binary that needs only glibc, which this base image has.

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# Versions the test suite ran against.
RUN pip install --no-cache-dir "fastapi==0.162.0" "uvicorn[standard]==0.54.0" "croniter==6.2.4" "python-multipart==0.0.33"
COPY app ./app

# 568 matches the owner of /mnt/Storage/Application/Arcane/* on bender.
ARG UID=568
RUN useradd -u ${UID} -M -d /data/home runner
USER runner

ENV DATA_DIR=/data HOME=/data/home PORT=8080 PYTHONUNBUFFERED=1 \
    PROTON_CLI_BIN=/opt/proton/proton-drive
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD curl -fs http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-m", "app.main"]
