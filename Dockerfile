FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY auth_helper.py /app/
COPY .env.example /app/

RUN mkdir -p /app/data && chown -R 1000:1000 /app

USER 1000:1000

ENTRYPOINT ["python", "-m", "src.main"]
CMD []
