FROM python:3.12-slim

# No Chromium / Playwright browser deps here on purpose: every Playwright
# call in app/ uses connect_over_cdp() against a browser on the local
# network (see app/scc_llc_filer.py, app/ein_filer.py,
# app/agents/scc_name_check.py) - none of them ever call .launch(), so
# there's no local browser binary to install. The `playwright` pip package
# (installed below via uv sync) is enough for those imports to succeed;
# they'll just fail to connect when run from Cloud Run, which is expected -
# those actions are meant to keep running from a local instance instead.

# Install uv (single static binary - no apt-get needed for this).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY . .

# --frozen: install exactly what's pinned in uv.lock rather than silently
# re-resolving if pyproject.toml and the lock have drifted.
RUN uv sync --frozen

# Cloud Run injects PORT and expects the container to listen on it - 8080
# is also Cloud Run's own default, so this works whether or not it's
# overridden later.
ENV PORT=8080
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
