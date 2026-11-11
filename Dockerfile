# Collab Board — single-container image (stage 4).
# The app is single-process by design (in-memory rooms + SQLite), so
# one service is the whole stack; scaling past one replica would need
# Redis pub/sub for rooms (see README "Scaling").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COLLAB_DB_PATH=/data/collab.db

WORKDIR /app

# Non-root runtime user + the data directory it owns.
RUN groupadd -r app && useradd -r -g app app \
    && mkdir -p /data && chown app:app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
