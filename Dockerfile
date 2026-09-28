# syntax=docker/dockerfile:1.7
#
# VoiceRT engine bridge -- the process a Unity NPC opens a TCP session to.
#
# Base image: python:3.12-slim-bookworm.
#   * The core has zero dependencies, so the image is CPython plus a ~40 KB
#     package. `slim` gets that in ~150 MB against ~1 GB for the full image.
#   * NOT alpine: the same Dockerfile builds the local-model variant via
#     --build-arg VOICERT_EXTRAS="[local]", and musl has no manylinux wheels
#     for ctranslate2 / sherpa-onnx / onnxruntime -- alpine turns a 30-second
#     build into a C++ toolchain build, or a failure.
#   * NOT distroless: no pip, and it pins its own interpreter minor version.
#     The test stage and the healthcheck both want an ordinary interpreter.
# Pin by digest (python:3.12-slim-bookworm@sha256:...) once you care about
# byte-identical rebuilds on the second machine.

ARG PYTHON_VERSION=3.12
ARG VOICERT_EXTRAS=""

# ---------------------------------------------------------------- builder ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
ARG VOICERT_EXTRAS

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# README.md is not decoration: pyproject.toml declares readme = "README.md",
# so excluding it from the build context breaks `pip install .`.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".${VOICERT_EXTRAS}"

# ------------------------------------------------------------------- test ---
# docker build --target test -t voicert-bridge:test .
FROM builder AS test
RUN pip install --no-cache-dir ".[dev]"
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q"]

# ---------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG VOICERT_EXTRAS

LABEL org.opencontainers.image.title="voicert-bridge" \
      org.opencontainers.image.description="VoiceRT engine bridge: TCP, one socket per live NPC, PCM16 mono 16 kHz" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/voicert \
    VOICERT_BIND_HOST=0.0.0.0 \
    VOICERT_PORT=8765 \
    VOICERT_MAX_SESSIONS=8 \
    VOICERT_SHUTDOWN_GRACE=5 \
    HF_HOME=/models/huggingface \
    XDG_CACHE_HOME=/models/.cache

# libgomp1 is only needed by the local-model extras (ctranslate2 / onnxruntime).
# Creating /models here, owned by uid 10001, is what makes the named volume
# writable by the non-root user: Docker copies the mountpoint's ownership onto
# a fresh named volume.
RUN set -eux; \
    if [ -n "$VOICERT_EXTRAS" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends libgomp1; \
        apt-get clean; \
        find /var/lib/apt/lists -mindepth 1 -delete; \
    fi; \
    groupadd --system --gid 10001 voicert; \
    useradd --system --uid 10001 --gid 10001 --home-dir /home/voicert --create-home voicert; \
    install -d -o 10001 -g 10001 /models /opt/voicert

COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.py docker/healthcheck.py docker/local_factory.py /opt/voicert/

WORKDIR /opt/voicert
USER 10001:10001

EXPOSE 8765
STOPSIGNAL SIGTERM

# A port check proves only that a socket was accepted. This completes the
# real handshake -- HELLO in, READY out -- the same thing a Unity NPC does
# on connect, so it exercises Hello.parse, runtime.start() and the writer
# pump. Exec form: no shell involved.
HEALTHCHECK --interval=30s --timeout=6s --start-period=10s --retries=3 \
    CMD ["python", "/opt/voicert/healthcheck.py"]

# Exec form so python is PID 1 and receives the signals the entrypoint
# installs handlers for. See docker/entrypoint.py for why that matters.
ENTRYPOINT ["python", "-u", "/opt/voicert/entrypoint.py"]
