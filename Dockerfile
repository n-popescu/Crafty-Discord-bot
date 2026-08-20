# Optional container image. On a Raspberry Pi Zero W, running the bot directly
# under systemd (see README) uses noticeably less memory than Docker.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "bot"]
