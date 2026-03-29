FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

VOLUME /app/data
ENV DB_PATH=/app/data/expenses.db

CMD ["python", "bot.py"]
