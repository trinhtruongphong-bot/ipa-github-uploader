FROM python:3.11-slim

WORKDIR /app

COPY bot-uploader/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot-uploader/upload_to_github_bot.py /app/upload_to_github_bot.py

CMD ["python", "/app/upload_to_github_bot.py"]
