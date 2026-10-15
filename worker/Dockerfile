FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[extract]"
ENV PYTHONUNBUFFERED=1
CMD ["sensing-worker"]
