FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=10000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Official rclone binary: install in the container image, not on persistent disk.
RUN python -c "import io,os,urllib.request,zipfile; u='https://downloads.rclone.org/rclone-current-linux-amd64.zip'; z=zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(u,timeout=90).read())); p=next(n for n in z.namelist() if n.endswith('/rclone')); dst='/usr/local/bin/rclone'; open(dst,'wb').write(z.read(p)); os.chmod(dst,0o755)"

COPY . .
EXPOSE 10000
CMD ["python", "cloud_app.py"]
