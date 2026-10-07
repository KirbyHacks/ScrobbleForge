FROM python:3.12-slim

# Prevent Python from writing bytecode and ensure immediate unbuffered log output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and helper scripts
COPY src/ /app/src/
COPY auth_helper.py /app/

# Create data directory and set permissions for non-root user (UID 1000)
RUN mkdir -p /app/data && chown -R 1000:1000 /app

# Run as non-privileged user to avoid permission escalation
USER 1000:1000

# Default command launches the scrobbler module
CMD ["python", "-m", "src.main"]
