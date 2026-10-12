FROM python:3.12-slim

WORKDIR /app

# Azure Speech SDK (Standard/Hybrid realtime) runtime libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libasound2 libssl3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY mai_client.py .
COPY english_summary.py .
COPY auth.py .
COPY realtime.py .
COPY public ./public

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
