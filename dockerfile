FROM python:3.11-slim
WORKDIR /stock_rsi_rsi_7_cross_14_strat
COPY kryptbot.py .
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "-u", "kryptbot.py"]
