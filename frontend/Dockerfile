# The Streamlit desk on its own: only the `frontend` dependency group, no
# fastapi or psycopg. Built from the repo root so pyproject.toml / uv.lock are visible.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --only-group frontend

COPY frontend frontend

EXPOSE 8501
CMD ["streamlit", "run", "frontend/app.py", "--server.address", "0.0.0.0", "--server.port", "8501",      "--server.headless", "true", "--server.fileWatcherType", "none",      "--client.toolbarMode", "minimal", "--browser.gatherUsageStats", "false"]
