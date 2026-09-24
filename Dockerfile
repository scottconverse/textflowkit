# syntax=docker/dockerfile:1
#
# Optional Linux deployment surface for the JSON HTTP adapter. The product stays
# native Windows; nothing here is needed for a local install, and the Compose
# file beside this one is the supported way to run it.
#
# What this image does NOT provide, and cannot:
#
#   * A public service. `compose.yaml` publishes the host port to 127.0.0.1 only,
#     so the container is reachable from its own host and nowhere else.
#     Terminating TLS, authenticating callers beyond the Bearer token, and
#     enforcing an independent rate and egress policy belong to a trusted gateway
#     in front of it. The built-in limiter is per process, not a distributed
#     quota.
#   * A working URL-job path on its own. Production URL acquisition requires an
#     SSRF-filtering TEXTFLOWKIT_EGRESS_PROXY and refuses to run without one, so
#     URL jobs fail closed until an operator supplies a proxy that blocks
#     private/loopback destinations and DNS rebinding. No proxy is bundled. Local
#     files and /health need no egress.
#   * More than one process. The durable SQLite store has one owning process; a
#     second one sharing TEXTFLOWKIT_DB would reap the first one's live jobs.
#
# The token is deliberately not here. The server fails at startup with a named
# error when TEXTFLOWKIT_API_TOKEN is missing, which is better than an
# image-embedded default that every puller would share.

# Pinned to a Python the CI matrix covers (3.10-3.13, linted on 3.12) and to a
# named Debian release rather than to `latest`, which would move under a rebuild.
# Not a digest pin: resolving a digest needs a registry lookup that could not be
# performed where this was written, and an invented one would be worse than an
# honest tag.
FROM python:3.12-slim-bookworm

# ffmpeg is required in every profile: decode, and the ffprobe duration bound the
# production profile enforces before a full decode.
# ca-certificates is the trust store URL acquisition needs.
# libstdc++6 is installed unconditionally so the Node binary copied below has a
# C++ runtime to link against whatever the slim base happens to carry; the gate
# below would fail the build if that binary could not run at all.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg libstdc++6; \
    rm -rf /var/lib/apt/lists/*

# The JavaScript runtime yt-dlp uses to solve some YouTube player challenges.
# It comes from the official Node image rather than from the base distribution,
# because the installed yt-dlp requires Node >= 22 (yt_dlp/utils/_jsruntime.py,
# `NodeJsRuntime.MIN_SUPPORTED_VERSION`) and the version the base distribution
# carries was not measured here. Taking the runtime from a tag that names the
# major version does not by itself prove the floor either, which is why the gate
# below runs the binary and fails the build rather than trusting a tag name.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

# yt-dlp's Node floor, taken from the installed package and re-checked against it
# by tests/test_container_example.py, so a yt-dlp upgrade that raises the floor
# fails the suite rather than only the build.
ARG NODE_MIN_MAJOR=22
RUN set -eux; \
    node --version; \
    node -e "const v = parseInt(process.versions.node, 10); if (v < ${NODE_MIN_MAJOR}) { console.error('node ' + process.versions.node + ' is below the yt-dlp Node floor of ${NODE_MIN_MAJOR}.0.0 (yt_dlp/utils/_jsruntime.py); use a newer node image, or raise NODE_MIN_MAJOR deliberately'); process.exit(1) }"; \
    ffmpeg -version | head -n 1; \
    ffprobe -version | head -n 1

# Nonroot runtime. The uid and gid are fixed so the Compose file can name them
# and the volumes below initialize with the right ownership.
RUN set -eux; \
    groupadd --gid 10001 textflowkit; \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin textflowkit; \
    install -d -o textflowkit -g textflowkit \
        /srv/textflowkit \
        /srv/textflowkit/input \
        /srv/textflowkit/output \
        /srv/textflowkit/work \
        /srv/textflowkit/state \
        /home/textflowkit/.cache

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The base dependencies are yt-dlp and openai-whisper, so this image is large:
# openai-whisper pulls a torch build. That is the product's own requirement, not
# something this file adds.
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir ".[http]"

# Non-secret defaults only. The roots are the directories created above; Compose
# repeats them so the volumes it mounts land on the paths the service reads.
# Concurrency stays at the documented default of 1: the store has one owner.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TEXTFLOWKIT_PROFILE=production \
    TEXTFLOWKIT_INPUT_ROOT=/srv/textflowkit/input \
    TEXTFLOWKIT_OUTPUT_ROOT=/srv/textflowkit/output \
    TEXTFLOWKIT_WORK_ROOT=/srv/textflowkit/work \
    TEXTFLOWKIT_DB=/srv/textflowkit/state/jobs.db \
    TEXTFLOWKIT_MAX_CONCURRENCY=1

USER 10001:10001

# Liveness only, and it has to carry the Bearer token: the production middleware
# covers /health like every other route. The token is read from the process
# environment, never passed as an argument, so it does not appear in `ps` inside
# the container. A missing token makes this probe fail rather than pass - the
# same fail-closed direction as the server itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; request = urllib.request.Request('http://127.0.0.1:8767/health', headers={'Authorization': 'Bearer ' + os.environ['TEXTFLOWKIT_API_TOKEN']}); body = urllib.request.urlopen(request, timeout=3).read(); assert b'status' in body, body"]

# 0.0.0.0 inside the container, so the published host port reaches the server;
# `--allow-remote` is the explicit opt-in the adapter requires for a bind beyond
# loopback, and the host side of that publication stays loopback-only in Compose.
EXPOSE 8767
CMD ["textflowkit-http", "--host", "0.0.0.0", "--port", "8767", "--allow-remote"]
