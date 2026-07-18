FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
COPY requirements.runtime.txt ./
ARG INSTALL_ML=false
# The full semantic image uses CPU-only model dependencies and includes the
# local parser model used when an extraction LLM is not configured.
RUN if [ "$INSTALL_ML" = "true" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
      && pip install --no-cache-dir -r requirements.txt \
      && python -m spacy download en_core_web_sm; \
    else \
      pip install --no-cache-dir -r requirements.runtime.txt; \
    fi

COPY memoryos ./memoryos
COPY migrations ./migrations
COPY schema.sql ./schema.sql

RUN useradd --create-home --uid 10001 memoryos
USER memoryos

CMD ["uvicorn", "memoryos.main:app", "--host", "0.0.0.0", "--port", "8088"]
