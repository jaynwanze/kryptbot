# -------- base image --------
FROM python:3.8.10-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC
ENV PYTHONPATH="/app/stock_rsi_rsi_7_cross_14_strat:${PYTHONPATH}"

COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc build-essential \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y build-essential gcc \
 && apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . /app

CMD ["python", "-m", "stock_rsi_rsi_7_cross_14_strat.LIVE_BOT_rsi_stoch_7_ma_cross_14_ma_strat"]
