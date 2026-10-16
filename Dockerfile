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
RUN python3 -c "\
import os, shutil, stat; \
root = '/root/.cache/prisma-python/binaries'; \
preferred = ('query-engine-debian-openssl-3.5.x', 'query-engine-debian-openssl-3.0.x'); \
hits = [os.path.join(dp, f) for dp, _, files in os.walk(root) for f in files if f.startswith('query-engine-')]; \
pref = sorted([h for h in hits if any(p in h for p in preferred)]); \
src = pref[0] if pref else (sorted(hits)[0] if hits else (_ for _ in ()).throw(SystemExit('No Prisma engine found'))); \
install = lambda dst: (shutil.copyfile(src, dst), os.chmod(dst, os.stat(dst).st_mode | 0o111)); \
install('/app/prisma-query-engine-debian-openssl-3.5.x'); \
install('/app/prisma-query-engine-debian-openssl-3.0.x'); \
print('Installed Prisma query engine from', src) \
"

# App code
COPY app ./app
COPY main.py ./

# Python Prisma expects to find generated client; ensure it's on path
ENV PYTHONPATH=/app
# Cloud Run sets PORT (default 8080)
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
