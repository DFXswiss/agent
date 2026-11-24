FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE teams.yaml keys.yaml ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health')"
CMD ["python", "-m", "agent_core"]
