FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据库文件目录（容器内默认 /data/coldchain.db，可挂卷持久化）
RUN mkdir -p /data
ENV DB_PATH=/data/coldchain.db \
    TEMP_LIMIT=8.0

EXPOSE 8000

# 启动即自动完成数据库初始化（建表，幂等）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
