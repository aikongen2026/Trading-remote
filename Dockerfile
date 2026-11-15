FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Official rclone binary: install in the container image, not on persistent disk.
RUN apt-get update && apt-get install -y --no-install-recommends rclone ca-certificates && rm -rf /var/lib/apt/lists/*

COPY . .
EXPOSE 10000
CMD ["python", "cloud_app.py"]
