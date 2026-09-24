from __future__ import annotations

import fcntl
import logging
import os
import re
from pathlib import Path

import yaml

from .models import (
    DEFAULT_UPDATE_URL, AccessPointConfig, ArfeaConfig, DependencyRule, LinphoneConfig,
    ServiceDefinition,
)

logger = logging.getLogger(__name__)

# Link di condivisione Nextcloud (https://host[/sub]/[index.php/]s/<token>): e' la
# pagina web della condivisione, non l'indirizzo WebDAV, e un PUT li' risponde 401.
# L'indirizzo per caricare in una condivisione pubblica e'
# https://host[/sub]/public.php/dav/files/<token> (Redmine #203).
_NC_SHARE_LINK = re.compile(r"^(https?://[^?#]+?)/(?:index\.php/)?s/([A-Za-z0-9]+)/?$")


def webdav_upload_url(url: str) -> str:
    """URL WebDAV da usare per un link di condivisione Nextcloud; gli altri restano."""
    m = _NC_SHARE_LINK.match(url.strip())
    return f"{m.group(1)}/public.php/dav/files/{m.group(2)}" if m else url


class ConfigManager:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self._config: ArfeaConfig | None = None

    def load(self) -> ArfeaConfig:
        raw = yaml.safe_load(self.config_path.read_text())
        self._config = ArfeaConfig.model_validate(raw)
        logger.info("Configuration loaded from %s", self.config_path)
        return self._config

    @property
    def config(self) -> ArfeaConfig:
        if self._config is None:
            return self.load()
        return self._config

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    def resolve_effective_enabled(self) -> dict[str, bool]:
        """Return a map of service_name -> effectively_enabled.

        A service is effectively enabled if:
        - its own ``enabled`` flag is True, OR
        - a dependency rule forces it on because at least one service
          in ``when_any_enabled`` has ``enabled: True``.
        """
        services = self.config.services
        result: dict[str, bool] = {
            name: svc.enabled for name, svc in services.items()
        }

        for rule in self.config.dependencies:
            if any(result.get(s, False) for s in rule.when_any_enabled):
                result[rule.then_enable] = True

        return result

    def get_startup_order(self) -> list[str]:
        """Topological sort of effectively-enabled services.

        Services with no dependencies come first, then those whose
        dependencies are already placed.  Core services are prioritised.
        """
        effective = self.resolve_effective_enabled()
        enabled_names = {n for n, on in effective.items() if on}
        services = self.config.services

        # Build adjacency list (only among enabled services)
        deps: dict[str, set[str]] = {}
        for name in enabled_names:
            svc = services[name]
            deps[name] = {
                d for d in svc.depends_on if d in enabled_names
            }

        ordered: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            visited.add(name)
            for dep in deps.get(name, set()):
                visit(dep)
            ordered.append(name)

        # Visit core services first
        for name in enabled_names:
            if services[name].core:
                visit(name)
        for name in enabled_names:
            visit(name)

        return ordered

    # ------------------------------------------------------------------
    # Persist enable/disable changes
    # ------------------------------------------------------------------

    def set_service_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.config.services:
            raise KeyError(f"Service '{name}' not found in configuration")
        svc = self.config.services[name]
        if svc.core and not enabled:
            raise ValueError(f"Cannot disable core service '{name}'")

        svc.enabled = enabled
        self._save()
        logger.info("Service '%s' set to enabled=%s", name, enabled)

    def set_service_images(self, image_map: dict[str, str]) -> list[str]:
        """Scrittura CHIRURGICA dei soli tag immagine in arfea.yml.

        Unica eccezione consentita alla regola "l'update non tocca arfea.yml":
        modifica solo ``services.<svc>.image`` per i servizi presenti.
        Persistenza atomica via ``_save()``. NON tocca il marker release: quello
        va aggiornato con ``set_release`` solo a step di upgrade riuscito.
        Ritorna l'elenco dei servizi effettivamente modificati.
        """
        changed: list[str] = []
        for svc_name, image in image_map.items():
            svc = self.config.services.get(svc_name)
            if svc is None:
                logger.warning("set_service_images: servizio '%s' assente, salto", svc_name)
                continue
            if svc.image != image:
                svc.image = image
                changed.append(svc_name)
        if changed:
            self._save()
            logger.info("Tag immagine aggiornati in arfea.yml: %s", changed)
        return changed

    def set_release(self, release: str) -> None:
        """Aggiorna solo il marker controller.release (persistenza atomica)."""
        self.config.controller.release = release
        self._save()

    def set_service_devices(self, name: str, devices: list[str]) -> list[str]:
        """Scrittura CHIRURGICA della lista ``devices`` di un servizio in arfea.yml.

        Permette di gestire le porte seriali (Modbus/Z-Wave/Zigbee/Thread) dalla
        Web UI invece di pre-configurarle nel template. Normalizza le voci
        (trim, scarta i vuoti) e persiste solo se qualcosa è cambiato. Ritorna la
        lista effettivamente salvata."""
        svc = self.config.services.get(name)
        if svc is None:
            raise KeyError(f"Service '{name}' not found in configuration")
        cleaned = [d.strip() for d in devices if d and d.strip()]
        if svc.devices != cleaned:
            svc.devices = cleaned
            self._save()
            logger.info("Device di '%s' aggiornati in arfea.yml: %s", name, cleaned)
        return cleaned

    def ensure_release_schema(self) -> bool:
        """Auto-migrazione dei canali OTA in un arfea.yml che ne è sprovvisto.

        arfea.yml è protetto dall'OTA, quindi le centraline aggiornate da versioni
        precedenti non hanno i campi nuovi; e migrate-to-controller.sh, fino alla
        1.8.1, svuotava update_url di proposito.
        - update_url vuoto → DEFAULT_UPDATE_URL (Redmine #192): senza, la
          centralina non riceve più l'OTA e nessuno se ne accorge;
        - releases_url vuoto → dalla stessa cartella di update_url (stesso host);
        - webdav_url link di condivisione Nextcloud → indirizzo WebDAV (#203).
        Ritorna True se ha modificato e salvato la config."""
        ctrl = self.config.controller
        changed = False
        if not ctrl.update_url.strip():
            ctrl.update_url = DEFAULT_UPDATE_URL
            logger.warning("Auto-migrazione arfea.yml: update_url era vuoto, impostato a %s", ctrl.update_url)
            changed = True
        if not ctrl.releases_url.strip():
            base = ctrl.update_url.rsplit("/", 1)[0]
            ctrl.releases_url = f"{base}/releases.json"
            logger.info("Auto-migrazione arfea.yml: releases_url impostato a %s", ctrl.releases_url)
            changed = True
        # WebDAV: il default degli installer era il link di condivisione, su cui il
        # caricamento risponde 401: nessun backup arrivava su WebDAV (Redmine #203).
        backup = self.config.backup
        fixed = webdav_upload_url(backup.webdav_url or "")
        if fixed != (backup.webdav_url or ""):
            logger.warning("Auto-migrazione arfea.yml: webdav_url era un link di condivisione, "
                           "ora %s", fixed)
            backup.webdav_url = fixed
            changed = True
        if changed:
            self._save()
        return changed

    def set_habapp_functions(self, functions: list[str]) -> None:
        """Persiste le funzioni HABApp attive. La validazione dei nomi sta in
        habapp_manager, che e' l'unico a sapere quali esistono."""
        self.config.habapp.functions = functions
        self._save()
        logger.info("Funzioni HABApp persistite in arfea.yml: %s", functions or "(nessuna)")

    def set_linphone_config(self, data: dict) -> LinphoneConfig:
        """Aggiorna (parzialmente) la configurazione linphone e persiste arfea.yml."""
        current = self.config.linphone.model_dump()
        current.update(data)
        self.config.linphone = LinphoneConfig.model_validate(current)
        self._save()
        logger.info("Configurazione linphone aggiornata (enabled=%s)", self.config.linphone.enabled)
        return self.config.linphone

    def set_access_point(self, data: dict) -> AccessPointConfig:
        """Aggiorna (parzialmente) la configurazione dell'access point e persiste
        arfea.yml. Valida tutto prima di salvare: un valore sbagliato non arriva
        mai su file."""
        current = self.config.access_point.model_dump()
        current.update(data)
        self.config.access_point = AccessPointConfig.model_validate(current)
        self._save()
        logger.info("Configurazione access point aggiornata (enabled=%s)",
                    self.config.access_point.enabled)
        return self.config.access_point

    def _save(self) -> None:
        # Atomic write: scrive in file temporaneo, poi rename.
        # Così se yaml.dump fallisce a metà, il file originale resta intatto.
        data = _config_to_dict(self.config)
        tmp_path = self.config_path.with_name(self.config_path.name + ".tmp")
        with open(tmp_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        tmp_path.replace(self.config_path)


def _config_to_dict(cfg: ArfeaConfig) -> dict:
    """Serialise the config back to a plain dict suitable for YAML dump."""
    data = cfg.model_dump(mode="json", exclude_none=True)

    # Clean up services: drop empty lists/dicts for readability
    for svc in data.get("services", {}).values():
        for key in list(svc.keys()):
            val = svc[key]
            if isinstance(val, (list, dict)) and not val:
                del svc[key]
    return data
