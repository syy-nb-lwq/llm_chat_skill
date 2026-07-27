# Skill Agent - 后端 Dockerfile
FROM python:3.11-slim

# 避免 .pyc,确保 stdout 不缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 单独装依赖,利用层缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

# P0-07: 创建非 root 用户并切换。
# 必须在 COPY 源码之前创建用户,然后把 /app 所有权交给它,避免后面写入文件(EFS/挂载卷)时无权限。
RUN useradd --create-home --shell /bin/bash app \
    && chown -R app:app /app

# 复制源码(以 app 身份)
USER app
COPY --chown=app:app . .

# 默认暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" || exit 1

CMD ["python", "backend/main.py"]
