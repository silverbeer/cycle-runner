# Cycle Runner's scheduled commands in k3s (V1.6: the daily standup).
#
# Only what those commands need: Python, the locked dependencies and the
# package. No Ollama, no Claude CLI, no git: the standup reads Linear and
# sends one Telegram message. The image carries no credentials; the CronJob
# gets them from a Secret as environment variables.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --uid 10001 --no-create-home runner
USER 10001

ENTRYPOINT ["python", "-m"]
CMD ["cycle_runner.standup"]
