FROM python:3.11-slim

WORKDIR /app
COPY upload_to_github_bot.py /app

RUN pip install --no-cache-dir python-telegram-bot==21.6 httpx==0.27.2 requests==2.32.3

EXPOSE 8080
CMD ["python", "upload_to_github_bot.py"]
