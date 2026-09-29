FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data \
    WORKER_POLL_INTERVAL=0.2

EXPOSE 8000

# Default command runs the API; the worker service overrides it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
