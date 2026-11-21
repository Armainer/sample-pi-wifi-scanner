FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir "psycopg[binary]==3.2.10"

WORKDIR /opt/web
COPY web.py /opt/web/web.py

EXPOSE 80
CMD ["python3", "-u", "/opt/web/web.py"]
