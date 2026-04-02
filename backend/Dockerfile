FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    uvicorn[standard]==0.30.0 \
    aiosqlite==0.20.0 \
    watchdog==4.0.0 \
    apscheduler==3.10.4 \
    python-multipart==0.0.9 \
    sse-starlette==2.0.0

# 复制 7zzs 二进制（支持更多压缩格式的 7-Zip）
COPY bin/7zzs /usr/local/bin/7zzs
RUN chmod 755 /usr/local/bin/7zzs

# 复制应用代码
COPY main.py ./
COPY frontend/ ./frontend/

EXPOSE 3088

CMD ["python", "main.py"]
