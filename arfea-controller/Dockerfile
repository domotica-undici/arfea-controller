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
# uvloop e httptools (gli extra "standard" di uvicorn) solo dove hanno le wheel: su
# arm a 32 bit andrebbero compilati, e la slim non ha il compilatore (Redmine #403).
# Conta l'architettura dei pacchetti dell'immagine: su un kernel a 64 bit anche
# un'immagine armhf vede "aarch64" da uname e da platform.machine().
RUN pip install --no-cache-dir -r requirements.txt \
 && case "$(dpkg --print-architecture)" in \
      amd64|arm64) pip install --no-cache-dir "uvloop>=0.14.0,!=0.15.0,!=0.15.1" "httptools>=0.5.0" ;; \
    esac

COPY app/ ./app/

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8888/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--log-level", "info"]
