FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY wsgi.py RELEASE ./
RUN useradd -r -u 1000 web && mkdir -p /data && chown web /data
USER web
EXPOSE 8000
CMD ["gunicorn", "--preload", "-w", "3", "--threads", "4", "-b", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
