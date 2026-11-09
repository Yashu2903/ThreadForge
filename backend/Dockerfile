# ThreadForge backend image. The AI worker runs from it (python -m app.workers.ai_worker) and it also
# runs the API (uvicorn app.main:app) and the operator commands. Build from the REPOSITORY ROOT:
#   docker build -f backend/Dockerfile -t threadforge-backend .
#
# Why this image has Tesseract: the worker OCRs scanned PDF pages locally (English data), and the
# sandboxed extractor starts the tesseract binary. `tesseract --list-langs` must list "eng" (the AI
# self-check verifies that at start).
#
# Why it has Node: the worker parse-checks the Mermaid diagrams in answers with the real Mermaid
# parser (packages/mermaid-check, Mermaid under jsdom, run as a sandboxed child process). The first
# stage builds that package; the final image only carries the Node binary, the built output and its
# production dependencies. Without Node the worker still enforces the static diagram rules and
# publishes diagrams as "unchecked" (docs/ai.md).

# ---- stage 1: build the Node side (shared schema + mermaid-check) ------------------------------------
FROM node:22-slim AS node-build
WORKDIR /repo
COPY package.json package-lock.json ./
COPY packages/editor-schema/package.json packages/editor-schema/
COPY packages/mermaid-check/package.json packages/mermaid-check/
# npm still wants the other workspaces' manifests to resolve the lockfile
COPY collab/package.json collab/
COPY frontend/package.json frontend/
RUN npm ci --workspace @threadforge/mermaid-check --workspace @threadforge/editor-schema --include-workspace-root
COPY packages/editor-schema packages/editor-schema
COPY packages/mermaid-check packages/mermaid-check
RUN npm run build -w @threadforge/editor-schema && npm run build -w @threadforge/mermaid-check \
    && npm prune --omit=dev --workspace @threadforge/mermaid-check --workspace @threadforge/editor-schema

# ---- stage 2: the Python worker image --------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# the Node runtime and the built Mermaid checker, laid out as in the repository
COPY --from=node-build /usr/local/bin/node /usr/local/bin/node
COPY --from=node-build /repo/node_modules /opt/threadforge/node_modules
COPY --from=node-build /repo/packages /opt/threadforge/packages
ENV AI_NODE_COMMAND=/usr/local/bin/node \
    AI_MERMAID_CHECK_SCRIPT=/opt/threadforge/packages/mermaid-check/dist/cli.js

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/ ./

# an unprivileged user; STORAGE_PATH is a volume shared with the API
RUN useradd --system --uid 10001 --home-dir /app threadforge \
    && mkdir -p /data/storage && chown -R threadforge /data/storage
USER threadforge

CMD ["python", "-m", "app.workers.ai_worker"]
