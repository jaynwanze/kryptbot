# -------- base image --------
FROM python:3.10-slim-bullseye

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC
ENV PYTHONPATH="/app/bot:${PYTHONPATH}"

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . /app
CMD ["python", "-m", "bot.liquidity_raid_scalper"]
