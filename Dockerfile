FROM python:3.12-slim

# The official proton-drive CLI is not baked in from a guessed URL. Download the Linux build from
# https://proton.me/drive/cli (or the page it links to), host it somewhere you control, and pass it:
#   docker build --build-arg PROTON_CLI_URL=https://.../proton-drive --build-arg PROTON_CLI_SHA256=<sum> .
# Pin the version. Tested against CLI 0.6.0.
ARG PROTON_CLI_URL
ARG PROTON_CLI_SHA256
ARG UID=1000

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN test -n "$PROTON_CLI_URL" && test -n "$PROTON_CLI_SHA256" \
    && curl -fsSL "$PROTON_CLI_URL" -o /usr/local/bin/proton-drive \
    && echo "$PROTON_CLI_SHA256  /usr/local/bin/proton-drive" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/proton-drive

WORKDIR /srv
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" croniter python-multipart
COPY app ./app

RUN useradd -u ${UID} -m runner && mkdir -p /data && chown runner /data
USER runner
ENV DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-m", "app.main"]
