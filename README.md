# Telegram Video Downloader Bot

Production-oriented Telegram bot for downloading public media supported by
`yt-dlp`. For an ordinary user the workflow is intentionally simple:

```text
send a URL -> receive the video
```

No onboarding, language choice, quality prompt, cache channel, or user setting
is required before the first download. `/settings` is optional customization;
`/admin` is an operational control center.

## Capabilities

- universal `yt-dlp` extraction (YouTube, Instagram, TikTok, Facebook,
  X/Twitter, Vimeo, VK, Odnoklassniki and other public extractors);
- Auto, 360p, 480p, 720p, 1080p, 1440p and 2160p with metadata-based size
  selection and FFmpeg video/audio merge;
- persistent user and group defaults;
- Russian, Kazakh, modern Uzbek Latin, Uzbek Cyrillic and English user UI;
- fair per-user queue, configurable concurrency, cancellation, bounded retry
  and in-process coalescing of identical content/quality work;
- invisible exact-quality Telegram media cache with primary/backup channels;
- square Telegram video notes for eligible videos up to 60 seconds;
- batch URLs, moderation, broadcasts, inline-button management, audit log,
  operational alerts and system health;
- additive SQLite migrations, WAL mode, retention and crash cleanup;
- optional self-hosted Telegram Bot API for large uploads.

Private/login-only media, paywalls, CAPTCHA and protected access are not
bypassed. Playlists are not bulk-downloaded: one submitted URL produces one
media job.

## Architecture

```text
Telegram polling
  -> Tracking + access middleware
  -> user/admin routers
  -> fair DownloadQueue (replaceable boundary for a future external queue)
  -> one metadata extraction
  -> exact content_id + effective quality lookup
  -> yt-dlp + FFmpeg preparation / in-flight coalescing
  -> cache-channel write with primary/backup failover
  -> direct Telegram delivery
```

Important module boundaries:

- `app/bot.py`: composition, migrations, commands, workers and shutdown;
- `app/config.py`: bootstrap/secrets configuration only;
- `app/runtime_config.py`: validated persistent operational settings;
- `app/handlers/`: localized user routes;
- `app/services/queue.py`: fair in-process queue abstraction;
- `app/services/downloader.py`: yt-dlp metadata/download boundary;
- `app/services/storage.py`: Telegram cache index and storage failover;
- `worker/worker.py`: one download-job lifecycle;
- `admin/`: role-protected control center, broadcast and button manager;
- `app/database.py`: ordered, non-destructive schema migrations.

SQLite and the in-process queue are appropriate for the current single-host
deployment. The service boundaries allow a later move to PostgreSQL/Redis and
separate download workers; this release does not claim SQLite alone is a
500,000-user distributed architecture.

## Requirements

- Ubuntu 24.04 or another Docker host;
- Docker Engine with Compose v2;
- a Telegram bot token from BotFather;
- at least one private Telegram channel only if persistent caching is wanted;
- Telegram `api_id` and `api_hash` only for optional Local Bot API mode.

The image installs Python 3.12, FFmpeg, Deno 2.9.7, `yt-dlp`, `procps` and the
Python dependencies. `procps` is required by the container healthcheck.

## Configuration

Copy `.env.example` to `.env`. `.env` is ignored by Git and Docker build
context.

```bash
cd /root/video-downloader-bot
cp .env.example .env
chmod 600 .env
```

The only mandatory value is `BOT_TOKEN`. `ADMIN_IDS` is strongly recommended
because bootstrap owners cannot be disabled or deleted in the UI.

Bootstrap/secrets settings remain in `.env`:

- `BOT_TOKEN`, `ADMIN_IDS`, `ADMIN_GROUP_ID`;
- `DATA_DIR`, `DATABASE_PATH`, `TEMP_DIR`;
- `TELEGRAM_API_BASE_URL`, `TELEGRAM_API_IS_LOCAL`,
  `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`;
- `LOG_LEVEL`, `APP_VERSION`;
- legacy `CACHE_SMALL/MEDIUM/LARGE/ADULT` channel IDs for one-time import.

Runtime defaults in `.env.example` seed SQLite only when a key does not yet
exist. Afterwards use `/admin -> Settings`; changing the old environment value
does not silently overwrite the persisted setting. Settings marked with `↻`
honestly require a restart.

An existing production `.env` remains usable. The obsolete
`MAX_FILESIZE_MB=45` may remain but is ignored. New variables are optional and
have documented defaults.

## First start (Cloud Bot API)

Cloud mode needs no cache channel and no Local Bot API credentials.

```bash
cd /root/video-downloader-bot
mkdir -p data
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
```

The bot migrates `./data/bot.db` in place, registers `/start` and `/settings`
for users and `/admin` in authorized private-chat scopes, starts conservative
defaults of two queue workers/two concurrent downloads, and begins polling.

## Telegram file limits and Local Bot API

The Telegram cloud Bot API accepts bot uploads up to 50 MB. This project uses a
49 MB safe application limit. It does not advertise 2 GB while using the cloud
transport.

The self-hosted Telegram Bot API supports uploads up to 2,000 MB. Local mode
uses a 1,900 MB safe application limit. It is optional because it needs
credentials from `my.telegram.org` and additional disk/operational capacity.

To enable it, set:

```dotenv
TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081
TELEGRAM_API_IS_LOCAL=true
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
MAX_DELIVERY_MB=1900
```

Then start the profile:

```bash
docker compose --profile local-api config --quiet
docker compose --profile local-api up -d --build
docker compose --profile local-api ps
```

`./data/telegram-bot-api` is persistent. A file can still be rejected when the
selected transport limit, source estimate, resulting merged file size, free
disk reserve, or Telegram media rules do not permit delivery. Cache failure and
delivery-size failure are handled independently.

## Cache channels

SQLite stores only cache metadata. Media copies live in private Telegram
channels. Cache is optional: if every storage is down, the bot still directly
delivers a file when the transport permits it.

1. Create a private channel and add the bot as administrator with permission to
   post messages.
2. Open `/admin -> Storage -> Add`.
3. Choose `SMALL`, `MEDIUM`, `LARGE` or `ADULT`, then primary/backup.
4. Forward a channel message or enter the numeric channel ID.
5. The bot performs a real permission/write probe before activation.

Default tiers are `<500 MB`, `500 MB–1 GB`, and `>1 GB`; boundaries are runtime
settings. They are mostly relevant in Local Bot API mode. Replacing/deleting a
storage configuration does not delete historical entries: valid Telegram
`file_id` references remain usable. Definitively dead references are deleted
and replaced after a fresh download; temporary Telegram/network errors never
delete them.

`ADULT` is deliberately marked inactive for automatic routing. No unreliable
keyword classifier pretends to identify adult content. Public adult-compatible
yt-dlp extractors are not artificially blocked.

## Administration

`/admin` contains working, role-protected sections for dashboard, downloads,
users, groups, moderation, storage, broadcasts, inline buttons, runtime
settings, system health, audit log and administrators. Visibility is not used
as authorization: every sensitive callback and FSM completion rechecks the
role server-side.

- `owner`: all permissions, including administrator management;
- `admin`: operations except owner-level administrator management;
- `moderator`: dashboard/download/user/group/moderation/log functions;
- `broadcaster`: dashboard and broadcasts.

Owners from `ADMIN_IDS` cannot be removed through Telegram UI. Maintenance mode
stops only new downloads. Operational alerts are deduplicated with cooldown and
can be disabled in runtime settings.

## Database, backup and restore

The persistent database is `./data/bot.db`. Connections use foreign keys, WAL,
`busy_timeout=30000` and short transactions. Migrations are recorded in
`schema_migrations`; existing users, groups, admins, cache, broadcasts, buttons,
moderation and statistics are preserved.

Create a consistent backup with SQLite's backup API (safe even with WAL):

```bash
cd /root/video-downloader-bot
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
python3 -c "import sqlite3; s=sqlite3.connect('data/bot.db'); d=sqlite3.connect('backups/bot.db.$STAMP'); s.backup(d); d.close(); s.close()"
cp -a .env "backups/env.$STAMP"
```

For a restore, stop the bot first, preserve the failed database under another
name, copy the backup to `data/bot.db`, then rebuild/start the matching code.
Never restore only a `-wal` file.

## Exact upgrade procedure for `/root/video-downloader-bot`

The following assumes the delivered archive is
`/root/video-downloader-bot-production.zip` and contains the top-level
`video-downloader-bot/` directory.

```bash
sudo apt-get update
sudo apt-get install -y unzip rsync python3

APP=/root/video-downloader-bot
RELEASE=/root/video-downloader-bot-production.zip
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/root/video-downloader-bot-backup-$STAMP
STAGE=/root/video-downloader-bot-stage-$STAMP
mkdir -p "$BACKUP/code" "$STAGE"

cd "$APP"
docker compose stop bot
rsync -a --exclude '.env' --exclude 'data/' "$APP/" "$BACKUP/code/"
cp -a "$APP/.env" "$BACKUP/.env"
python3 -c "import sqlite3; s=sqlite3.connect('$APP/data/bot.db'); d=sqlite3.connect('$BACKUP/bot.db'); s.backup(d); d.close(); s.close()"

unzip -q "$RELEASE" -d "$STAGE"
test -f "$STAGE/video-downloader-bot/docker-compose.yml"
rsync -a --delete --exclude '.env' --exclude 'data/' \
  "$STAGE/video-downloader-bot/" "$APP/"

cd "$APP"
docker compose config --quiet
docker compose build --pull
docker compose run --rm --no-deps bot python -c "from app.config import load_bootstrap_config; from app.database import set_database_path,migrate; c=load_bootstrap_config(); set_database_path(c.database_path); print('applied migrations:', migrate())"
docker compose up -d
docker compose ps
docker compose logs --tail=150 bot
```

For Local Bot API mode, replace the final two Compose commands with:

```bash
docker compose --profile local-api up -d
docker compose --profile local-api ps
```

Do not remove or recreate `.env` or `data/bot.db`. The explicit pre-start
migration is optional but makes migration failure visible before polling; the
normal startup also runs the same idempotent migrations.

### Post-upgrade checks

```bash
cd /root/video-downloader-bot
docker compose ps
docker compose exec -T bot test -f /app/data/health.ready
docker compose exec -T bot ffmpeg -version
docker compose exec -T bot deno --version
docker compose exec -T bot yt-dlp --version
docker compose logs --tail=200 bot
```

Then manually verify `/start`, `/settings`, `/admin` as an owner, one known
public URL, and the configured storage test. A real Telegram/YouTube smoke test
requires production credentials and network access and cannot be replaced by a
compile check.

## Exact rollback procedure

Use the `BACKUP` directory printed/created during the upgrade. The full DB
rollback discards data written after that backup, so first preserve the failed
database for forensic/recovery use.

```bash
APP=/root/video-downloader-bot
BACKUP=/root/video-downloader-bot-backup-YYYYMMDDTHHMMSSZ
FAILED_STAMP=$(date -u +%Y%m%dT%H%M%SZ)

cd "$APP"
docker compose --profile local-api down
mv "$APP/data/bot.db" "$APP/data/bot.db.failed-$FAILED_STAMP"
for sidecar in "$APP/data/bot.db-wal" "$APP/data/bot.db-shm"; do
  if [ -e "$sidecar" ]; then mv "$sidecar" "$sidecar.failed-$FAILED_STAMP"; fi
done
cp -a "$BACKUP/bot.db" "$APP/data/bot.db"
cp -a "$BACKUP/.env" "$APP/.env"
rsync -a --delete --exclude '.env' --exclude 'data/' "$BACKUP/code/" "$APP/"

cd "$APP"
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=150 bot
```

If the old deployment used Local Bot API, start the profile instead. The
`bot.db.failed-*` file and sidecars make this rollback recoverable rather than
destructive.

## Health, logs and common failures

```bash
docker compose ps
docker compose logs -f --tail=200 bot
docker compose restart bot
```

The bot healthcheck requires both the Python process and a readiness timestamp
updated by internal maintenance. `/admin -> System` caches expensive version
checks for 60 seconds and reports polling, workers, queue, SQLite quick-check,
disk, temp, yt-dlp, FFmpeg, Deno, Telegram mode and storage health.

Common causes:

- `ConfigurationError: BOT_TOKEN is required`: set a valid token in `.env`;
- unhealthy bot: inspect logs and confirm `./data` is writable and disk reserve
  remains available;
- storage red: re-add the bot as channel administrator and run `Test`;
- Local Bot API unavailable: verify the profile, API ID/hash and service logs;
- media unavailable/private: this is a source/access restriction, not a reason
  to add cookies or bypass protection;
- YouTube extractor/challenge regression: rebuild with current `yt-dlp` while
  retaining Deno, then verify `deno --version` inside the image.

## Development and validation

```bash
python -m compileall -q app admin worker tests bot.py
python -m pip install -r requirements-dev.txt
pytest -q
docker compose config --quiet
docker compose --profile local-api config --quiet
docker compose build
```

Tests use temporary SQLite databases and mocked Telegram/yt-dlp paths. Live
Telegram delivery, source extractors and channel permissions still require an
operator smoke test with real credentials.

## Privacy and security notes

Only operationally necessary Telegram profile fields, settings, counters and
job/cache metadata are stored. Large media is never stored in SQLite. Ordinary
users never see cache hits, channels, file IDs, server paths or downloader
internals. Event/download history has configurable retention; useful cache
metadata is not deleted by log retention.

Submitted URLs must be public HTTP(S), cannot contain credentials, and are
checked against private/link-local/reserved networks before extraction.
Extracted media/CDN origins are checked again before download. This is a
best-effort SSRF boundary around a universal third-party extractor, not a claim
that arbitrary untrusted extractor code is a security sandbox.
