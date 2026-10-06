FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    YFINANCE_CACHE_DIR=/tmp/macro-pulse-yfinance \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver \
    PATH="/opt/venv/bin:/root/.local/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --all-groups --no-install-project

COPY . .

CMD ["uv", "run", "--frozen", "python", "src/render_main.py"]
