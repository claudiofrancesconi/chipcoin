FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY docker ./docker
COPY docker/entrypoint.sh /usr/local/bin/chipcoin-entrypoint
COPY services/bootstrap-seed/src ./services/bootstrap-seed/src

RUN pip install --no-cache-dir .
RUN chmod +x /usr/local/bin/chipcoin-entrypoint

CMD ["chipcoin", "--help"]
