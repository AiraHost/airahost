FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY ml_sidecar/requirements.txt /app/ml_sidecar/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/ml_sidecar/requirements.txt

COPY ml_sidecar /app/ml_sidecar

CMD ["python", "-m", "ml_sidecar.worker"]
