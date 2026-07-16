from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from .backup import BackupManager
from .config import ConfigManager
from .docker_manager import DockerManager
from .release_manager import ReleaseManager
from .models import (
    BackupStatus,
    LinphoneConfigUpdate,
    NetworkInfo,
    OperationResponse,
    ReleaseCheckResult,
    ReleaseUpdateStatus,
    SerialDevice,
    ServiceDevicesUpdate,
    ServiceStatus,
    SystemInfo,
)

logger = logging.getLogger(__name__)

# Versione del controller: bump ad ogni release di feature (traccia versione -> features)
#   1.0.0  baseline (servizi, backup/restore, VPN, self-update)
#   1.1.0  chiamata di emergenza via linphone + TTS offline (pico2wave)
#   1.1.1  fix linphone: attesa demone pronto + registrazione effettiva prima del dial
#   1.2.0  TTS chiamate via gTTS (voce naturale, WAV 8kHz) + fallback espeak-ng
#   1.2.1  fix self-update: rebuild in unità systemd transitoria (non muore con la recreate)
#   1.3.0  aggiornamento versioni immagini ("release certificate"): manifest releases.json,
#          check + apply con migrazioni, conferma utente, health-gate e rollback
#   1.4.0  conferma aggiornamento software-per-software; reimport widget UI via OTA;
#          auto-migrazione arfea.yml (releases_url); FIX anti-downgrade: un container
#          che gira ma non ancora healthy non innesca più il rollback (niente downgrade)
#   1.4.1  FIX avvio: i device host assenti (es. /dev/ttyUSB0 su centralina senza
#          modbus) vengono saltati con warning invece di lasciare il container in
#          stato "created" senza log; openhab parte anche senza seriale collegata
#   1.5.0  gestione porte seriali (device) dalla Web UI: endpoint devices +
#          rilevamento porte host (/proc/1/root/dev); UI per aggiungere/togliere
#          device e ricreare il servizio. Aggiornamento immagini (release) dalla
#          UI con selezione per-servizio (es. solo openhab) e progress bar.
#          FIX OTA anti-downgrade: startup e "Aggiorna controller" confrontano la
#          VERSION del tarball con quella in esecuzione (niente più downgrade a un
#          tarball più vecchio/diverso su una install appena fatta).
#   1.5.1  install.sh: rimuove la sottocartella sorgente ridondante se la repo è
#          clonata dentro il target; label ai campi numerici linphone (UI);
#          import-ui-components.sh usa un token Karaf (Bearer) invece della Basic
#          Auth (che OpenHAB rifiuta → falso "password errata"); nuova pagina UI
#          page_arfeaController; import widget/sitemap anche nella migrazione.
#   1.5.2  rimosse le regole DSL di default obsolete (skeleton conf/rules/core.rules:
#          Send Push Message/Broadcast via cloud non connesso; Change log level su
#          item inesistenti, funzione ora in arfea_system.js): andavano in errore.
#   1.5.3  FIX import UI/OTA/reboot (3 bug in cascata scoperti sul campo):
#          a) host con AppArmor bloccava nsenter → docker-compose security_opt
#             apparmor:unconfined (senza, saltavano reboot, self-update e import UI);
#          b) console Karaf: il comando come argomento su OpenHAB 5.x non esegue
#             (stampa "Closed") → passato via stdin (karaf_console) + estrazione
#             token oh.<nome>.<segreto>;
#          c) i componenti UI nuovi vanno CREATI con POST sul namespace: il PUT
#             sul singolo uid dà 404 se non esiste → ora PUT, e su 404 POST.
#   1.5.4  FIX pagine UI vuote: il contenuto va sotto "slots" (default/masonry/
#          grid/canvas). Le chiavi top-level blocks:/masonry:/... NON sono nel
#          modello componente e la REST le SCARTA in silenzio: le pagine si
#          importavano senza contenuto. Rimossa page_sitemap (oh-sitemap-page non
#          esiste più in OpenHAB 5.2: la sitemap si gestisce in Settings →
#          Sitemaps e si vede in BasicUI). Nuovo item arfea_controller_version
#          (da /system/info) mostrato in widget e sitemap sotto la versione OpenHAB.
VERSION = "1.5.4"

# -- Globals initialised at startup -----------------------------------------

config_manager: ConfigManager
docker_manager: DockerManager
backup_manager: BackupManager
release_manager: ReleaseManager

HOST_PROC = "/host/proc"

# Network interfaces to check (same as system_operations.sh)
LAN_INTERFACES = ["eth0", "end0", "enp2s0", "enp3s0", "wlan0"]
VPN_INTERFACES = ["tun0", "wg0"]


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

# Backup di arfea.yml tenuto FUORI dalla cartella config/, così sopravvive
# anche a uno svuotamento di config/ (update interrotto, codice vecchio, ecc.).
_CONFIG_BACKUP = Path("/opt/docker_store/arfea-controller/.arfea.yml.bak")


def _backup_config(config_path: str) -> None:
    """Salva una copia di arfea.yml fuori da config/ dopo un load riuscito."""
    try:
        src = Path(config_path)
        if src.exists():
            _CONFIG_BACKUP.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, _CONFIG_BACKUP)
    except Exception as exc:  # best-effort, non deve mai bloccare lo startup
        logger.warning("Backup arfea.yml fallito: %s", exc)


def _restore_config_if_missing(config_path: str) -> None:
    """Se arfea.yml manca ma esiste il backup off-config, lo ripristina."""
    try:
        dst = Path(config_path)
        if not dst.exists() and _CONFIG_BACKUP.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_CONFIG_BACKUP, dst)
            logger.warning("arfea.yml mancante: ripristinato dal backup automatico %s", _CONFIG_BACKUP)
    except Exception as exc:
        logger.error("Ripristino arfea.yml fallito: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config_manager, docker_manager, backup_manager, release_manager

    config_path = os.environ.get("CONFIG_PATH", "/data/arfea.yml")

    # Auto-guarigione config: se arfea.yml è sparito (es. update interrotto),
    # lo ripristiniamo dal backup tenuto FUORI da config/ (sopravvive a uno
    # svuotamento della cartella config). Vedi _backup_config() dopo il load.
    _restore_config_if_missing(config_path)

    config_manager = ConfigManager(config_path)
    config_manager.load()

    # Auto-migrazione schema: centraline aggiornate da versioni precedenti non
    # hanno releases_url (arfea.yml è protetto dall'OTA). Lo deriviamo da update_url.
    config_manager.ensure_release_schema()

    # Aggiorna il backup off-config dopo un load riuscito
    _backup_config(config_path)

    docker_manager = DockerManager(config_manager)
    backup_manager = BackupManager(config_manager.config.backup, docker_manager)
    release_manager = ReleaseManager(config_manager, docker_manager, backup_manager)

    # Check for updates at startup (before starting services)
    if _check_startup_update():
        logger.info("Aggiornamento in corso, il controller si riavvierà...")
        yield
        return

    logger.info("Starting all enabled services...")
    results = docker_manager.start_all_enabled()
    for r in results:
        if not r.success:
            logger.warning("Startup issue: %s", r.message)

    # Reimport widget UI se cambiati (post-OTA): in thread per non ritardare lo startup
    threading.Thread(target=_maybe_import_ui, daemon=True).start()

    logger.info("ARFEA Controller ready")
    yield


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

STATIC_DIR = Path(__file__).parent / "static"

# Private IP ranges (RFC1918 + loopback + Docker bridge)
import ipaddress

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("11.0.0.0/8"),       # VPN ARFEA
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return False


def _is_trusted(ip_str: str) -> bool:
    """Trusted sources that don't need API key:
    - Loopback (127.x.x.x, ::1)
    - Docker bridge networks (172.16.0.0/12)
    - VPN networks (10.0.0.0/8, 11.0.0.0/8)
    """
    try:
        addr = ipaddress.ip_address(ip_str)
        return (
            addr in ipaddress.ip_network("127.0.0.0/8")
            or addr == ipaddress.ip_address("::1")
            or addr in ipaddress.ip_network("172.16.0.0/12")
            or addr in ipaddress.ip_network("10.0.0.0/8")
            or addr in ipaddress.ip_network("11.0.0.0/8")
        )
    except ValueError:
        return False


app = FastAPI(title="ARFEA Controller", version=VERSION, lifespan=lifespan)


@app.get("/", include_in_schema=False)
def serve_ui(request: Request):
    client_ip = request.client.host if request.client else ""
    if not _is_private_ip(client_ip):
        raise HTTPException(403, "Access denied: LAN only")
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# Security: API Key + LAN restriction
# ---------------------------------------------------------------------------

async def verify_api_key(request: Request, api_key: str = Depends(api_key_header)):
    """Access control:
    - localhost (OpenHAB, same host): full access, no API key needed
    - LAN (private IP): requires API key
    - External: blocked
    """
    client_ip = request.client.host if request.client else ""

    # Block non-private IPs entirely
    if not _is_private_ip(client_ip):
        raise HTTPException(403, "Access denied: LAN only")

    # Trusted sources: localhost, Docker bridge, VPN
    if _is_trusted(client_ip):
        return

    # LAN requires API key
    configured_key = config_manager.config.controller.api_key
    if not configured_key:
        return  # No API key configured, allow all LAN
    if api_key != configured_key:
        raise HTTPException(401, "Invalid or missing API key")


# ---------------------------------------------------------------------------
# Health (no auth - used by Docker healthcheck)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "managed_services": len(config_manager.config.services),
    }


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@app.get("/api/services", response_model=list[ServiceStatus], dependencies=[Depends(verify_api_key)])
def list_services():
    return docker_manager.get_all_statuses()


@app.get("/api/services/{name}", response_model=ServiceStatus, dependencies=[Depends(verify_api_key)])
def get_service(name: str):
    try:
        return docker_manager.get_service_status(name)
    except KeyError:
        raise HTTPException(404, f"Service '{name}' not found")


@app.post("/api/services/{name}/start", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def start_service(name: str):
    if name not in config_manager.config.services:
        raise HTTPException(404, f"Service '{name}' not found")
    return docker_manager.create_and_start(name)


@app.post("/api/services/{name}/restart", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def restart_service(name: str):
    if name not in config_manager.config.services:
        raise HTTPException(404, f"Service '{name}' not found")
    return docker_manager.restart_service(name)


@app.post("/api/services/{name}/recreate", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def recreate_service(name: str):
    """Stop+remove+create-and-start: applica eventuali modifiche di arfea.yml."""
    if name not in config_manager.config.services:
        raise HTTPException(404, f"Service '{name}' not found")
    docker_manager.stop_service(name)
    return docker_manager.create_and_start(name)


@app.get("/api/services/{name}/devices", dependencies=[Depends(verify_api_key)])
def get_service_devices(name: str):
    """Ritorna i mapping device (porte seriali) configurati per il servizio."""
    svc = config_manager.config.services.get(name)
    if svc is None:
        raise HTTPException(404, f"Service '{name}' not found")
    return {"name": name, "devices": svc.devices}


@app.put("/api/services/{name}/devices", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def set_service_devices(name: str, body: ServiceDevicesUpdate):
    """Aggiorna i device del servizio in arfea.yml e lo ricrea per applicarli.

    Sostituisce la pre-configurazione nel template: le porte seriali si
    gestiscono da qui. Il servizio va RICREATO (non solo riavviato) perché i
    device sono fissati alla creazione del container."""
    if name not in config_manager.config.services:
        raise HTTPException(404, f"Service '{name}' not found")
    try:
        saved = config_manager.set_service_devices(name, body.devices)
    except KeyError:
        raise HTTPException(404, f"Service '{name}' not found")

    status = docker_manager.get_service_status(name)
    if status.state.value == "running":
        docker_manager.stop_service(name)
        res = docker_manager.create_and_start(name)
        return OperationResponse(
            success=res.success,
            message=f"Device salvati ({len(saved)}); '{name}': {res.message}",
            details={"devices": saved},
        )
    return OperationResponse(
        success=True,
        message=f"Device salvati ({len(saved)}); si applicheranno all'avvio di '{name}'",
        details={"devices": saved},
    )


@app.get("/api/system/serial-devices", response_model=list[SerialDevice], dependencies=[Depends(verify_api_key)])
def list_serial_devices():
    """Elenca le porte seriali rilevate sull'host (sorgenti per un mapping device)."""
    return docker_manager.list_host_serial_devices()


@app.put("/api/services/{name}/enable", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def enable_service(name: str):
    try:
        config_manager.set_service_enabled(name, True)
    except KeyError:
        raise HTTPException(404, f"Service '{name}' not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Resolve dependencies: auto-enable required services
    effective = config_manager.resolve_effective_enabled()
    results = []
    order = config_manager.get_startup_order()
    for svc_name in order:
        if effective.get(svc_name) and svc_name != name:
            svc = config_manager.config.services[svc_name]
            if not svc.enabled:
                # This is an auto-dependency, don't persist the change
                pass
        status = docker_manager.get_service_status(svc_name)
        if effective.get(svc_name) and status.state.value != "running":
            res = docker_manager.create_and_start(svc_name)
            results.append(f"{svc_name}: {res.message}")

    return OperationResponse(
        success=True,
        message=f"'{name}' enabled",
        details={"started": results} if results else None,
    )


@app.put("/api/services/{name}/disable", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def disable_service(name: str):
    try:
        config_manager.set_service_enabled(name, False)
    except KeyError:
        raise HTTPException(404, f"Service '{name}' not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # Stop the service
    stop_result = docker_manager.stop_service(name)

    # Re-evaluate dependencies: if no one needs mosquitto anymore, stop it
    effective = config_manager.resolve_effective_enabled()
    stopped = [name]
    for svc_name, svc in config_manager.config.services.items():
        if svc_name == name:
            continue
        if not svc.enabled and not effective.get(svc_name, False):
            # This service is no longer needed
            status = docker_manager.get_service_status(svc_name)
            if status.state.value == "running":
                docker_manager.stop_service(svc_name)
                stopped.append(svc_name)

    return OperationResponse(
        success=stop_result.success,
        message=f"'{name}' disabled",
        details={"stopped": stopped} if len(stopped) > 1 else None,
    )


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@app.get("/api/system/network", response_model=NetworkInfo, dependencies=[Depends(verify_api_key)])
def get_network_info():
    lan_ip = _detect_ip(LAN_INTERFACES)
    vpn_ip = _detect_ip(VPN_INTERFACES)
    external_ip = _get_external_ip()
    return NetworkInfo(lan_ip=lan_ip, vpn_ip=vpn_ip, external_ip=external_ip)


@app.get("/api/system/info", response_model=SystemInfo, dependencies=[Depends(verify_api_key)])
def get_system_info():
    hostname = _read_proc1_file("/etc/hostname").strip()
    uptime_raw = _read_host_file("proc/uptime")
    uptime_secs = float(uptime_raw.split()[0]) if uptime_raw else 0
    days = int(uptime_secs // 86400)
    hours = int((uptime_secs % 86400) // 3600)
    return SystemInfo(
        hostname=hostname,
        version=VERSION,
        uptime=f"{days}d {hours}h",
    )


@app.post("/api/system/reboot", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def reboot_system():
    try:
        subprocess.run(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager.Reboot",
                "boolean:true",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return OperationResponse(success=True, message="Reboot command sent")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("Reboot failed: %s", exc)
        return OperationResponse(success=False, message=f"Reboot failed: {exc}")


# ---------------------------------------------------------------------------
# VPN (OpenVPN on host)
# ---------------------------------------------------------------------------

_VPN_UNIT = "openvpn@arfea.service"


def _systemd_unit_action(action: str, unit: str) -> None:
    """Start/Stop/Restart a systemd unit via D-Bus (same as reboot uses)."""
    subprocess.run(
        [
            "dbus-send", "--system", "--print-reply",
            "--dest=org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            f"org.freedesktop.systemd1.Manager.{action}Unit",
            f"string:{unit}",
            "string:replace",
        ],
        check=True, capture_output=True, timeout=15,
    )


def _is_vpn_active() -> bool:
    """Check if a VPN interface (tun0, wg0) is present in the host's routing table."""
    try:
        route_lines = Path("/proc/1/net/route").read_text().splitlines()[1:]
        for line in route_lines:
            parts = line.split()
            if parts and parts[0] in VPN_INTERFACES:
                return True
        return False
    except OSError:
        return False


@app.get("/api/system/vpn", dependencies=[Depends(verify_api_key)])
def get_vpn_status():
    """Check if VPN is active by looking for tun0/wg0 in the host's routing table."""
    active = _is_vpn_active()
    return {"active": active, "state": "active" if active else "inactive"}


@app.post("/api/system/vpn/start", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def start_vpn():
    try:
        _systemd_unit_action("Start", _VPN_UNIT)
        return OperationResponse(success=True, message="VPN avviata")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("VPN start failed: %s", exc)
        return OperationResponse(success=False, message=f"Avvio VPN fallito: {exc}")


@app.post("/api/system/vpn/stop", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def stop_vpn():
    try:
        _systemd_unit_action("Stop", _VPN_UNIT)
        return OperationResponse(success=True, message="VPN fermata")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("VPN stop failed: %s", exc)
        return OperationResponse(success=False, message=f"Arresto VPN fallito: {exc}")


# ---------------------------------------------------------------------------
# Self-update
# ---------------------------------------------------------------------------

_update_in_progress = False
_UPDATE_TARBALL = Path("/tmp/arfea-controller-update.tar.xz")


def _compute_file_hash(path: Path) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _tarball_version(tarball: Path) -> str:
    """Estrae la VERSION del controller da un tarball OTA (arfea-controller.tar.xz).

    Legge la riga ``VERSION = "x.y.z"`` da arfea-controller/app/main.py DENTRO il
    tarball, senza estrarlo. Ritorna "" se non determinabile."""
    import re
    try:
        with tarfile.open(tarball, "r:xz") as tar:
            member = next(
                (m for m in tar.getmembers() if m.name.endswith("app/main.py")),
                None,
            )
            if member is None:
                return ""
            fobj = tar.extractfile(member)
            if fobj is None:
                return ""
            for raw in fobj.read().decode("utf-8", "replace").splitlines():
                m = re.match(r'\s*VERSION\s*=\s*"([^"]+)"', raw)
                if m:
                    return m.group(1)
    except Exception as exc:
        logger.warning("Lettura VERSION dal tarball fallita: %s", exc)
    return ""


def _version_tuple(v: str) -> tuple[int, ...]:
    """Converte 'x.y.z' in tuple di interi per confronto (componenti non numerici -> 0)."""
    out = []
    for p in v.split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def _get_hash_file() -> Path:
    """Path del file che memorizza l'hash dell'ultimo update applicato."""
    return Path(config_manager.config.controller.data_path) / "arfea-controller" / ".update_hash"


def _extract_and_install(tarball: Path, dest: Path) -> None:
    """Estrai il tarball e copia i file nella destinazione, saltando config/.

    Se il tarball contiene skeleton-openhab/, i file vengono copiati
    anche nelle directory di OpenHAB (items, regole JS, script, cont-init.d).
    """
    tmpdir = Path("/tmp/arfea-update")
    if tmpdir.exists():
        shutil.rmtree(tmpdir)
    tmpdir.mkdir()

    with tarfile.open(tarball, "r:xz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            relative = parts[1]
            if relative.startswith("config/"):
                continue
            member.name = relative
            tar.extract(member, tmpdir)

    tarball.unlink(missing_ok=True)

    # Rete di sicurezza: arfea.yml NON deve MAI essere toccato da un update.
    # Lo teniamo in memoria e lo ripristiniamo a fine procedura se sparisse,
    # qualunque sia la causa (skip filter bypassato, copia anomala, ecc.).
    config_file = dest / "config" / "arfea.yml"
    config_backup = config_file.read_bytes() if config_file.exists() else None

    # Deploy skeleton-openhab files to OpenHAB directories
    skeleton_dir = tmpdir / "skeleton-openhab"
    if skeleton_dir.is_dir():
        _deploy_openhab_files(skeleton_dir)
        # Persisti la ui/ nell'install dir del controller: i widget/pagine si
        # importano via REST (serve OpenHAB su), cosa che facciamo al prossimo
        # avvio del controller (_maybe_import_ui) o via endpoint /system/import-ui.
        ui_src = skeleton_dir / "ui"
        if ui_src.is_dir():
            ui_dst = dest / "skeleton-openhab" / "ui"
            if ui_dst.exists():
                shutil.rmtree(ui_dst)
            ui_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(ui_src, ui_dst)
        shutil.rmtree(skeleton_dir)

    # Copy remaining files to controller destination
    for item in os.listdir(tmpdir):
        if item == "config":
            continue  # config/ è protetto: mai sovrascritto/rimosso da un update
        src = tmpdir / item
        dst = dest / item
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    # Ripristina arfea.yml se per qualunque motivo fosse stato rimosso
    if config_backup is not None and not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_bytes(config_backup)
        logger.warning("arfea.yml ripristinato dopo update (era stato rimosso)")

    shutil.rmtree(tmpdir, ignore_errors=True)


def _deploy_openhab_files(skeleton_dir: Path) -> None:
    """Copia i file skeleton-openhab nelle directory di OpenHAB (owner 9001:9001)."""
    data_path = Path(config_manager.config.controller.data_path)
    openhab_base = data_path / "openhab"
    OH_UID = 9001
    OH_GID = 9001

    # conf/ → /opt/docker_store/openhab/conf/ (items, automation/js, scripts, services)
    skeleton_conf = skeleton_dir / "conf"
    if skeleton_conf.is_dir():
        openhab_conf = openhab_base / "conf"
        for src_file in skeleton_conf.rglob("*"):
            if not src_file.is_file():
                continue
            relative = src_file.relative_to(skeleton_conf)
            dst_file = openhab_conf / relative
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            os.chown(dst_file, OH_UID, OH_GID)
            logger.info("OpenHAB updated: conf/%s", relative)

    # cont-init.d/ → /opt/docker_store/openhab/cont-init.d/
    skeleton_init = skeleton_dir / "cont-init.d"
    if skeleton_init.is_dir():
        openhab_init = openhab_base / "cont-init.d"
        openhab_init.mkdir(parents=True, exist_ok=True)
        for src_file in skeleton_init.iterdir():
            if src_file.is_file():
                dst_file = openhab_init / src_file.name
                shutil.copy2(src_file, dst_file)
                dst_file.chmod(0o755)
                os.chown(dst_file, OH_UID, OH_GID)
                logger.info("OpenHAB updated: cont-init.d/%s", src_file.name)


# ---------------------------------------------------------------------------
# Import componenti UI (widget/pagine) in OpenHAB via REST
#
# I widget UI vivono nel JSONDB di OpenHAB, non su file: vanno importati via REST.
# L'OTA del controller li porta nel tarball ma NON può caricarli da solo se non
# reimportandoli. Qui il controller conia un token admin (come lo script di setup)
# ed esegue le PUT REST verso localhost:8080 entrando nel network namespace
# dell'host con nsenter (OpenHAB è network_mode: host).
# ---------------------------------------------------------------------------

_OH_ADMIN_USER = "admin"


def _ui_dir() -> Path:
    return Path(config_manager.config.controller.data_path) / "arfea-controller" / "skeleton-openhab" / "ui"


def _ui_hash_file() -> Path:
    return Path(config_manager.config.controller.data_path) / "arfea-controller" / ".ui_hash"


def _compute_ui_hash() -> str:
    ui_dir = _ui_dir()
    if not ui_dir.is_dir():
        return ""
    h = hashlib.sha256()
    for f in sorted(ui_dir.glob("*.yaml")):
        h.update(f.read_bytes())
    return h.hexdigest()


def _nsenter_net(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Esegue cmd nel network namespace dell'host (dove localhost:8080 = OpenHAB)."""
    return subprocess.run(
        ["nsenter", "-t", "1", "-n", "--", *cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _oh_rest_ready() -> bool:
    """True se la REST di OpenHAB risponde (200/401/403)."""
    try:
        res = _nsenter_net([
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "http://localhost:8080/rest/",
        ], timeout=10)
        return res.stdout.strip() in ("200", "401", "403")
    except Exception:
        return False


def _mint_oh_token() -> str:
    """Conia un API token admin via console Karaf (come lo script di setup)."""
    import re
    token_name = f"arfeaOTA{int(time.time())}"
    code, out = docker_manager.karaf_console(
        f"openhab:users addApiToken {_OH_ADMIN_USER} {token_name} arfea"
    )
    if code != 0:
        return ""
    # Il token ha forma oh.<nome>.<segreto>; lo estraiamo ignorando banner/prompt.
    tokens = re.findall(r"oh\.[A-Za-z0-9._-]+", out)
    return tokens[-1] if tokens else ""


def _import_ui_components(wait_ready: bool = True) -> tuple[bool, str]:
    """Reimporta tutti i widget/pagine da skeleton-openhab/ui in OpenHAB via REST."""
    ui_dir = _ui_dir()
    if not ui_dir.is_dir():
        return (False, "Nessuna cartella ui/ da importare")

    if wait_ready:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not _oh_rest_ready():
            time.sleep(5)
    if not _oh_rest_ready():
        return (False, "OpenHAB REST non raggiungibile")

    token = _mint_oh_token()
    if not token:
        return (False, "Impossibile generare il token admin OpenHAB")

    ok, ko = 0, 0
    for yml in sorted(ui_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yml.read_text())
            uid = data["uid"]
            if yml.name.startswith("widget_"):
                ctype = "ui:widget"
            elif yml.name.startswith("page_"):
                ctype = "ui:page"
            else:
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
                json.dump(data, tf)
                tf_path = tf.name
            # PUT aggiorna un componente esistente ma ritorna 404 se non c'è:
            # in quel caso lo si CREA con POST sul namespace. (Il PUT-solo non
            # creava mai i componenti nuovi al primo import.)
            def _curl(method: str, url: str) -> str:
                r = _nsenter_net([
                    "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", method,
                    url,
                    "-H", f"Authorization: Bearer {token}",
                    "-H", "Content-Type: application/json",
                    "-d", f"@{tf_path}",
                ])
                return r.stdout.strip()

            code = _curl("PUT", f"http://localhost:8080/rest/ui/components/{ctype}/{uid}")
            if code == "404":
                code = _curl("POST", f"http://localhost:8080/rest/ui/components/{ctype}")
            os.unlink(tf_path)
            if code in ("200", "201"):
                ok += 1
                logger.info("UI import: %s (%s) OK", yml.name, ctype)
            else:
                ko += 1
                logger.warning("UI import: %s FALLITO (HTTP %s)", yml.name, code)
        except Exception as exc:
            ko += 1
            logger.warning("UI import: %s errore: %s", yml.name, exc)

    if ok and not ko:
        _ui_hash_file().write_text(_compute_ui_hash())
    return (ko == 0, f"UI importati: {ok} ok, {ko} falliti")


def _maybe_import_ui() -> None:
    """All'avvio: reimporta i widget solo se sono cambiati dall'ultimo import
    (hash), tipicamente dopo un OTA che porta interfacce nuove. Eseguito in un
    thread per non ritardare la disponibilità del controller."""
    try:
        current = _compute_ui_hash()
        if not current:
            return
        stored = _ui_hash_file().read_text().strip() if _ui_hash_file().exists() else ""
        if current == stored:
            return
        logger.info("Widget UI cambiati: reimport in OpenHAB...")
        ok, detail = _import_ui_components(wait_ready=True)
        logger.info("Reimport UI: %s", detail)
    except Exception as exc:
        logger.warning("Reimport UI all'avvio fallito: %s", exc)


def _trigger_rebuild() -> None:
    """Avvia rebuild e restart del container in un'unità systemd TRANSITORIA.

    Cruciale: il rebuild NON deve essere figlio del container. Con `docker compose
    up --force-recreate`, Docker ferma il vecchio container e uccide tutti i
    processi del suo cgroup — incluso un eventuale `bash`/`nsenter` lanciato da
    qui. Risultato osservato: build completata, vecchio container fermato, ma il
    nuovo resta in stato "Created" e non parte mai (controller giù, versione
    invariata). `systemd-run` registra il comando come servizio gestito da PID 1,
    fuori dal cgroup del container: sopravvive alla recreate e completa l'avvio.
    """
    subprocess.Popen([
        "nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--",
        "systemd-run",
        "--unit=arfea-selfupdate",
        "--collect",            # rimuove l'unità a fine esecuzione (nome riusabile)
        "bash", "-c",
        "sleep 2 && cd /opt/docker_store/arfea-controller "
        "&& docker compose up -d --build --force-recreate 2>&1 "
        "| logger -t arfea-update",
    ])


def _nsenter_available() -> bool:
    """Verifica che nsenter possa entrare nei namespaces dell'host (richiede CAP_SYS_ADMIN)."""
    try:
        result = subprocess.run(
            ["nsenter", "-t", "1", "-m", "--", "true"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _check_startup_update() -> bool:
    """Controlla aggiornamenti all'avvio. Ritorna True se un update è stato avviato."""
    update_url = config_manager.config.controller.update_url
    if not update_url:
        return False

    logger.info("Controllo aggiornamenti da %s", update_url)

    hash_file = _get_hash_file()

    try:
        result = subprocess.run(
            ["curl", "-fsSL", "-o", str(_UPDATE_TARBALL), update_url],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("Controllo aggiornamenti: download fallito")
            _UPDATE_TARBALL.unlink(missing_ok=True)
            return False

        new_hash = _compute_file_hash(_UPDATE_TARBALL)
        old_hash = hash_file.read_text().strip() if hash_file.exists() else ""

        if new_hash == old_hash:
            logger.info("Nessun aggiornamento disponibile")
            _UPDATE_TARBALL.unlink(missing_ok=True)
            return False

        # Anti-downgrade: applichiamo solo se il tarball è una versione PIÙ RECENTE
        # di quella in esecuzione. Senza questo, una install appena fatta (build dal
        # sorgente) verrebbe sovrascritta al primo avvio dal tarball del server
        # anche se più vecchio/diverso — è esattamente il "torna a 1.4.0" osservato.
        # Registriamo comunque l'hash: così questo stesso tarball non viene più
        # rivalutato ad ogni boot (un tarball NUOVO avrà hash diverso e ripasserà).
        tarball_ver = _tarball_version(_UPDATE_TARBALL)
        if tarball_ver and _version_tuple(tarball_ver) <= _version_tuple(VERSION):
            logger.info(
                "Tarball OTA versione %s <= versione in esecuzione %s: nessun "
                "aggiornamento (niente downgrade).", tarball_ver, VERSION,
            )
            hash_file.write_text(new_hash)
            _UPDATE_TARBALL.unlink(missing_ok=True)
            return False

        # Pre-flight: senza CAP_SYS_ADMIN, _trigger_rebuild fallirebbe.
        # Meglio non applicare l'update e proseguire con lo startup normale.
        if not _nsenter_available():
            logger.warning(
                "Aggiornamento disponibile ma nsenter non funziona (manca CAP_SYS_ADMIN?). "
                "Skip update, proseguo con startup normale."
            )
            _UPDATE_TARBALL.unlink(missing_ok=True)
            return False

        logger.info("Nuovo aggiornamento trovato, applicazione in corso...")
        dest = Path(config_manager.config.controller.data_path) / "arfea-controller"

        hash_file.write_text(new_hash)
        _extract_and_install(_UPDATE_TARBALL, dest)

        logger.info("Aggiornamento applicato, rebuild e restart...")
        _trigger_rebuild()
        return True

    except Exception as exc:
        logger.warning("Controllo aggiornamenti fallito: %s", exc)
        _UPDATE_TARBALL.unlink(missing_ok=True)
        return False


def _do_self_update() -> None:
    """Background task: estrae il tarball GIÀ scaricato/verificato, rebuild e restart."""
    global _update_in_progress
    try:
        dest = Path(config_manager.config.controller.data_path) / "arfea-controller"
        logger.info("Update: estrazione e installazione...")
        _extract_and_install(_UPDATE_TARBALL, dest)
        logger.info("Update: rebuild e restart...")
        _trigger_rebuild()
        logger.info("Update: rebuild avviato, il controller si riavvierà a breve")
    except Exception as exc:
        logger.error("Update fallito: %s", exc)
    finally:
        _update_in_progress = False


@app.post("/api/system/update", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def self_update(background_tasks: BackgroundTasks):
    global _update_in_progress
    if _update_in_progress:
        return OperationResponse(success=False, message="Aggiornamento già in corso")

    update_url = config_manager.config.controller.update_url
    if not update_url:
        raise HTTPException(400, "update_url non configurato in arfea.yml")

    if not _nsenter_available():
        raise HTTPException(
            503, "Aggiornamento non possibile: nsenter non disponibile (manca CAP_SYS_ADMIN)."
        )

    # Scarichiamo subito (tarball di poche decine di KB) per poter confrontare la
    # versione e dare un esito immediato invece di un rebuild "cieco".
    try:
        subprocess.run(
            ["curl", "-fsSL", "-o", str(_UPDATE_TARBALL), update_url],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _UPDATE_TARBALL.unlink(missing_ok=True)
        return OperationResponse(success=False, message=f"Download aggiornamento fallito: {exc}")

    # Anti-downgrade anche sull'update manuale: mai tornare a una versione più
    # vecchia; se è identica non c'è nulla da fare.
    tarball_ver = _tarball_version(_UPDATE_TARBALL)
    if tarball_ver and _version_tuple(tarball_ver) < _version_tuple(VERSION):
        _UPDATE_TARBALL.unlink(missing_ok=True)
        return OperationResponse(
            success=False,
            message=(
                f"Il server offre una versione più vecchia ({tarball_ver}) di quella in "
                f"esecuzione ({VERSION}): aggiornamento annullato per evitare un downgrade."
            ),
        )
    if tarball_ver and _version_tuple(tarball_ver) == _version_tuple(VERSION):
        _get_hash_file().write_text(_compute_file_hash(_UPDATE_TARBALL))
        _UPDATE_TARBALL.unlink(missing_ok=True)
        return OperationResponse(success=False, message=f"Già aggiornato alla versione {VERSION}.")

    _get_hash_file().write_text(_compute_file_hash(_UPDATE_TARBALL))
    _update_in_progress = True
    background_tasks.add_task(_do_self_update)
    target = f" alla {tarball_ver}" if tarball_ver else ""
    return OperationResponse(success=True, message=f"Aggiornamento{target} avviato, il controller si riavvierà")


# ---------------------------------------------------------------------------
# Aggiornamento versioni immagini ("release certificate")
# ---------------------------------------------------------------------------

@app.get("/api/system/releases/check", response_model=ReleaseCheckResult, dependencies=[Depends(verify_api_key)])
def releases_check():
    """Verifica (non distruttiva) se esiste una release certificata più recente."""
    return release_manager.check()


@app.get("/api/system/releases/status", response_model=ReleaseUpdateStatus, dependencies=[Depends(verify_api_key)])
def releases_status():
    """Stato/avanzamento dell'ultimo (o corrente) aggiornamento di versione."""
    return release_manager.status


@app.post("/api/system/releases/apply", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def releases_apply(background_tasks: BackgroundTasks, services: str = ""):
    """Avvia l'aggiornamento verso l'ultima release certificata.

    ``services`` (opzionale, CSV): limita l'upgrade ai soli software indicati
    (conferma software-per-software). Se assente, aggiorna tutti quelli con una
    versione più recente disponibile. Operazione su conferma utente: backup,
    migrazioni (solo upgrade completo), recreate con health-gate, rollback se fallisce."""
    if release_manager.status.state.value in (
        "backup", "migrating_pre", "pulling", "recreating", "waiting_healthy", "migrating_post",
    ):
        return OperationResponse(success=False, message="Aggiornamento già in corso")
    if not config_manager.config.controller.releases_url:
        raise HTTPException(400, "releases_url non configurato in arfea.yml")

    selected = [s.strip() for s in services.split(",") if s.strip()] or None
    background_tasks.add_task(release_manager.run_apply, selected)
    msg = "Aggiornamento di versione avviato"
    if selected:
        msg += f" (servizi: {', '.join(selected)})"
    return OperationResponse(success=True, message=msg)


@app.post("/api/system/import-ui", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def import_ui():
    """Reimporta i widget/pagine UI in OpenHAB (dal set skeleton bundlato).
    Serve dopo un OTA che porta widget nuovi: l'OTA lo chiama da solo, questo è
    l'aggancio manuale."""
    ok, detail = _import_ui_components()
    return OperationResponse(success=ok, message=detail)


# ---------------------------------------------------------------------------
# Linphone (chiamata di emergenza)
# ---------------------------------------------------------------------------

_OH_UID = 9001
_OH_GID = 9001


def _sh_quote(value) -> str:
    """Quoting sicuro per un valore in un file .env sourced da bash."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _write_linphone_env(cfg) -> None:
    """Scrive linphone.env nel volume conf di OpenHAB (owner 9001:9001).

    Il file è letto sia dal cont-init.d (registrazione iniziale) sia dallo
    script di chiamata (conf/scripts/linphone_call.sh).
    """
    data_path = Path(config_manager.config.controller.data_path)
    scripts_dir = data_path / "openhab" / "conf" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    env_path = scripts_dir / "linphone.env"

    lines = [
        "# Generato da arfea-controller — NON modificare a mano",
        f"ENABLED={'1' if cfg.enabled else '0'}",
        f"SIP_HOST={_sh_quote(cfg.sip_host)}",
        f"SIP_USER={_sh_quote(cfg.sip_username)}",
        f"SIP_PASS={_sh_quote(cfg.sip_password)}",
        f"EMERGENCY_NUMBER={_sh_quote(cfg.emergency_number)}",
        f"CALL_TIMEOUT={int(cfg.call_timeout)}",
        f"REPEAT={int(cfg.repeat)}",
        f"MESSAGE={_sh_quote(cfg.message)}",
    ]
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o640)
    try:
        os.chown(env_path, _OH_UID, _OH_GID)
    except (PermissionError, OSError) as exc:
        logger.warning("chown linphone.env fallito: %s", exc)


@app.get("/api/linphone/config", dependencies=[Depends(verify_api_key)])
def get_linphone_config():
    """Configurazione linphone. La password non viene esposta (solo has_password)."""
    cfg = config_manager.config.linphone
    data = cfg.model_dump()
    data["sip_password"] = ""
    data["has_password"] = bool(cfg.sip_password)
    return data


@app.put("/api/linphone/config", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
def update_linphone_config(body: LinphoneConfigUpdate):
    data = body.model_dump(exclude_none=True)
    # password vuota = mantieni quella esistente
    if data.get("sip_password", None) == "":
        data.pop("sip_password")
    cfg = config_manager.set_linphone_config(data)
    _write_linphone_env(cfg)
    return OperationResponse(success=True, message="Configurazione linphone salvata")


@app.get("/api/linphone/status", dependencies=[Depends(verify_api_key)])
def get_linphone_status():
    cfg = config_manager.config.linphone
    registration = docker_manager.linphone_status() if cfg.enabled else "disabled"
    return {
        "enabled": cfg.enabled,
        "configured": bool(cfg.sip_host and cfg.sip_username and cfg.emergency_number),
        "registration": registration,
    }


@app.post("/api/linphone/call", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def linphone_call(
    background_tasks: BackgroundTasks,
    number: str | None = None,
    message: str | None = None,
):
    """Avvia una chiamata di emergenza. number/message opzionali sovrascrivono i default."""
    cfg = config_manager.config.linphone
    if not cfg.enabled:
        raise HTTPException(400, "linphone non abilitato")
    num = number or cfg.emergency_number
    msg = message or cfg.message
    if not num:
        raise HTTPException(400, "nessun numero di emergenza configurato")
    background_tasks.add_task(docker_manager.linphone_call, num, msg)
    return OperationResponse(success=True, message=f"Chiamata avviata verso {num}")


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

@app.post("/api/backup/run", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def run_backup(background_tasks: BackgroundTasks):
    background_tasks.add_task(backup_manager.run_backup)
    return OperationResponse(success=True, message="Backup started")


@app.get("/api/backup/config", dependencies=[Depends(verify_api_key)])
def get_backup_config():
    """Stato della destinazione backup. Non espone MAI le credenziali WebDAV:
    ritorna solo se sono configurate e l'host di destinazione (senza token)."""
    b = config_manager.config.backup
    def _is_set(v: str) -> bool:
        return bool(v) and not v.startswith("CAMBIARE")
    host = ""
    if _is_set(b.webdav_url):
        try:
            from urllib.parse import urlparse
            host = urlparse(b.webdav_url).hostname or ""
        except Exception:
            host = ""
    return {
        "configured": _is_set(b.webdav_url) and _is_set(b.webdav_user) and _is_set(b.webdav_password),
        "host": host,
    }


@app.get("/api/backup/status", response_model=BackupStatus, dependencies=[Depends(verify_api_key)])
def get_backup_status():
    return backup_manager.status


@app.get("/api/backup/list", dependencies=[Depends(verify_api_key)])
def list_backups():
    """List available backup files."""
    data_path = Path(config_manager.config.controller.data_path)
    backup_dir = data_path / "arfea-controller" / "backups"
    if not backup_dir.exists():
        return []
    files = sorted(backup_dir.glob("arfea-backup-*.tar.gz"), reverse=True)
    return [
        {
            "name": f.name,
            "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
            "date": f.name.split("-")[2] + "-" + f.name.split("-")[3] + "-" + f.name.split("-")[4].split("_")[0],
        }
        for f in files
    ]


@app.get("/api/backup/download/{backup_name}", dependencies=[Depends(verify_api_key)])
def download_backup(backup_name: str):
    """Download a backup file. Blocks path traversal."""
    if "/" in backup_name or ".." in backup_name:
        raise HTTPException(400, "Invalid backup name")

    data_path = Path(config_manager.config.controller.data_path)
    backup_file = data_path / "arfea-controller" / "backups" / backup_name
    if not backup_file.exists() or not backup_file.is_file():
        raise HTTPException(404, "Backup not found")

    return FileResponse(
        path=str(backup_file),
        filename=backup_name,
        media_type="application/gzip",
    )


@app.post("/api/backup/restore", response_model=OperationResponse, dependencies=[Depends(verify_api_key)])
async def run_restore(backup_name: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(backup_manager.run_restore, backup_name)
    return OperationResponse(success=True, message=f"Restore from '{backup_name}' started")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_host_file(relative_path: str) -> str:
    """Read a file from the host filesystem via bind-mounted /host/proc or /host/sys."""
    for base in ["/host"]:
        full = Path(base) / relative_path
        if full.exists():
            return full.read_text()
    return ""


def _read_proc1_file(path: str) -> str:
    """Read a file from the host's root filesystem via /proc/1/root (requires pid:host)."""
    full = Path(f"/proc/1/root{path}")
    try:
        return full.read_text()
    except OSError:
        return ""


def _detect_ip(interfaces: list[str]) -> str:
    """Detect the first available IPv4 address among the given interfaces.

    Reads from /proc/1/net/ (host's PID 1 network namespace, thanks to pid:host)
    and correlates route entries with interface addresses.
    """
    # Step 1: find which of our target interfaces appear in the host's route table
    # /proc/1/net/route has the host's routing table (PID 1 = host init)
    try:
        route_lines = Path("/proc/1/net/route").read_text().splitlines()[1:]
    except OSError:
        logger.warning("Cannot read /proc/1/net/route")
        return "N/A"

    active_ifaces = set()
    for line in route_lines:
        parts = line.split()
        if parts and parts[0] in interfaces:
            active_ifaces.add(parts[0])

    if not active_ifaces:
        return "N/A"

    # Step 2: parse /proc/1/net/fib_trie to find LOCAL addresses
    # and match them against the subnets from the route table
    try:
        fib_content = Path("/proc/1/net/fib_trie").read_text()
    except OSError:
        logger.warning("Cannot read /proc/1/net/fib_trie")
        return "N/A"

    # Collect all LOCAL /32 IPs from fib_trie
    local_ips: list[str] = []
    lines = fib_content.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|-- "):
            ip_candidate = stripped[4:].strip()
            # Check next lines for "/32 host LOCAL"
            for j in range(i + 1, min(i + 3, len(lines))):
                next_line = lines[j].strip()
                if "/32 host LOCAL" in next_line:
                    local_ips.append(ip_candidate)
                    break
                if next_line.startswith("|--") or next_line.startswith("+--"):
                    break

    # Step 3: match route entries to find the IP for our target interfaces
    # Parse route table: each entry has iface, destination (hex), gateway (hex)
    for iface in interfaces:
        if iface not in active_ifaces:
            continue
        for route_line in route_lines:
            parts = route_line.split()
            if len(parts) < 8 or parts[0] != iface:
                continue
            # Decode the subnet from the route entry
            try:
                dest_hex = parts[1]
                mask_hex = parts[7]
                dest_int = int(dest_hex, 16)
                mask_int = int(mask_hex, 16)
            except ValueError:
                continue

            # Skip default routes (mask=0 matches everything)
            if mask_int == 0:
                continue

            # Find a LOCAL IP that falls within this subnet
            for ip_str in local_ips:
                ip_parts = ip_str.split(".")
                if len(ip_parts) != 4:
                    continue
                try:
                    ip_int = (int(ip_parts[3]) << 24 | int(ip_parts[2]) << 16 |
                              int(ip_parts[1]) << 8 | int(ip_parts[0]))
                except ValueError:
                    continue
                if (ip_int & mask_int) == dest_int:
                    # Skip network/broadcast addresses
                    if ip_str.endswith(".0") or ip_str.endswith(".255"):
                        continue
                    return ip_str

    return "N/A"


def _get_external_ip() -> str:
    try:
        response = httpx.get("https://ipinfo.io/ip", timeout=5)
        if response.status_code == 200:
            return response.text.strip()
    except httpx.HTTPError:
        pass
    return "N/A"
