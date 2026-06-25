FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY . /app/gateway

# Cloud Run sets $PORT (default 8080).
CMD ["sh", "-c", "uvicorn gateway.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
