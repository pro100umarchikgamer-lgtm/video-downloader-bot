FROM python:3.12-slim

ARG DENO_VERSION=2.9.7
ENV PATH="/usr/local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# procps is intentionally retained: the container healthcheck uses pgrep.
# FFmpeg performs media merges and video-note conversion. Deno is the tested
# JS runtime used by yt-dlp for YouTube challenges.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        nodejs \
        npm \
        procps \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) deno_target="x86_64-unknown-linux-gnu" ;; \
         arm64) deno_target="aarch64-unknown-linux-gnu" ;; \
         *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/deno.zip \
         "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_target}.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm -f /tmp/deno.zip \
    && chmod +x /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import aiogram, yt_dlp" \
    && ffmpeg -version >/dev/null \
    && deno --version \
    && yt-dlp --version

COPY . .
RUN mkdir -p /app/data/tmp \
    && python -m compileall -q /app

CMD ["python", "bot.py"]
