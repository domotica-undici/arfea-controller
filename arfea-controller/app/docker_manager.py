from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import docker
from docker.errors import APIError, NotFound
from docker.types import LogConfig

from .config import ConfigManager
from .models import (
    ContainerState,
    HealthState,
    OperationResponse,
    ServiceDefinition,
    ServiceStatus,
)

logger = logging.getLogger(__name__)

MANAGED_LABEL = "arfea.managed"

# Utente amministratore di OpenHAB (creato dal wizard al primo accesso).
_OH_ADMIN_USER = "admin"

# UID/GID del container OpenHAB: i file in /opt/docker_store/openhab devono essere suoi.
_OH_UID = 9001
_OH_GID = 9001

# Config che un servizio deve trovare al PRIMO avvio, pena un container che parte
# ma e' inutilizzabile: mosquitto senza listener non accetta connessioni,
# zwave-js-ui senza settings.json non sa ne' quale seriale aprire ne' che deve
# alzare il server WebSocket per il binding zwavejs di OpenHAB.
# Mappa: servizio -> [(sorgente sotto templates/, destinazione sotto data_path)].
_DEFAULT_CONFIGS: dict[str, list[tuple[str, str]]] = {
    "mosquitto": [("mosquitto/mosquitto.conf", "mosquitto/config/mosquitto.conf")],
    "zwave-js-ui": [("zwave-js-ui/settings.json", "zwave-js-ui/settings.json")],
    "zigbee2mqtt": [("zigbee2mqtt/configuration.yaml", "zigbee2mqtt/data/configuration.yaml")],
}


class DockerManager:
    def __init__(self, config_manager: ConfigManager):
        self.cfg = config_manager
        self.client = docker.DockerClient(base_url="unix:///var/run/docker.sock")

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def ensure_network(self) -> None:
        net_cfg = self.cfg.config.network
        try:
            self.client.networks.get(net_cfg.name)
            logger.info("Network '%s' already exists", net_cfg.name)
        except NotFound:
            ipam_pool = docker.types.IPAMPool(
                subnet=net_cfg.subnet, gateway=net_cfg.gateway
            )
            ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            self.client.networks.create(
                net_cfg.name, driver="bridge", ipam=ipam_config
            )
            logger.info("Network '%s' created", net_cfg.name)

    # ------------------------------------------------------------------
    # Container inspection
    # ------------------------------------------------------------------

    def _get_container(self, container_name: str):
        try:
            return self.client.containers.get(container_name)
        except NotFound:
            return None

    def get_service_status(self, name: str) -> ServiceStatus:
        services = self.cfg.config.services
        if name not in services:
            raise KeyError(f"Unknown service: {name}")

        svc = services[name]
        effective = self.cfg.resolve_effective_enabled()
        container = self._get_container(svc.container_name)

        state = ContainerState.NOT_CREATED
        health = HealthState.NONE
        if container is not None:
            container.reload()
            raw_state = container.status
            state = _map_container_state(raw_state)

            health_data = container.attrs.get("State", {}).get("Health")
            if health_data:
                health = _map_health_state(health_data.get("Status", ""))

        return ServiceStatus(
            name=name,
            container_name=svc.container_name,
            enabled=svc.enabled,
            effectively_enabled=effective.get(name, False),
            core=svc.core,
            state=state,
            health=health,
            image=svc.image,
            ports=svc.ports,
        )

    def get_all_statuses(self) -> list[ServiceStatus]:
        return [
            self.get_service_status(name)
            for name in self.cfg.config.services
        ]

    # ------------------------------------------------------------------
    # Container lifecycle
    # ------------------------------------------------------------------

    def create_and_start(self, name: str) -> OperationResponse:
        services = self.cfg.config.services
        if name not in services:
            return OperationResponse(success=False, message=f"Unknown service: {name}")

        svc = services[name]

        # Check that dependencies are running
        for dep_name, dep_cond in svc.depends_on.items():
            dep_svc = services.get(dep_name)
            if dep_svc is None:
                continue
            dep_container = self._get_container(dep_svc.container_name)
            if dep_container is None or dep_container.status != "running":
                return OperationResponse(
                    success=False,
                    message=f"Dependency '{dep_name}' is not running",
                )
            if dep_cond.condition == "healthy":
                if not self._wait_healthy(dep_svc.container_name, timeout=180):
                    return OperationResponse(
                        success=False,
                        message=f"Dependency '{dep_name}' did not become healthy",
                    )

        existing = self._get_container(svc.container_name)
        if existing is not None:
            existing.reload()
            if existing.status == "running":
                return OperationResponse(
                    success=True, message=f"'{name}' is already running"
                )
            # Container exists but stopped: remove and recreate
            try:
                existing.remove(force=True)
            except APIError as exc:
                return OperationResponse(
                    success=False,
                    message=f"Failed to remove old container: {exc}",
                )

        self._ensure_default_config(name)

        kwargs = self._build_run_kwargs(name, svc)
        try:
            self.client.containers.run(**kwargs)
            logger.info("Service '%s' started", name)
            return OperationResponse(success=True, message=f"'{name}' started")
        except APIError as exc:
            # Fallback: container con stesso nome esiste ma non era stato rilevato
            # (può capitare se il controller è stato riavviato lasciando container orfani).
            err_str = str(exc).lower()
            if "conflict" in err_str and ("already in use" in err_str or "name" in err_str):
                logger.warning(
                    "Conflitto nome '%s', rimuovo container esistente e riprovo...",
                    svc.container_name,
                )
                try:
                    stale = self.client.containers.get(svc.container_name)
                    stale.remove(force=True)
                    self.client.containers.run(**kwargs)
                    logger.info("Service '%s' started after cleanup", name)
                    return OperationResponse(success=True, message=f"'{name}' started (after cleanup)")
                except Exception as exc2:
                    logger.error("Cleanup fallito per '%s': %s", name, exc2)
                    return OperationResponse(success=False, message=f"Conflict + cleanup failed: {exc2}")

            logger.error("Failed to start '%s': %s", name, exc)
            return OperationResponse(success=False, message=str(exc))

    def _ensure_default_config(self, name: str) -> None:
        """Installa la config di default di un servizio se manca (no-clobber).

        Va fatto QUI, alla creazione del container, e non negli script di setup:
        un servizio puo' essere acceso in qualsiasi momento dalla Web UI — o
        auto-abilitato come dipendenza (mosquitto quando si accende
        zwave-js-ui/zigbee2mqtt) — anche anni dopo l'installazione. install.sh
        copriva solo il primo impianto e la migrazione solo se stessa: chi
        accendeva un servizio dalla UI si ritrovava un container senza config,
        che parte ma non funziona. Con l'aggancio qui tutti e tre i percorsi sono
        coperti da un'unica implementazione (le due negli script erano gia'
        andate alla deriva: install.sh creava mosquitto.conf, la migrazione no).

        No-clobber: una config esistente e' dell'utente e non va MAI toccata.
        Un template mancante non e' fatale — si logga e il container parte
        comunque, come faceva il vecchio codice negli script.
        """
        if name == "habapp":
            # HABApp non e' una copia di template: va scelto COSA deployare (le
            # funzioni attive) e serve un token OpenHAB. Import locale: e'
            # habapp_manager a dipendere da noi (karaf_console), non il contrario.
            from .habapp_manager import HABAppManager

            ok, msg = HABAppManager(self.cfg, self).provision()
            if not ok:
                # Non blocchiamo l'avvio: HABApp parte senza regole e la Web UI
                # mostra il motivo (status.token_ok / installed).
                logger.warning("HABApp: provisioning incompleto: %s", msg)
            return

        entries = _DEFAULT_CONFIGS.get(name)
        if not entries:
            return

        # data_path e' un path dell'HOST, ma il controller monta /opt/docker_store
        # 1:1 (vedi docker-compose.yml), quindi qui e' scrivibile direttamente.
        data_path = Path(self.cfg.config.controller.data_path)
        templates = data_path / "arfea-controller" / "templates"

        for src_rel, dst_rel in entries:
            dst = data_path / dst_rel
            if dst.exists():
                continue
            src = templates / src_rel
            if not src.is_file():
                logger.warning(
                    "Servizio '%s': template '%s' assente, '%s' non creata",
                    name, src, dst,
                )
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                logger.info("Servizio '%s': creata config di default %s", name, dst)
            except OSError as exc:
                logger.error(
                    "Servizio '%s': copia della config di default %s fallita: %s",
                    name, dst, exc,
                )

    def stop_service(self, name: str) -> OperationResponse:
        svc = self.cfg.config.services.get(name)
        if svc is None:
            return OperationResponse(success=False, message=f"Unknown service: {name}")

        container = self._get_container(svc.container_name)
        if container is None:
            return OperationResponse(
                success=True, message=f"'{name}' is not running (no container)"
            )
        try:
            container.stop(timeout=30)
            container.remove(force=True)
            logger.info("Service '%s' stopped and removed", name)
            return OperationResponse(success=True, message=f"'{name}' stopped")
        except APIError as exc:
            logger.error("Failed to stop '%s': %s", name, exc)
            return OperationResponse(success=False, message=str(exc))

    def restart_service(self, name: str) -> OperationResponse:
        svc = self.cfg.config.services.get(name)
        if svc is None:
            return OperationResponse(success=False, message=f"Unknown service: {name}")

        container = self._get_container(svc.container_name)
        if container is None:
            return self.create_and_start(name)
        try:
            container.restart(timeout=30)
            logger.info("Service '%s' restarted", name)
            return OperationResponse(success=True, message=f"'{name}' restarted")
        except APIError as exc:
            logger.error("Failed to restart '%s': %s", name, exc)
            return OperationResponse(success=False, message=str(exc))

    # ------------------------------------------------------------------
    # Aggiornamento immagini (release certificate)
    # ------------------------------------------------------------------

    def openhab_exec(self, cmd: list[str], timeout: int = 60) -> tuple[int, str]:
        """Esegue un comando DENTRO il container openhab. Ritorna (exit_code, output).
        (-1, msg) se il container non c'è o l'exec fallisce."""
        container = self._get_container("openhab")
        if container is None:
            return (-1, "container openhab non trovato")
        try:
            res = container.exec_run(cmd, demux=False)
            out = res.output.decode("utf-8", "replace") if res.output else ""
            return (res.exit_code, out)
        except APIError as exc:
            return (-1, f"exec fallito: {exc}")

    def karaf_console(self, command: str, timeout: int = 60) -> tuple[int, str]:
        """Esegue un comando nella console Karaf di OpenHAB e ritorna (rc, output).

        IMPORTANTE: su OpenHAB 5.x il client con il comando passato come ARGOMENTO
        (`client -p habopen "cmd"`) non lo esegue e stampa solo "Closed". Va passato
        via STDIN. Qui usiamo `sh -c` con una pipe, così funziona anche via
        docker exec non interattivo."""
        safe = command.replace("'", "'\\''")
        return self.openhab_exec(
            ["sh", "-c", f"echo '{safe}' | /openhab/runtime/bin/client -p habopen"],
            timeout,
        )

    def mint_oh_token(self, purpose: str = "arfea") -> str:
        """Conia un API token admin OpenHAB. Stringa vuota se non ci riesce.

        Serve a chiunque debba parlare con la REST di OpenHAB con privilegi di
        amministratore: la Basic Auth e' rifiutata di default
        (org.openhab.restauth:allowBasicAuth=false) e le richieste anonime
        ottengono il solo ruolo USER, che non basta per creare item o scrivere
        metadata (@RolesAllowed({Role.ADMIN})).
        """
        token_name = f"arfea{purpose}{int(time.time())}"
        code, out = self.karaf_console(
            f"openhab:users addApiToken {_OH_ADMIN_USER} {token_name} arfea"
        )
        if code != 0:
            logger.warning("Conio token OpenHAB fallito (rc=%s): %s", code, out.strip()[:200])
            return ""
        # Il token ha forma oh.<nome>.<segreto>; lo estraiamo ignorando banner/prompt.
        tokens = re.findall(r"oh\.[A-Za-z0-9._-]+", out)
        if not tokens:
            logger.warning("Conio token OpenHAB: nessun token nell'output della console")
            return ""
        return tokens[-1]

    def pull_image(self, image: str) -> OperationResponse:
        """Scarica un'immagine (repo:tag). Fallisce PRIMA di toccare i container,
        così un tag inesistente non lascia il servizio a metà aggiornamento."""
        repo, tag = _split_image_ref(image)
        try:
            self.client.images.pull(repository=repo, tag=tag)
            logger.info("Immagine scaricata: %s", image)
            return OperationResponse(success=True, message=f"Immagine '{image}' scaricata")
        except (APIError, NotFound) as exc:
            logger.error("Pull '%s' fallito: %s", image, exc)
            return OperationResponse(success=False, message=f"Pull '{image}' fallito: {exc}")

    def recreate_service(self, name: str, health_timeout: int = 600) -> OperationResponse:
        """Ricrea il container (per applicare un nuovo tag immagine) e verifica
        che parta.

        IMPORTANTE (anti-downgrade): il criterio di fallimento è "il container NON
        resta in esecuzione" (crash/exit), NON "non è diventato healthy in tempo".
        OpenHAB al primo avvio su una versione nuova può metterci molti minuti a
        diventare healthy: trattarlo come fallimento farebbe scattare un rollback
        e quindi un DOWNGRADE, che è peggio di un'attesa. Quindi un container che
        gira ma non è ancora healthy viene considerato OK (con warning)."""
        svc = self.cfg.config.services.get(name)
        if svc is None:
            return OperationResponse(success=False, message=f"Unknown service: {name}")

        self.stop_service(name)
        res = self.create_and_start(name)
        if not res.success:
            return res

        # Deve almeno restare in esecuzione (non andare in crash-loop) per ~20s.
        if not self._stays_running(name, seconds=20):
            return OperationResponse(
                success=False,
                message=f"'{name}' ricreato ma non resta in esecuzione (crash all'avvio)",
            )

        # Se ha un healthcheck, attendiamo 'healthy' ma senza mai fallire finché
        # il container gira: niente rollback/downgrade su semplice lentezza.
        if svc.healthcheck is not None:
            if self._wait_healthy(svc.container_name, timeout=health_timeout):
                return OperationResponse(success=True, message=f"'{name}' ricreato e healthy")
            still_running = self.get_service_status(name).state == ContainerState.RUNNING
            if still_running:
                logger.warning(
                    "'%s' avviato ma non healthy entro %ss: procedo comunque "
                    "(evito il downgrade), verifica lo stato più tardi.", name, health_timeout,
                )
                return OperationResponse(
                    success=True,
                    message=f"'{name}' avviato (health non ancora confermato dopo {health_timeout}s)",
                )
            return OperationResponse(
                success=False,
                message=f"'{name}' non healthy e non più in esecuzione",
            )

        return OperationResponse(success=True, message=f"'{name}' ricreato e in esecuzione")

    def _stays_running(self, name: str, seconds: int = 20) -> bool:
        """True se il container risulta 'running' e ci resta per il periodo indicato
        (rileva i crash-loop all'avvio)."""
        deadline = time.monotonic() + seconds
        seen_running = False
        while time.monotonic() < deadline:
            if self.get_service_status(name).state == ContainerState.RUNNING:
                seen_running = True
            elif seen_running:
                return False  # era partito e poi è uscito → crash
            time.sleep(3)
        return self.get_service_status(name).state == ContainerState.RUNNING

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def start_all_enabled(self) -> list[OperationResponse]:
        """Start all effectively-enabled services in dependency order."""
        self.ensure_network()
        order = self.cfg.get_startup_order()
        results = []
        for name in order:
            res = self.create_and_start(name)
            results.append(res)
            if not res.success:
                logger.warning(
                    "Service '%s' failed to start: %s", name, res.message
                )
        return results

    def stop_all(self, exclude: Optional[list[str]] = None) -> list[OperationResponse]:
        """Stop all managed containers. Returns which were running."""
        exclude = set(exclude or [])
        results = []
        for name, svc in self.cfg.config.services.items():
            if name in exclude:
                continue
            container = self._get_container(svc.container_name)
            if container is not None and container.status == "running":
                res = self.stop_service(name)
                results.append(res)
        return results

    def get_running_services(self) -> list[str]:
        """Return names of services whose container is currently running."""
        running = []
        for name, svc in self.cfg.config.services.items():
            container = self._get_container(svc.container_name)
            if container is not None and container.status == "running":
                running.append(name)
        return running

    # ------------------------------------------------------------------
    # Linphone (chiamata di emergenza eseguita DENTRO il container OpenHAB)
    # ------------------------------------------------------------------

    def _openhab_container(self):
        svc = self.cfg.config.services.get("openhab")
        name = svc.container_name if svc else "openhab"
        return self._get_container(name)

    def _prepare_tts_wav(self, message: str) -> None:
        """Genera il WAV del messaggio con gTTS (voce naturale, formato telefonico)
        e lo deposita nel volume sounds di OpenHAB perché lo script lo riproduca.

        Cancella sempre il WAV precedente: se gTTS fallisce (rete assente) il file
        non esiste e lo script ricade su espeak-ng offline, senza riusare audio stale.
        """
        from . import tts

        data_path = Path(self.cfg.config.controller.data_path)
        wav_path = data_path / "openhab" / "conf" / "sounds" / "arfea_call.wav"
        try:
            if wav_path.exists():
                wav_path.unlink()
        except OSError as exc:
            logger.warning("rimozione WAV TTS precedente fallita: %s", exc)
        tts.synthesize_wav(message, wav_path, uid=_OH_UID, gid=_OH_GID)

    def linphone_call(self, number: str, message: str) -> OperationResponse:
        """Esegue lo script di chiamata dentro il container OpenHAB via docker exec.

        Prima genera il WAV con gTTS (voce naturale) nel volume sounds; lo script
        (conf/scripts/linphone_call.sh) lo riproduce, registra al SIP e compone il
        numero. Bloccante: va invocato in un background task.
        """
        container = self._openhab_container()
        if container is None or container.status != "running":
            return OperationResponse(success=False, message="container OpenHAB non in esecuzione")
        self._prepare_tts_wav(message or "")
        try:
            # come utente 'openhab': stesso contesto di actions.Exec nelle regole OpenHAB
            res = container.exec_run(
                cmd=["/openhab/conf/scripts/linphone_call.sh", number, message or ""],
                user="openhab",
            )
            out = res.output.decode("utf-8", "replace").strip() if res.output else ""
            ok = res.exit_code == 0
            logger.info("linphone_call verso %s exit=%s: %s", number, res.exit_code, out)
            return OperationResponse(
                success=ok,
                message=(out[-300:] or "chiamata eseguita") if ok else (out[-300:] or "chiamata fallita"),
            )
        except APIError as exc:
            logger.error("linphone_call fallita: %s", exc)
            return OperationResponse(success=False, message=str(exc))

    def linphone_status(self) -> str:
        """Ritorna lo stato di registrazione SIP interrogando linphonecsh nel container."""
        container = self._openhab_container()
        if container is None or container.status != "running":
            return "openhab_down"
        try:
            res = container.exec_run(
                cmd=["linphonecsh", "status", "register"],
                user="openhab",
            )
            out = res.output.decode("utf-8", "replace").strip() if res.output else ""
            return out or "unknown"
        except APIError as exc:
            logger.warning("linphone_status fallito: %s", exc)
            return "error"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wait_healthy(self, container_name: str, timeout: int = 180) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            container = self._get_container(container_name)
            if container is None:
                return False
            container.reload()
            health = container.attrs.get("State", {}).get("Health", {})
            if health.get("Status") == "healthy":
                return True
            time.sleep(5)
        return False

    # ------------------------------------------------------------------
    # Porte seriali dell'host (per la gestione device dalla Web UI)
    # ------------------------------------------------------------------

    def list_host_serial_devices(self) -> list[dict]:
        """Enumera le porte seriali presenti sull'host, come sorgenti per un mapping.

        Il controller gira con ``pid: host``, quindi ``/proc/1/root`` è il
        filesystem dell'host: /proc/1/root/dev è la sua /dev reale (non quella
        del container). Preferiamo i nomi stabili in /dev/serial/by-id/ (non
        cambiano al riavvio), aggiungendo i /dev/ttyUSB* /dev/ttyACM* /dev/ttyAML*
        non ancora coperti da un by-id. Segnala anche quali porte sono già mappate
        da un servizio in arfea.yml (used_by)."""
        # /proc/1/root = host rootfs (pid:host); fallback a / se non accessibile.
        # Stessa rootfs usata da _split_present_devices al momento di creare i
        # container: se le due divergono, la UI mostra una seriale che poi il
        # controller scarta come "assente" (o viceversa).
        host_root = _host_root()
        dev = host_root / "dev"

        # Porte già mappate → per marcarle "used_by"
        used: dict[str, str] = {}
        for sname, svc in self.cfg.config.services.items():
            for d in svc.devices:
                src = d.split(":", 1)[0]
                used[src] = sname
                real = self._realpath(host_root, src)
                if real:
                    used.setdefault(real, sname)

        found: dict[str, dict] = {}

        by_id = dev / "serial" / "by-id"
        if _dir_ok(by_id):
            try:
                links = sorted(by_id.iterdir())
            except OSError:
                links = []
            for link in links:
                path = f"/dev/serial/by-id/{link.name}"
                resolved = self._realpath(host_root, path)
                found[path] = {
                    "path": path,
                    "resolved": resolved,
                    "used_by": used.get(path) or (used.get(resolved, "") if resolved else ""),
                }

        # tty grezzi non ancora rappresentati da un by-id (stesso target)
        resolved_targets = {v["resolved"] for v in found.values() if v["resolved"]}
        for pattern in ("ttyUSB*", "ttyACM*", "ttyAML*"):
            try:
                nodes = sorted(dev.glob(pattern))
            except OSError:
                nodes = []
            for node in nodes:
                path = f"/dev/{node.name}"
                if path in resolved_targets or path in found:
                    continue
                found[path] = {"path": path, "resolved": "", "used_by": used.get(path, "")}

        return list(found.values())

    @staticmethod
    def _realpath(host_root: Path, dev_path: str) -> str:
        """Risolve un symlink di /dev sull'host restituendo un path /dev/... assoluto."""
        try:
            target = os.path.realpath(str(host_root / dev_path.lstrip("/")))
        except OSError:
            return ""
        # target è nello spazio di host_root: riportalo a /dev/...
        hr = str(host_root)
        if hr != "/" and target.startswith(hr):
            target = target[len(hr):] or "/"
        return target if target.startswith("/dev/") else ""

    def _build_run_kwargs(self, name: str, svc: ServiceDefinition) -> dict:
        kwargs: dict = {
            "image": svc.image,
            "name": svc.container_name,
            "hostname": svc.container_name,
            "detach": True,
            "labels": {MANAGED_LABEL: "true", "arfea.service": name},
            "restart_policy": {"Name": svc.restart_policy, "MaximumRetryCount": 0},
            "log_config": LogConfig(
                type=LogConfig.types.JSON, config={"max-size": svc.log_max_size}
            ),
        }

        # Network
        net_name = self.cfg.config.network.name
        if svc.network_mode:
            kwargs["network_mode"] = svc.network_mode
        else:
            kwargs["network"] = net_name

        # Volumes
        if svc.volumes:
            binds = []
            for vol in svc.volumes:
                binds.append(vol)
            kwargs["volumes"] = _parse_volumes(svc.volumes)

        # Ports
        if svc.ports and not svc.network_mode:
            kwargs["ports"] = _parse_ports(svc.ports)

        # Environment
        if svc.environment:
            kwargs["environment"] = svc.environment

        # Devices — salta quelli il cui nodo host non esiste. Docker, davanti a un
        # device mappato ma assente (es. /dev/ttyUSB0 su una centralina senza
        # modbus), NON avvia il container e lo lascia in stato "created" senza
        # scrivere log: openhab risulterebbe "installato ma mai partito".
        # Meglio partire senza quel device (il binding fallirà, ma il sistema è su)
        # che restare a terra per una seriale scollegata.
        if svc.devices:
            present, missing = _split_present_devices(svc.devices)
            if missing:
                logger.warning(
                    "Servizio '%s': device host assenti, saltati: %s",
                    name, ", ".join(missing),
                )
            if present:
                kwargs["devices"] = present

        # Capabilities
        if svc.cap_add:
            kwargs["cap_add"] = svc.cap_add

        # Privileged
        if svc.privileged:
            kwargs["privileged"] = True

        # DNS
        if svc.dns:
            kwargs["dns"] = svc.dns

        # Group add (per accesso a device con permessi di gruppo)
        if svc.group_add:
            kwargs["group_add"] = svc.group_add

        # Healthcheck
        if svc.healthcheck:
            kwargs["healthcheck"] = {
                "test": svc.healthcheck.test,
                "interval": svc.healthcheck.interval * 10**9,  # nanoseconds
                "timeout": svc.healthcheck.timeout * 10**9,
                "retries": svc.healthcheck.retries,
                "start_period": svc.healthcheck.start_period * 10**9,
            }

        return kwargs


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_volumes(volume_list: list[str]) -> dict[str, dict]:
    """Convert ``host:container[:mode]`` strings to docker-py volumes dict."""
    result = {}
    for v in volume_list:
        parts = v.split(":")
        if len(parts) == 3:
            host, container, mode = parts
        elif len(parts) == 2:
            host, container = parts
            mode = "rw"
        else:
            continue
        result[host] = {"bind": container, "mode": mode}
    return result


def _dir_ok(p: Path) -> bool:
    """``p.is_dir()`` che non esplode: su /proc/1/root senza privilegi is_dir()
    SOLLEVA PermissionError invece di restituire False."""
    try:
        return p.is_dir()
    except OSError:
        return False


def _host_root() -> Path:
    """Rootfs dell'host vista dal controller.

    Il controller gira con ``pid: host`` ma NON monta /dev: nel suo mount
    namespace /dev è quello minimale di Docker (null, zero, pts, ...), SENZA
    nessuna seriale. Un ``os.path.exists('/dev/ttyUSB0')`` eseguito qui dentro è
    quindi sempre falso e non dice nulla sull'host. /proc/1/root è invece la
    rootfs reale dell'host (serve pid:host + SYS_PTRACE, entrambi nel compose).
    Fallback a '/' per l'esecuzione fuori container (sviluppo).
    """
    for r in (Path("/proc/1/root"), Path("/")):
        if _dir_ok(r / "dev"):
            return r
    return Path("/")


def _split_present_devices(devices: list[str]) -> tuple[list[str], list[str]]:
    """Separa i mapping ``host[:container[:perms]]`` in (presenti, host_assenti).

    Il path host è la prima parte prima di ':'. La presenza va verificata sulla
    rootfs dell'host (_host_root()), MAI nel mount namespace del controller: lì
    /dev non ha nessuna seriale e ogni device risulterebbe assente, facendo
    cadere in silenzio TUTTI i mapping (openhab senza modbus/zwave, zwave-js-ui
    senza stick → "cannot open /dev/zwave").

    ``os.path.exists`` segue i symlink, quindi un /dev/serial/by-id/... pendente
    = device non presente. I link udev by-id sono relativi (../../ttyUSB0) e si
    risolvono correttamente dentro _host_root().

    NB: nella lista restituita i path restano quelli ORIGINALI dell'host: è il
    daemon Docker (che gira sull'host) a doverli risolvere, non il controller.
    """
    root = _host_root()
    present: list[str] = []
    missing: list[str] = []
    for d in devices:
        host = d.split(":", 1)[0]
        if os.path.exists(str(root / host.lstrip("/"))):
            present.append(d)
        else:
            missing.append(host)
    return present, missing


def _split_image_ref(image: str) -> tuple[str, str]:
    """Separa ``repo:tag`` in (repo, tag), gestendo registry con porta.

    Il tag è dopo l'ultimo ':' che segue l'ultimo '/'. Esempi:
      openhab/openhab:5.2.0        -> ("openhab/openhab", "5.2.0")
      ghcr.io/foo/bar:1.2          -> ("ghcr.io/foo/bar", "1.2")
      registry:5000/foo/bar:1.2    -> ("registry:5000/foo/bar", "1.2")
      eclipse-mosquitto            -> ("eclipse-mosquitto", "latest")
    """
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon], image[last_colon + 1:]
    return image, "latest"


def _parse_ports(port_list: list[str]) -> dict[str, int]:
    """Convert ``host:container`` port strings to docker-py port bindings."""
    result = {}
    for p in port_list:
        parts = p.split(":")
        if len(parts) == 2:
            host_port, container_port = parts
            result[f"{container_port}/tcp"] = int(host_port)
    return result


def _map_container_state(raw: str) -> ContainerState:
    mapping = {
        "running": ContainerState.RUNNING,
        "exited": ContainerState.STOPPED,
        "created": ContainerState.STOPPED,
        "restarting": ContainerState.RESTARTING,
        "paused": ContainerState.PAUSED,
        "dead": ContainerState.DEAD,
    }
    return mapping.get(raw, ContainerState.STOPPED)


def _map_health_state(raw: str) -> HealthState:
    mapping = {
        "healthy": HealthState.HEALTHY,
        "unhealthy": HealthState.UNHEALTHY,
        "starting": HealthState.STARTING,
    }
    return mapping.get(raw, HealthState.NONE)
