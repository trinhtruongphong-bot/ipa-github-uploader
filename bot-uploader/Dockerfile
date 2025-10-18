FROM python:3.11-slim

WORKDIR /app

# Cài dependencies
COPY bot-uploader/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy đúng file đã sửa
COPY bot-uploader/upload_to_github_bot.py /app/upload_to_github_bot.py

# Render tự set PORT; app bind 0.0.0.0:$PORT trong file .py
CMD ["python", "/app/upload_to_github_bot.py"]
