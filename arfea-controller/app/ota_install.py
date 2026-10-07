"""Installazione del tarball OTA che regge un'interruzione di corrente (Redmine #355).

Fino alla 1.8.15 il self-update cancellava ogni cartella (app/, habapp/,
templates/...) e la ricopiava, poi lanciava subito la build. Su ext4 i file
nuovi restano solo in memoria per una trentina di secondi: su un impianto la
corrente e' mancata in quel momento, e i file installati, e con loro l'immagine
appena costruita, sono rimasti a 0 byte. Docker ha poi scartato il container
nuovo («failed to load container mount»), e senza controller nessuno lo ha
ricreato (Redmine #354).

Qui l'installazione avviene in tre passi, e un'interruzione in qualunque punto
lascia la versione vecchia o la nuova, mai una cartella vuota:
  1. estrazione in una cartella di appoggio sullo stesso filesystem, os.sync()
     e controllo di ogni file (dimensione dal tar, sha256 dal MANIFEST.sha256
     che build-update-tarball.sh mette nel tarball);
  2. scambio delle cartelle con rename (la vecchia va in .update-prev/);
  3. os.sync() di nuovo, prima di lanciare la build.

Lo stato «aggiornamento in sospeso» (.update-pending.json) sostituisce lo
.update_hash scritto prima dell'installazione: l'hash si registra solo quando
il controller nuovo e' partito, e un tarball che non ci arriva si riprova al
massimo MAX_ATTEMPTS volte.

Il guardiano sull'host (script/arfea-controller-guard.sh) fa il rebuild e
controlla ogni 5 minuti che il container del controller ci sia: lo installa
install_host_guard() a ogni avvio del controller, solo se e' cambiato.
"""

from __future__ import annotations

import filecmp
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MANIFEST = "MANIFEST.sha256"
STAGING = ".update-staging"
PREV = ".update-prev"
PENDING = ".update-pending.json"
HASH_FILE = ".update_hash"
GUARD_EVENTS = ".guard-events"
MAX_ATTEMPTS = 2

GUARD_SRC = "script/arfea-controller-guard.sh"
GUARD_MARKER = b"# arfea-controller-guard: fine"
GUARD_BIN = "/usr/local/sbin/arfea-controller-guard"
GUARD_SERVICE = "/etc/systemd/system/arfea-controller-guard.service"
GUARD_TIMER = "/etc/systemd/system/arfea-controller-guard.timer"


# ---------------------------------------------------------------------------
# File scritti per intero o per niente
# ---------------------------------------------------------------------------

def _fsync(path: Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def write_text_atomic(path: Path, text: str) -> None:
    """Scrive in un file temporaneo accanto, fsync, poi rename sopra l'originale."""
    tmp = path.with_name(f".{path.name}.arfea-new")
    tmp.write_text(text)
    _fsync(tmp)
    os.replace(tmp, path)


def atomic_copy(src: Path, dst: Path, uid: int | None = None, gid: int | None = None,
                mode: int | None = None) -> bool:
    """Copia src su dst passando da un file nascosto accanto (OpenHAB ignora i
    file che cominciano col punto) e un rename. False se dst era gia' uguale
    (permessi e proprietario si sistemano comunque)."""
    if dst.is_file() and filecmp.cmp(src, dst, shallow=False):
        st = dst.stat()
        if mode is not None and st.st_mode & 0o7777 != mode:
            dst.chmod(mode)
        if uid is not None and (st.st_uid, st.st_gid) != (uid, gid if gid is not None else uid):
            os.chown(dst, uid, gid if gid is not None else uid)
        return False
    tmp = dst.with_name(f".{dst.name}.arfea-new")
    shutil.copy2(src, tmp)
    if mode is not None:
        tmp.chmod(mode)
    if uid is not None:
        os.chown(tmp, uid, gid if gid is not None else uid)
    _fsync(tmp)
    os.replace(tmp, dst)
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Controllo dei file col MANIFEST
# ---------------------------------------------------------------------------

def _read_manifest(root: Path) -> dict[str, str]:
    """{percorso relativo: sha256} dal MANIFEST.sha256 (formato di sha256sum)."""
    out: dict[str, str] = {}
    try:
        text = (root / MANIFEST).read_text()
    except OSError:
        return out
    for line in text.splitlines():
        digest, sep, rel = line.partition("  ")
        if sep and len(digest) == 64:
            out[rel] = digest
    return out


def _installed_in_dest(rel: str) -> bool:
    """I file del tarball che restano nella cartella del controller: lo skeleton
    di OpenHAB va in openhab/, e config/ non lo tocca nessun aggiornamento."""
    return not rel.startswith(("config/", "skeleton-openhab/conf/", "skeleton-openhab/cont-init.d/"))


def verify_installed(dest: Path) -> list[str]:
    """File della cartella del controller diversi dal MANIFEST (mancanti, vuoti,
    contenuto diverso). Lista vuota anche quando il MANIFEST non c'e' (tarball
    fino alla 1.8.15): li' non si puo' dire niente."""
    bad = []
    for rel, digest in _read_manifest(dest).items():
        if not _installed_in_dest(rel):
            continue
        try:
            if _sha256(dest / rel) != digest:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    return bad


# ---------------------------------------------------------------------------
# Installazione
# ---------------------------------------------------------------------------

def _extract(tarball: Path, staging: Path) -> dict[str, int]:
    """Estrae il tarball (wrapper arfea-controller/ tolto, config/ saltata) in
    staging. Ritorna {percorso relativo: dimensione} dei file."""
    sizes: dict[str, int] = {}
    with tarfile.open(tarball, "r:xz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            relative = parts[1].rstrip("/")
            if relative == "config" or relative.startswith("config/"):
                continue
            if relative.startswith("/") or ".." in relative.split("/"):
                raise RuntimeError(f"percorso non valido nel tarball: {member.name}")
            if not (member.isfile() or member.isdir()):
                continue
            member.name = relative
            tar.extract(member, staging)
            if member.isfile():
                sizes[relative] = member.size
    return sizes


def _verify_staging(staging: Path, sizes: dict[str, int]) -> list[str]:
    bad = []
    for rel, size in sizes.items():
        try:
            if (staging / rel).stat().st_size != size:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    for rel, digest in _read_manifest(staging).items():
        if rel.startswith("config/"):
            continue
        try:
            if _sha256(staging / rel) != digest:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    return bad


def _swap(new: Path, live: Path, old: Path) -> None:
    """Mette new al posto di live, e live in old: due rename, atomici."""
    live.parent.mkdir(parents=True, exist_ok=True)
    old.parent.mkdir(parents=True, exist_ok=True)
    if live.exists() or live.is_symlink():
        os.rename(live, old)
    os.rename(new, live)


def install(tarball: Path, dest: Path, deploy_skeleton: Callable[[Path], None]) -> None:
    """Installa il tarball in dest. deploy_skeleton(skeleton_dir) porta lo
    skeleton nella conf di OpenHAB. Solleva RuntimeError se l'estrazione non
    torna: in quel caso dest non e' stato toccato."""
    staging = dest / STAGING
    prev = dest / PREV
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    sizes = _extract(tarball, staging)
    os.sync()
    bad = _verify_staging(staging, sizes)
    if bad:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"tarball estratto male ({len(bad)} file: {', '.join(bad[:5])})")

    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir()

    skeleton = staging / "skeleton-openhab"
    if skeleton.is_dir():
        deploy_skeleton(skeleton)
        # la ui/ resta nella cartella del controller: i widget si importano via
        # REST al prossimo avvio (_maybe_import_ui) o con /api/system/import-ui
        if (skeleton / "ui").is_dir():
            _swap(skeleton / "ui", dest / "skeleton-openhab" / "ui", prev / "skeleton-openhab-ui")
        shutil.rmtree(skeleton)

    for item in sorted(os.listdir(staging)):
        if item == "config":
            continue  # config/ e' protetto: mai sovrascritto/rimosso da un update
        _swap(staging / item, dest / item, prev / item)

    os.sync()
    shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spazio per la build (Redmine #365)
# ---------------------------------------------------------------------------

# Una build da zero (cambia l'immagine base python:3.11-slim, e la cache non vale
# piu') con l'image store di containerd tiene insieme cache, contenuto e
# snapshot: sulla centralina di test 762 MB liberi non sono bastati e
# l'esportazione e' morta con «no space left on device».
BUILD_SPACE_MB = 1500


def _free_mb(path: Path) -> int | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize // (1024 * 1024)


def build_free_mb(paths: list[Path]) -> int | None:
    """Il minimo dello spazio libero fra i percorsi che esistono."""
    values = [v for v in (_free_mb(p) for p in paths) if v is not None]
    return min(values) if values else None


def ensure_build_space(paths: list[Path]) -> str:
    """'' se c'e' spazio per ricostruire l'immagine, altrimenti il motivo. Se
    manca, prima toglie le immagini orfane e la cache di build (sull'host)."""
    free = build_free_mb(paths)
    if free is None or free >= BUILD_SPACE_MB:
        return ""
    logger.warning("Spazio per la build: %d MB liberi, ne servono %d: tolgo immagini "
                   "orfane e cache di build", free, BUILD_SPACE_MB)
    # A gradini: la cache ancora valida rende la build questione di secondi, e
    # va tolta solo se senza non c'e' posto.
    for cmd in (["docker", "image", "prune", "-f"], ["docker", "builder", "prune", "-f"],
                ["docker", "builder", "prune", "-af"]):
        _host(cmd, timeout=600)
        free = build_free_mb(paths)
        if free is None or free >= BUILD_SPACE_MB:
            return ""
    return (f"spazio insufficiente per ricostruire il controller: {free} MB liberi anche dopo "
            f"aver tolto immagini orfane e cache di build, ne servono {BUILD_SPACE_MB}")


# ---------------------------------------------------------------------------
# Aggiornamento in sospeso
# ---------------------------------------------------------------------------

def read_pending(dest: Path) -> dict:
    try:
        return json.loads((dest / PENDING).read_text())
    except (OSError, ValueError):
        return {}


def write_pending(dest: Path, data: dict) -> None:
    write_text_atomic(dest / PENDING, json.dumps(data))


def confirm(dest: Path, running_version: str) -> str:
    """All'avvio: se l'aggiornamento in sospeso era verso la versione che gira,
    registra il suo hash in .update_hash (da qui non si riapplica piu'). Ritorna
    la versione confermata, o ""."""
    pending = read_pending(dest)
    if not pending or pending.get("version") != running_version or not pending.get("hash"):
        return ""
    write_text_atomic(dest / HASH_FILE, pending["hash"])
    (dest / PENDING).unlink(missing_ok=True)
    return running_version


def attempts(dest: Path, new_hash: str) -> int:
    """Quante volte questo tarball e' gia' stato tentato senza arrivare in fondo
    (0 se e' nuovo)."""
    pending = read_pending(dest)
    return int(pending.get("attempts", 0)) if pending.get("hash") == new_hash else 0


# ---------------------------------------------------------------------------
# Guardiano sull'host
# ---------------------------------------------------------------------------

_SERVICE = """[Unit]
Description=ARFEA: guardiano del controller (lo ricrea se manca, torna indietro se non parte)
After=docker.service

[Service]
Type=oneshot
Environment=ARFEA_DIR={dest}
ExecStart=/bin/bash {bin} check
"""

_TIMER = """[Unit]
Description=ARFEA: guardiano del controller ogni 5 minuti

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
"""


def _host(cmd: list[str], data: bytes | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["nsenter", "-t", "1", "-m", "--", *cmd], input=data,
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 124, b"", str(exc).encode())


def _host_write(path: str, data: bytes, mode: str) -> bool:
    """Scrive un file sull'host, per intero o per niente. False se era gia' uguale."""
    cur = _host(["cat", path])
    if cur.returncode == 0 and cur.stdout == data:
        return False
    res = _host(["sh", "-c", 'set -e; t="$1.arfea-new"; cat >"$t"; chmod "$2" "$t"; sync; mv -f "$t" "$1"; sync',
                 "sh", path, mode], data=data)
    if res.returncode != 0:
        raise RuntimeError(f"{path}: {res.stderr.decode(errors='replace').strip()}")
    return True


def install_host_guard(dest: Path) -> tuple[bool, str]:
    """Installa o aggiorna il guardiano sull'host. Il timer si abilita solo
    quando le unita' sono nuove o cambiate: se qualcuno lo ha disabilitato a
    mano (manutenzione), un riavvio del controller non lo riaccende."""
    try:
        script = (dest / GUARD_SRC).read_bytes()
    except OSError:
        return False, f"{GUARD_SRC} assente: guardiano non installato"
    if GUARD_MARKER not in script:
        return False, f"{GUARD_SRC} incompleto: guardiano non installato"
    try:
        bin_changed = _host_write(GUARD_BIN, script, "0755")
        units_changed = _host_write(GUARD_SERVICE, _SERVICE.format(dest=dest, bin=GUARD_BIN).encode(), "0644")
        units_changed |= _host_write(GUARD_TIMER, _TIMER.encode(), "0644")
    except RuntimeError as exc:
        return False, f"guardiano non installato: {exc}"
    if units_changed:
        for cmd in (["systemctl", "daemon-reload"],
                    ["systemctl", "enable", "--now", "arfea-controller-guard.timer"]):
            res = _host(cmd)
            if res.returncode != 0:
                return False, f"{' '.join(cmd)}: {res.stderr.decode(errors='replace').strip()}"
        return True, "guardiano installato e timer acceso"
    return True, "guardiano aggiornato" if bin_changed else "guardiano gia' a posto"


def take_guard_events(dest: Path) -> list[tuple[str, str]]:
    """Le cose fatte dal guardiano mentre il controller non c'era, come
    (ora, messaggio); il file si svuota."""
    path = dest / GUARD_EVENTS
    taken = path.with_name(path.name + ".letti")
    try:
        os.replace(path, taken)
    except OSError:
        return []
    out = []
    try:
        for line in taken.read_text(errors="replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append((parts[0], parts[2]))
    finally:
        taken.unlink(missing_ok=True)
    return out
