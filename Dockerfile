FROM python:3.11-slim

WORKDIR /app

# Create non-root user first
RUN useradd -r -u 1000 appuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and set permissions
COPY --chown=appuser:appuser src/ .

USER appuser

CMD ["python", "main.py"]
