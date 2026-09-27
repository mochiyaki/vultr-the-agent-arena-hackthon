# Sandbox image: what the agent's code runs in. Kept small, no package managers needed
# at runtime because the container has no network. Runs as nobody (uid 65534).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs coreutils curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir numpy pandas matplotlib requests \
    && mkdir -p /workspace && chown 65534:65534 /workspace

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg HOME=/tmp
WORKDIR /workspace
USER 65534:65534
CMD ["sleep", "infinity"]
