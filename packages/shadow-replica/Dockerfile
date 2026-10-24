# Build context: workspace root (docker build -f packages/shadow-replica/Dockerfile .)
FROM python:3.14-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

# Copy full workspace so uv can resolve workspace sources
COPY pyproject.toml uv.lock ./
COPY packages/shadow-replica ./packages/shadow-replica
COPY packages/ghost-daemon ./packages/ghost-daemon
COPY packages/employer-voice ./packages/employer-voice
COPY src ./src

# Install only shadow-replica and its transitive deps
RUN uv sync --package shadow-replica --no-dev --frozen

EXPOSE 7475

# Replica config is provided via a mounted file or environment;
# default config auto-creates at /root/.vantage-replica/replica.toml
CMD ["uv", "run", "shadow-replica", "serve"]
