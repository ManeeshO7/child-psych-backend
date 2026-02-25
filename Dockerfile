# Stage 1: Generate Prisma Client Python
# We use the Python CLI (not Node Prisma) to avoid version-mismatch issues.
FROM python:3.12-slim AS prisma
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl \
    libatomic1 \
    ca-certificates \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir prisma==0.11.0
RUN command -v prisma-client-py >/dev/null

COPY prisma ./prisma

# Remove the JS generator for container builds (not needed at runtime)
RUN python3 -c "import re; from pathlib import Path; t=Path('prisma/schema.prisma').read_text(); t=re.sub(r'(?ms)^generator\\s+studio_client\\s*\\{.*?^\\}\\s*\\n','',t); Path('prisma/schema.deploy.prisma').write_text(t)"

RUN python3 -m prisma generate --schema=prisma/schema.deploy.prisma

# Stage 2: Run FastAPI app
FROM python:3.12-slim
WORKDIR /app

# Prisma engines require shared libs at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl \
    libatomic1 \
    ca-certificates \
  && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Prisma schema + generated client + engine cache from stage 1
COPY --from=prisma /app/prisma ./prisma
COPY --from=prisma /usr/local/lib/python3.12/site-packages/prisma /usr/local/lib/python3.12/site-packages/prisma
COPY --from=prisma /root/.cache/prisma-python /root/.cache/prisma-python

# Copy the query engine binary into /app, which Prisma checks first.
RUN python3 - <<'PY'
import os
import shutil
import stat

root = "/root/.cache/prisma-python/binaries"
preferred_names = (
    "query-engine-debian-openssl-3.5.x",
    "query-engine-debian-openssl-3.0.x",
)

def find_engine() -> str:
    preferred_hits: list[str] = []
    fallback_hits: list[str] = []
    for dp, _, files in os.walk(root):
        for f in files:
            if f in preferred_names:
                preferred_hits.append(os.path.join(dp, f))
            elif f.startswith("query-engine-"):
                fallback_hits.append(os.path.join(dp, f))
    if preferred_hits:
        preferred_hits.sort()
        return preferred_hits[0]
    if fallback_hits:
        fallback_hits.sort()
        return fallback_hits[0]
    raise SystemExit(
        f"No Prisma query engine found under {root}. "
        "Expected a file named like query-engine-debian-openssl-3.0.x"
    )

src = find_engine()

def install(dst: str) -> None:
    shutil.copyfile(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

# Prisma Client Python checks /app for prisma-query-engine-* first.
install("/app/prisma-query-engine-debian-openssl-3.5.x")
install("/app/prisma-query-engine-debian-openssl-3.0.x")
print("Installed Prisma query engine from", src)
PY

# App code
COPY app ./app
COPY main.py ./

# Python Prisma expects to find generated client; ensure it's on path
ENV PYTHONPATH=/app
# Cloud Run sets PORT (default 8080)
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
