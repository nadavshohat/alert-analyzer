# Build stage: install dependencies into a venv
FROM python:3.14-slim-bookworm AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --no-compile --prefer-binary -r requirements.txt

# Runtime stage: copy only the venv + source (no pip/setuptools/wheel)
FROM python:3.14-slim-bookworm

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" --no-create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser src/ .

USER 10001

ENTRYPOINT ["python", "main.py"]
