FROM python:3.12-slim

WORKDIR /app
COPY app.py subtitle.py store.py ./

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/data/subtitles.db

VOLUME /data
EXPOSE 8000

# 应用启动时自动初始化数据库（建库建表）
CMD ["python", "app.py"]
