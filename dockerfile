# -------- base image --------
FROM python:3.8.10-slim

# -------- runtime env --------
WORKDIR /stock_rsi_rsi_7_cross_14_strat
# -u equivalent
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC

# -------- dependencies --------
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc build-essential && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y build-essential gcc && \
    apt-get autoremove -y && apt-get clean && rm -rf /var/lib/apt/lists/*

# -------- code --------
COPY stock_rsi_rsi_7_cross_14_strat/LIVE_BOT_rsi_stoch_7_ma_cross_14_ma_strat.py .

CMD ["python", "LIVE_BOT_rsi_stoch_7_ma_cross_14_ma_strat.py"]
