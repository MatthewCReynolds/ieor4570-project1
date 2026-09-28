# Cloud Run builds this from the GitHub repo on every push to main.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Install dependencies first so they cache separately from the app code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app.py tools.py index.html ./

ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD [".venv/bin/python", "app.py"]
