FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt pyproject.toml README.md ./
COPY cair_agent ./cair_agent
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

# Data and secrets are mounted/injected at runtime, never baked into the image:
#   docker run --rm -v $PWD/data_subset10:/data -v $PWD/runs:/app/runs \
#     -e OPENAI_BASE_URL -e OPENAI_API_KEY -e CAIR_DATA_DIR=/data cair-agent run -c case-01 --critic
ENV CAIR_DATA_DIR=/data CAIR_INDEX_PATH=/app/runs/case_index.json CAIR_SQLITE_PATH=/app/runs/cair.sqlite
ENTRYPOINT ["python", "-m", "cair_agent.cli"]
CMD ["--help"]
