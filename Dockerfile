# Cycle Runner's scheduled commands in k3s (V1.6: the daily standup).
#
# Only what those commands need: Python, the core dependencies and the
# package. --no-default-groups leaves out ADK, LiteLLM and the Claude Agent
# SDK (the agent and executor groups in pyproject.toml): the standup reads
# Linear and sends Telegram messages, and imports none of them (SB-1216).
#
# Two stages on Alpine: uv and the build context stay in the first; the
# image is Python plus the venv. 1.35 GB before SB-1216, about 90 MB after;
# .github/workflows/image.yml fails the build above its size budget.
# The image carries no credentials; the CronJob gets them from a Secret.
FROM python:3.14-alpine AS build

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-default-groups --no-install-project
COPY src ./src
# --no-editable: the package goes into the venv, so the next stage needs only the venv.
RUN uv sync --locked --no-default-groups --no-editable

FROM python:3.14-alpine

COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

RUN adduser -D -H -u 10001 runner
USER 10001

ENTRYPOINT ["python", "-m"]
CMD ["cycle_runner.standup"]
