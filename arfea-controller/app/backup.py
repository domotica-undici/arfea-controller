from __future__ import annotations

import fnmatch
import logging
import os
import shutil
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

# Roba che nel backup non ci va, per nome (il match e' sul percorso DENTRO
# l'archivio, a qualsiasi profondita': exclude_paths di arfea.yml invece filtra
# solo le cartelle di primo livello del data path).
# Il kar degli addon OpenHAB e' ~600 MB ri-scaricabili in qualsiasi momento, non
# dati dell'impianto: metterlo dentro raddoppierebbe l'archivio e il tempo di
# upload su WebDAV — che su una linea domestica si misura in decine di minuti e ha
# un tetto di mezz'ora, oltre il quale il backup fallisce. Al ripristino ci pensa
# il controller a riportarlo a bordo (vedi app/addons.py).
# Stesso discorso per le sue copie: Karaf lo estrae in userdata/kar e in
# userdata/tmp/kar (~600 MB ciascuna). Con quelle dentro il backup era passato da
# ~460 MB a 2,15 GB e aveva riempito una eMMC da 16 GB (Redmine #202). cache e tmp
# di userdata li svuota comunque il container a ogni avvio (cont-init.d).
# Un pattern che combacia con una cartella la esclude con tutto il contenuto;
# "…/*" lascia la cartella (vuota) e toglie cio' che c'e' dentro.
_EXCLUDE_GLOBS = (
    "openhab/addons/*.kar",
    "openhab/addons/.*.kar.part",
    "openhab/userdata/kar/*",
    "openhab/userdata/tmp/*",
    "openhab/userdata/cache/*",
)

_BACKUP_GLOB = "arfea-backup-*.tar.gz"
_MB = 1024 * 1024
# Margine oltre la stima dell'archivio: il resto dell'impianto deve poter scrivere.
_SPACE_MARGIN = 200 * _MB


class NoSpaceError(RuntimeError):
    """Spazio insufficiente per il backup anche dopo aver tolto quelli vecchi."""


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


def _excluded(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in _EXCLUDE_GLOBS)


def _skip_excluded(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Filtro di tar.add: scarta le voci che combaciano con _EXCLUDE_GLOBS.

    ``info.name`` e' il percorso relativo alla radice dell'archivio, es.
    ``openhab/addons/openhab-addons-5.2.0.kar``."""
    if _excluded(info.name):
        logger.info("Backup: escluso %s", info.name)
        return None
    return info


class BackupManager:
    def __init__(self, config: BackupConfig, docker_manager: DockerManager):
        self.config = config
        self.docker = docker_manager
        self.status = BackupStatus()

    # ------------------------------------------------------------------
    # Spazio su disco (Redmine #201)
    # ------------------------------------------------------------------

    def _estimate_size(self, data_path: Path, exclude_set: set[str]) -> int:
        """Byte che finiscono nell'archivio, NON compressi: per eccesso. Meglio
        chiedere un po' di spazio in piu' che riempire il disco a meta' archivio."""
        total = 0
        for item in data_path.iterdir():
            if any(str(item).startswith(ex) for ex in exclude_set):
                continue
            if item.is_file():
                total += item.lstat().st_size
                continue
            for root, dirs, files in os.walk(item):
                rel_root = os.path.relpath(root, data_path)
                dirs[:] = [d for d in dirs if not _excluded(os.path.join(rel_root, d))]
                for name in files:
                    if _excluded(os.path.join(rel_root, name)):
                        continue
                    try:
                        total += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        pass
        return total

    @staticmethod
    def _local_backups(backup_dir: Path) -> list[Path]:
        """Backup locali, dal piu' vecchio al piu' nuovo."""
        return sorted(backup_dir.glob(_BACKUP_GLOB), key=lambda f: f.stat().st_mtime)

    def _make_room(self, backup_dir: Path, needed: int) -> int:
        """Toglie i backup locali vecchi finche' c'e' spazio per quello nuovo.
        Ritorna lo spazio libero; solleva NoSpaceError se non basta comunque."""
        free = shutil.disk_usage(backup_dir).free
        for old in self._local_backups(backup_dir):
            if free >= needed:
                break
            size = old.stat().st_size
            old.unlink()
            free += size
            logger.warning("Backup: tolto %s (%.0f MB) per fare spazio al nuovo", old.name, size / _MB)
        if free < needed:
            raise NoSpaceError(
                f"Spazio insufficiente per il backup: servono circa {needed / _MB:.0f} MB, "
                f"liberi {free / _MB:.0f} MB anche dopo aver tolto i backup vecchi"
            )
        return free

    def _keep_only(self, backup_dir: Path, keep: Path) -> None:
        """Sulla centralina resta solo l'ultimo backup completo: lo storico sta su
        WebDAV, e su una eMMC da 16 GB ogni archivio in piu' toglie spazio a
        immagini e aggiornamenti."""
        for old in self._local_backups(backup_dir):
            if old != keep:
                try:
                    old.unlink()
                    logger.info("Backup: tolto il backup locale precedente %s", old.name)
                except OSError as exc:
                    logger.warning("Backup: non riesco a togliere %s: %s", old.name, exc)

    # ------------------------------------------------------------------
    # Backup
    # ------------------------------------------------------------------

    def run_backup(self) -> BackupStatus:
        if self.status.state not in (BackupState.IDLE, BackupState.COMPLETED, BackupState.FAILED):
            return self.status

        self.status = BackupStatus(
            state=BackupState.STOPPING_CONTAINERS,
            message="Controllo dello spazio su disco...",
            started_at=datetime.now(),
        )

        data_path = Path(self.docker.cfg.config.controller.data_path)
        backup_dir = data_path / "arfea-controller" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        exclude_set = set(self.config.exclude_paths)
        exclude_set.add(str(backup_dir))

        # Spazio PRIMA di fermare qualunque cosa: senza, l'impianto si spegneva per
        # riempire il disco a meta' archivio (Redmine #201).
        try:
            needed = self._estimate_size(data_path, exclude_set) + _SPACE_MARGIN
            self._make_room(backup_dir, needed)
        except NoSpaceError as exc:
            logger.error("%s", exc)
            self.status = BackupStatus(
                state=BackupState.FAILED,
                message=str(exc),
                no_space=True,
                started_at=self.status.started_at,
                completed_at=datetime.now(),
            )
            return self.status
        self.status.message = "Arresto container in corso..."

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

            with tarfile.open(str(archive_path), "w:gz") as tar:
                for item in data_path.iterdir():
                    item_path = str(item)
                    if any(item_path.startswith(ex) for ex in exclude_set):
                        logger.debug("Excluding %s", item_path)
                        continue
                    tar.add(item_path, arcname=item.name, filter=_skip_excluded)

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

            # L'archivio c'e' ed e' completo: da qui e' un punto di ripristino, e sulla
            # centralina resta solo lui.
            self._keep_only(backup_dir, archive_path)

            # Upload to WebDAV. Un caricamento fallito non annulla l'archivio locale
            # (Redmine #204): il backup lo dice, ma resta utilizzabile.
            upload_error = ""
            if self.config.webdav_url:
                self.status.state = BackupState.UPLOADING
                self.status.message = f"Upload in corso ({size_mb:.0f} MB)..."
                try:
                    self._upload_webdav(archive_path, filename)
                except Exception as exc:
                    upload_error = str(exc).splitlines()[0]
                    logger.error("Upload WebDAV fallito: %s", exc)

            if upload_error:
                self.status = BackupStatus(
                    state=BackupState.FAILED,
                    message=(f"Backup salvato sulla centralina ({filename}, {size_mb:.0f} MB), "
                             f"ma il caricamento su WebDAV non è riuscito: {upload_error}"),
                    archive=str(archive_path),
                    started_at=self.status.started_at,
                    completed_at=datetime.now(),
                )
            else:
                self.status = BackupStatus(
                    state=BackupState.COMPLETED,
                    message=f"Backup completato: {filename}",
                    archive=str(archive_path),
                    started_at=self.status.started_at,
                    completed_at=datetime.now(),
                )
                logger.info("Backup completed successfully")

        except Exception as exc:
            logger.error("Backup failed: %s", exc)
            # Un archivio a meta' (disco pieno, errore di lettura) e' solo spazio perso.
            if archive_path.exists() and self.status.state == BackupState.CREATING_ARCHIVE:
                try:
                    archive_path.unlink()
                    logger.info("Backup: tolto l'archivio incompleto %s", filename)
                except OSError:
                    pass
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
