FROM python:3.12-slim

WORKDIR /app

COPY app.py subtitles.py ./
COPY static/ ./static/

ENV PORT=8000 \
    DB_PATH=/data/app.db

RUN mkdir -p /data

EXPOSE 8000

# 启动时自动初始化数据库（CREATE TABLE IF NOT EXISTS）
CMD ["python", "app.py"]
