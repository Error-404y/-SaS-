FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
COPY bot.py database.py ./
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
