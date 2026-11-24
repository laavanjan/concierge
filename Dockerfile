# syntax=docker/dockerfile:1.7
# One image, many roles: gateway, inventory, pricing, reservations, mcp, worker, ingest (set by the command).

FROM python:3.11-slim AS deps
RUN pip install --no-cache-dir uv==0.8.18
WORKDIR /build
COPY pyproject.toml README.md ./
# Install dependencies only; the code runs from /app so data/ and prompts/ resolve next to it.
RUN uv venv /opt/venv && VIRTUAL_ENV=/opt/venv uv pip install -r pyproject.toml --extra postgres --extra redis-checkpoint

FROM python:3.11-slim
LABEL org.opencontainers.image.title="concierge" \
      org.opencontainers.image.description="Multi-agent reservation assistant (LangGraph, MCP, RAG, HITL)"
RUN useradd --create-home --uid 10001 concierge && mkdir -p /data && chown concierge /data
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    LOG_JSON=true
COPY --from=deps /opt/venv /opt/venv
WORKDIR /app
COPY --chown=concierge . .
USER concierge
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "channels.gateway:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
