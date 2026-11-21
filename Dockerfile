# Playwright's image ships Chromium and its system libraries, used when
# REDDIT_LIVE_CLIENT=browser. The Python package version must match the tag.
FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/bin/python3 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra browser --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra browser --no-editable

# The image provides an unprivileged user for running browsers. The app
# stays owned by root (pwuser can run it, not change it). The container
# starts as root only so the entrypoint can give pwuser the session volume
# (mounted owned by root); it then drops to pwuser.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0

# Railway sets PORT; 8000 is the local default.
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/app/.venv/bin/reddit-mcp"]
