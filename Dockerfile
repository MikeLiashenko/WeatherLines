# Для запуска на сервере:
#   docker build -t weatherlines .
#   docker run -d --name weatherlines --restart unless-stopped --env-file .env -v weatherlines-data:/app/var weatherlines
FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY weatherlines weatherlines
COPY data data
ENV VAR_DIR=/app/var

CMD ["python", "-m", "weatherlines"]
