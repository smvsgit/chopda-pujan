FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pin the runtime identity so the QNAP NFS bind can be prepared with a known
# numeric UID/GID instead of relying on distro useradd defaults.
RUN groupadd --gid "${APP_GID}" smvs \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /bin/bash smvs \
    && mkdir -p /app/media \
    && chmod +x entrypoint.sh \
    && chown -R "${APP_UID}:${APP_GID}" /app

USER 10001:10001

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:3000/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
