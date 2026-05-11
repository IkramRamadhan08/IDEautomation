FROM nikolaik/python-nodejs:python3.12-nodejs24-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt
RUN npm install -g 9router

COPY api /app/api
COPY scripts /app/scripts

EXPOSE 8000

CMD ["sh", "-c", "sh /app/scripts/start-railway.sh"]
