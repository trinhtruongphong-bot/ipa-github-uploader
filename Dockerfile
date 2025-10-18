FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY upload_to_github_bot.py /app/upload_to_github_bot.py

EXPOSE 10000
CMD ["python", "/app/upload_to_github_bot.py"]
