FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt \
    && groupadd --gid 10001 lanxi \
    && useradd --uid 10001 --gid lanxi --home-dir /var/lib/lanxi --no-create-home lanxi \
    && install -d -m 0700 -o lanxi -g lanxi /var/lib/lanxi

COPY coordination_server.py coordination.py compute_queue.py compute_artifacts.py personal_relay.py device_enrollment.py personal_task_locks.py ./
COPY handoff.py handoff_sync.py lfs_assets.py service_lock.py coordination_client.py cloud_pairing.py rpc_errors.py ./
COPY deploy/coordinator/cloud_admin.py ./
COPY portable_releases.py portable_update.py temporary_file_server.py code_backup_server.py ./

USER 10001:10001
ENTRYPOINT ["python"]
CMD ["coordination_server.py", "--devices", "/var/lib/lanxi/devices.json", "--database", "/var/lib/lanxi/coordination.sqlite3", "--host", "0.0.0.0", "--no-access-log", "--limit-concurrency", "24"]
