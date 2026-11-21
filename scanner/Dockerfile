FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends iw \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/scanner
COPY scanner.py /opt/scanner/scanner.py

CMD ["python3", "-u", "/opt/scanner/scanner.py"]
