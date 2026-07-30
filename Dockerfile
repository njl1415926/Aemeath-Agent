# syntax=docker/dockerfile:1.7

FROM python:3.10.20-slim-bookworm@sha256:5cc3381b18ec240e4462510648ed7970917ac8eff4c3463320429fec285491be AS dependencies

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}"
COPY requirements.deploy.lock /tmp/requirements.deploy.lock
RUN python -m pip install \
        --no-compile \
        --only-binary=:all: \
        --require-hashes \
        --requirement /tmp/requirements.deploy.lock \
    && python -m pip uninstall --yes pip setuptools

FROM python:3.10.20-slim-bookworm@sha256:5cc3381b18ec240e4462510648ed7970917ac8eff4c3463320429fec285491be AS runtime

ARG BUILD_COMMIT=unknown

LABEL org.opencontainers.image.title="Aemeath Agent API" \
      org.opencontainers.image.revision="${BUILD_COMMIT}" \
      org.opencontainers.image.source="Aemeath-Agent"

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m pip uninstall --yes pip setuptools wheel \
    && groupadd --gid 10001 aemeath \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aemeath \
    && mkdir -p /opt/aemeath /var/lib/aemeath/data /var/log/aemeath/app /var/log/aemeath/trace /var/log/aemeath/prompt \
    && chown -R 10001:10001 /opt/aemeath /var/lib/aemeath /var/log/aemeath

COPY --from=dependencies /opt/venv /opt/venv
COPY --chown=10001:10001 app /opt/aemeath/app
COPY --chown=10001:10001 scripts/deployment /opt/aemeath/scripts/deployment

WORKDIR /opt/aemeath
USER 10001:10001

EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).read(1)"]

CMD ["python", "/opt/aemeath/scripts/deployment/start_api.py"]
