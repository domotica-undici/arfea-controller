from __future__ import annotations

import logging
import os
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .models import BackupConfig, BackupState, BackupStatus

if TYPE_CHECKING:
    from .docker_manager import DockerManager

logger = logging.getLogger(__name__)

# Timeout per singola operazione di rete dell'upload: una connessione che si pianta
# se ne accorge in fretta, senza aspettare i 10 minuti di prima.
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, write=120.0, read=300.0, pool=30.0)

# Tetto complessivo all'upload. Serve perche' i timeout per operazione NON bastano:
# un trasferimento che avanza a singhiozzo non li fa mai scattare e il backup resta
# appeso per sempre, bloccando anche l'apply di release che lo aspetta.
UPLOAD_MAX_SECONDS = 1800


class _DeadlineFile:
    """File-like con scadenza, da dare a httpx al posto del file aperto.

    httpx legge l'archivio a blocchi mentre lo trasmette (IteratorByteStream usa
    read() se c'e'), quindi e' qui dentro che si intercetta un upload che non
    finisce piu'. Espone fileno() perche' httpx ne ricava la dimensione e manda
    Content-Length invece di Transfer-Encoding: chunked, e __iter__ perche'
    accetta come content solo oggetti iterabili."""

    CHUNK_SIZE = 65_536

    def __init__(self, fh, max_seconds: int):
        self._fh = fh
        self._max_seconds = max_seconds
        self._deadline = time.monotonic() + max_seconds
        self.sent = 0

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() > self._deadline:
            raise TimeoutError(
                f"upload non completato entro {self._max_seconds}s "
                f"(inviati {self.sent / 1024 / 1024:.0f} MB), interrotto"
            )
        chunk = self._fh.read(size)
        self.sent += len(chunk)
        return chunk

    def fileno(self) -> int:
        return self._fh.fileno()

    def __iter__(self):
        while True:
            chunk = self.read(self.CHUNK_SIZE)
            if not chunk:
                return
            yield chunk


class BackupManager:
    def __init__(self, config: BackupConfig, docker_manager: DockerManager):
        self.config = config
        self.docker = docker_manager
        self.status = BackupStatus()

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def run_backup(self) -> BackupStatus:
        if self.status.state not in (BackupState.IDLE, BackupState.COMPLETED, BackupState.FAILED):
            return self.status

        self.status = BackupStatus(
            state=BackupState.STOPPING_CONTAINERS,
            message="Arresto container in corso...",
            started_at=datetime.now(),
        )

        data_path = Path(self.docker.cfg.config.controller.data_path)
        backup_dir = data_path / "arfea-controller" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Read UUID for filename
        uuid_file = data_path / "openhab" / "userdata" / "uuid"
        uuid_str = uuid_file.read_text().strip() if uuid_file.exists() else "unknown"
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        filename = f"arfea-backup-{timestamp}-{uuid_str}.tar.gz"
        archive_path = backup_dir / filename

        # Record running services before stopping
        previously_running = self.docker.get_running_services()
        restarted = False

        try:
            # Stop all containers except ourselves
            self.docker.stop_all()

            # Create tar archive (streaming to disk)
            self.status.state = BackupState.CREATING_ARCHIVE
            self.status.message = "Creazione archivio backup..."
            logger.info("Creating backup archive: %s", archive_path)

            exclude_set = set(self.config.exclude_paths)
            exclude_set.add(str(backup_dir))

            with tarfile.open(str(archive_path), "w:gz") as tar:
                for item in data_path.iterdir():
                    item_path = str(item)
                    if any(item_path.startswith(ex) for ex in exclude_set):
                        logger.debug("Excluding %s", item_path)
                        continue
                    tar.add(item_path, arcname=item.name)

            size_mb = archive_path.stat().st_size / 1024 / 1024
            logger.info("Archive created: %s (%.1f MB)", archive_path, size_mb)

            # I container tornano su PRIMA dell'upload: l'archivio e' gia' su disco ed
            # e' coerente, tenere l'impianto fermo anche per la trasmissione e' downtime
            # inutile. Su una linea domestica mezzo giga di backup sono decine di minuti
            # (e con l'apply di release davanti, decine di minuti di impianto spento).
            self.status.state = BackupState.RESTARTING_CONTAINERS
            self.status.message = "Riavvio container..."
            self._restart_services(previously_running)
            restarted = True

            # Upload to WebDAV
            if self.config.webdav_url:
                self.status.state = BackupState.UPLOADING
                self.status.message = f"Upload in corso ({size_mb:.0f} MB)..."
                self._upload_webdav(archive_path, filename)

            self.status = BackupStatus(
                state=BackupState.COMPLETED,
                message=f"Backup completato: {filename}",
                started_at=self.status.started_at,
                completed_at=datetime.now(),
            )
            logger.info("Backup completed successfully")

        except Exception as exc:
            logger.error("Backup failed: %s", exc)
            self.status = BackupStatus(
                state=BackupState.FAILED,
                message=f"Backup fallito: {exc}",
                started_at=self.status.started_at,
                completed_at=datetime.now(),
            )
            # Always try to restart services on failure
            if not restarted:
                try:
                    self._restart_services(previously_running)
                except Exception:
                    logger.error("Failed to restart services after backup failure")

        return self.status

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def run_restore(self, backup_name: str) -> BackupStatus:
        if self.status.state not in (BackupState.IDLE, BackupState.COMPLETED, BackupState.FAILED):
            return self.status

        self.status = BackupStatus(
            state=BackupState.STOPPING_CONTAINERS,
            message="Arresto container per ripristino...",
            started_at=datetime.now(),
        )

        data_path = Path(self.docker.cfg.config.controller.data_path)
        backup_dir = data_path / "arfea-controller" / "backups"
        archive_path = backup_dir / backup_name

        try:
            # Download from WebDAV if not present locally
            if not archive_path.exists() and self.config.webdav_url:
                self.status.message = "Download backup dal cloud..."
                self._download_webdav(backup_name, archive_path)

            if not archive_path.exists():
                raise FileNotFoundError(f"Backup file not found: {backup_name}")

            previously_running = self.docker.get_running_services()
            self.docker.stop_all()

            # Extract archive
            self.status.state = BackupState.CREATING_ARCHIVE
            self.status.message = "Ripristino da archivio..."
            logger.info("Restoring from: %s", archive_path)

            with tarfile.open(str(archive_path), "r:gz") as tar:
                tar.extractall(path=str(data_path))

            # Restart all enabled services
            self.status.state = BackupState.RESTARTING_CONTAINERS
            self.status.message = "Riavvio container..."
            # Reload config (may have changed after restore)
            self.docker.cfg.load()
            self.docker.start_all_enabled()

            self.status = BackupStatus(
                state=BackupState.COMPLETED,
                message=f"Ripristino completato da {backup_name}",
                started_at=self.status.started_at,
                completed_at=datetime.now(),
            )
            logger.info("Restore completed successfully")

        except Exception as exc:
            logger.error("Restore failed: %s", exc)
            self.status = BackupStatus(
                state=BackupState.FAILED,
                message=f"Ripristino fallito: {exc}",
                started_at=self.status.started_at,
                completed_at=datetime.now(),
            )

        return self.status

    # ------------------------------------------------------------------
    # WebDAV (synchronous)
    # ------------------------------------------------------------------

    def _upload_webdav(self, local_path: Path, remote_name: str) -> None:
        url = f"{self.config.webdav_url}/{remote_name}"
        auth = (self.config.webdav_user, self.config.webdav_password)

        logger.info("Uploading to %s", url)
        started = time.monotonic()
        with httpx.Client(timeout=UPLOAD_TIMEOUT) as client:
            with open(local_path, "rb") as f:
                response = client.put(
                    url,
                    content=_DeadlineFile(f, UPLOAD_MAX_SECONDS),
                    auth=auth,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                response.raise_for_status()
        elapsed = max(time.monotonic() - started, 0.001)
        size_mb = os.path.getsize(local_path) / 1024 / 1024
        logger.info(
            "Upload completed (HTTP %s) - %.0f MB in %.0fs (%.2f MB/s)",
            response.status_code, size_mb, elapsed, size_mb / elapsed,
        )

    def _download_webdav(self, remote_name: str, local_path: Path) -> None:
        url = f"{self.config.webdav_url}/{remote_name}"
        auth = (self.config.webdav_user, self.config.webdav_password)

        logger.info("Downloading from %s", url)
        with httpx.Client(timeout=UPLOAD_TIMEOUT) as client:
            with client.stream("GET", url, auth=auth) as response:
                response.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)
        logger.info("Download completed")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _restart_services(self, service_names: list[str]) -> None:
        """Restart all previously-running services in dependency order.

        Uses a simple ordering: core services first, then services with
        depends_on satisfied, then the rest. Does NOT filter by enabled
        flag — if it was running before, restart it.
        """
        services = self.docker.cfg.config.services
        remaining = set(service_names)
        started: set[str] = set()

        # Multiple passes to resolve dependency order
        for _ in range(len(remaining) + 1):
            if not remaining:
                break
            for name in list(remaining):
                svc = services.get(name)
                if svc is None:
                    remaining.discard(name)
                    continue
                # Check if dependencies are satisfied
                deps_met = all(d in started for d in svc.depends_on)
                if deps_met or svc.core:
                    self.docker.create_and_start(name)
                    started.add(name)
                    remaining.discard(name)

        # Start anything still remaining (deps may not be satisfiable)
        for name in remaining:
            self.docker.create_and_start(name)
