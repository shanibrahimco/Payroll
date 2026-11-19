# The payroll app. Data is never stored in this image: it lives in the /data folder mounted from the server.
FROM python:3.15.2-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Check-in times and late arrivals use this clock
ENV TZ=Asia/Karachi \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAYROLL_DATA_DIR=/data \
    PAYROLL_BACKUP_DIR=/backups \
    PAYROLL_HOST=0.0.0.0 \
    PAYROLL_PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# The app runs as an ordinary user, not as root
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin payroll \
    && mkdir -p /data /backups \
    && chown payroll:payroll /data /backups
USER payroll

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://127.0.0.1:8080/health', timeout=8)"]

CMD ["python", "app.py"]
