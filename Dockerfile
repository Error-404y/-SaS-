FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -r requirements.txt && useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
COPY bot.py database.py views.py time_utils.py identity.py delivery.py event_center.py event_graphics.py main.py ./
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
