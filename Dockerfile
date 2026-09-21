FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# libpq-dev/gcc are only needed to build psycopg2; drop them from the final
# layer to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --shell /bin/bash smvs \
    && chmod +x entrypoint.sh \
    && chown -R smvs:smvs /app
USER smvs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:3000/healthz || exit 1

ENTRYPOINT ["./entrypoint.sh"]
