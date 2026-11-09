FROM docker.io/library/python:3.12-slim-bookworm

WORKDIR /app

RUN pip install --no-cache-dir pymysql requests

COPY sla_external_sync.py /app/sla_external_sync.py

ENV PYTHONUNBUFFERED=1

CMD ["python3", "/app/sla_external_sync.py"]
