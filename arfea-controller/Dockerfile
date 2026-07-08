FROM --platform=$BUILDPLATFORM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus \
    curl \
    iproute2 \
    util-linux \
    xz-utils \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8888/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--log-level", "info"]
