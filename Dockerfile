# ---------- Stage 1: build dependencies + download the embedding model ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-app.txt .
RUN pip install -r requirements-app.txt
# Bake the model into the image so the container starts fast and needs no download at runtime
ARG EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}', cache_dir='/opt/models')"

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE=/opt/models \
    PYDANTIC_AI_NO_BANNER=1
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models
# App code
COPY agent.py tools.py embeddings.py app.py chainlit.md ./
COPY chainlit_config.toml .chainlit/config.toml
COPY translations/en-US.json .chainlit/translations/en-US.json
COPY public ./public
# Pre-built data (run build_index.py before docker build)
COPY data/properties.db data/index_report.json data/darglobal_contact.json data/
COPY data/lancedb data/lancedb
RUN chown -R app:app /app /opt/models
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
