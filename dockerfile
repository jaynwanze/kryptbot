FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC PYTHONPATH="/app/bot:${PYTHONPATH}"

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt

COPY . /app
