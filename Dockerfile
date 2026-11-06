# 后端：FastAPI + Playwright 自带的 Chromium（导出 PDF 用）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    LLM_BACKEND=ollama \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    OLLAMA_MODEL=qwen2.5:7b-instruct

# 中文字体：PDF 里的中文靠它
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（这一层变化少，能被缓存），再拷代码和数据
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip && pip install . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY data ./data

# 不用 root 跑；行程数据库（plans.db）落在挂载卷里
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data/plans \
    && chown -R app:app /app/data/plans \
    && chmod -R a+rX /ms-playwright
USER app
VOLUME ["/app/data/plans"]
EXPOSE 8137

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8137/health', timeout=4)"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8137"]
