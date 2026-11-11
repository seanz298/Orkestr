# Node base because the Codex CLI is an npm package; Python comes from Debian.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git ca-certificates curl ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    ADT_WORKSPACE=/workspace \
    ADT_CONFIG=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orchestrator ./orchestrator
COPY config.yaml .

# Agents never run as root. The image's built-in "node" user is uid 1000.
RUN mkdir -p /workspace && chown -R node:node /workspace /app
USER node

CMD ["python", "-m", "orchestrator", "run"]
