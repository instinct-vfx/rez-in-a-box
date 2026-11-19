# SPDX-License-Identifier: Apache-2.0
# rez-aware memcached inspector. Standard library only.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REZBOX_MEMCACHED=memcached:11211

WORKDIR /app
COPY rezbox/ /app/rezbox/
COPY services/memcache-ui/app.py services/memcache-ui/index.html /app/

RUN useradd --system --no-create-home inspector
USER inspector

EXPOSE 8080
CMD ["python", "/app/app.py", "--host", "0.0.0.0", "--port", "8080"]
