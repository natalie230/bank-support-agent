# One image for both servers; docker-compose.yml picks the command.
# ponytail: the backend ships streamlit's dependencies it never imports. Split
# into two images when that size starts to matter.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY backend backend
COPY frontend frontend

EXPOSE 8000 8501
CMD ["uvicorn", "main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
