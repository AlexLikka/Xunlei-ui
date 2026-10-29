FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 THUNDER_DATA_DIR=/data THUNDER_BIND=0.0.0.0 THUNDER_PORT=8765
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home thunder && mkdir /data && chown thunder:thunder /data
COPY monitor.py paths.py web_auth.py workspaces.py migrate_users.py fleet.py live_tracker.py local_control.py machine_settings.py manual_actions.py thunder_api.py cloud_transfer.py resource_limits.py previews.py ./
COPY web/ ./web/
COPY remote/ ./remote/
USER thunder
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/auth/status', timeout=3).read()"]
CMD ["python", "monitor.py"]
