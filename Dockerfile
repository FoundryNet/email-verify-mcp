FROM python:3.11-slim

WORKDIR /app

# `whois` binary backs python-whois (domain age); ca-certificates for TLS.
RUN apt-get update && apt-get install -y --no-install-recommends \
    whois ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
