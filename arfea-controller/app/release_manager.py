from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import httpx

from .models import (
    ReleaseCheckResult,
    ReleaseUpdateState,
    ReleaseUpdateStatus,
    ServiceUpdateInfo,
)

if TYPE_CHECKING:
    from .backup import BackupManager
    from .config import ConfigManager
    from .docker_manager import DockerManager

logger = logging.getLogger(__name__)


class ReleaseManager:
    """Gestisce l'aggiornamento delle versioni immagine ("release certificate").

    Separato dall'OTA del codice del controller. Scarica un manifest
    ``releases.json``, calcola quali servizi hanno un'immagine più recente
    (target = release ``latest``) e, su richiesta esplicita (conferma utente,
    eventualmente software-per-software), applica l'aggiornamento con:
    backup -> migrazione pre -> pull -> scrittura tag -> recreate -> health-gate
    -> migrazione post. In caso di fallimento esegue rollback dei tag immagine.

    La selezione per-servizio: l'utente può scegliere QUALI software aggiornare.
    Le migrazioni (fix di versione) girano solo per un upgrade COMPLETO (nessun
    servizio deselezionato), perché sono pensate per l'intero set della release.
    Il marker ``controller.release`` avanza a ``latest`` solo quando TUTTE le
    immagini della release combaciano con arfea.yml (upgrade completato).
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        docker_manager: DockerManager,
        backup_manager: BackupManager,
    ):
        self.cfg = config_manager
        self.docker = docker_manager
        self.backup = backup_manager
        self.status = ReleaseUpdateStatus()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _fetch_manifest(self) -> dict:
        url = self.cfg.config.controller.releases_url
        if not url:
            raise RuntimeError("releases_url non configurato in arfea.yml")
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        if "releases" not in data or not isinstance(data["releases"], list):
            raise RuntimeError("Manifest non valido: manca la lista 'releases'")
        return data

    def _infer_current(self, releases: list[dict]) -> str:
        """Identifica la release corrente confrontando i tag immagine di arfea.yml.
        Ritorna "" se nessuna release combacia esattamente."""
        services = self.cfg.config.services
        for rel in reversed(releases):
            images = rel.get("images", {})
            if not images:
                continue
            if all(
                svc in services and services[svc].image == img
                for svc, img in images.items()
            ):
                return rel.get("version", "")
        return ""

    def _build_path(self, manifest: dict, current: str) -> tuple[list[str], str]:
        """Ritorna (lista versioni da attraversare in ordine per le migrazioni, latest)."""
        releases = manifest["releases"]
        versions = [r.get("version", "") for r in releases]
        latest = manifest.get("latest") or (versions[-1] if versions else "")
        if latest not in versions:
            return [], latest
        target_idx = versions.index(latest)
        if current in versions:
            cur_idx = versions.index(current)
            if cur_idx >= target_idx:
                return [], latest
            return versions[cur_idx + 1: target_idx + 1], latest
        # Stato non riconosciuto: nessuna migrazione intermedia certa.
        return [], latest

    def _release_by_version(self, manifest: dict, version: str) -> Optional[dict]:
        for rel in manifest["releases"]:
            if rel.get("version") == version:
                return rel
        return None

    def _target_images(self, manifest: dict, latest: str) -> dict[str, str]:
        """Immagini della release latest, filtrate ai servizi presenti in arfea.yml."""
        rel = self._release_by_version(manifest, latest) or {}
        services = self.cfg.config.services
        return {
            svc: img for svc, img in rel.get("images", {}).items()
            if svc in services
        }

    def _pending(self, target_images: dict[str, str]) -> dict[str, str]:
        """Sottoinsieme di target_images con tag diverso da quello installato."""
        services = self.cfg.config.services
        return {
            svc: img for svc, img in target_images.items()
            if services[svc].image != img
        }

    # ------------------------------------------------------------------
    # Check (non distruttivo)
    # ------------------------------------------------------------------

    def check(self) -> ReleaseCheckResult:
        try:
            manifest = self._fetch_manifest()
        except Exception as exc:
            logger.warning("Check release fallito: %s", exc)
            return ReleaseCheckResult(error=str(exc))

        current = self.cfg.config.controller.release or self._infer_current(manifest["releases"])
        path, latest = self._build_path(manifest, current)
        target_images = self._target_images(manifest, latest)
        pending = self._pending(target_images)

        services_diff = [
            ServiceUpdateInfo(
                name=svc,
                current_image=self.cfg.config.services[svc].image,
                target_image=img,
            )
            for svc, img in pending.items()
        ]

        notes_parts = []
        for v in path or [latest]:
            rel = self._release_by_version(manifest, v)
            if rel and rel.get("notes"):
                notes_parts.append(f"{v}: {rel['notes']}")

        return ReleaseCheckResult(
            update_available=bool(services_diff),
            current_release=current,
            latest_release=latest,
            path=path,
            services=services_diff,
            notes="\n".join(notes_parts),
        )

    # ------------------------------------------------------------------
    # Apply (background task)
    # ------------------------------------------------------------------

    def _migrations_dir(self) -> Path:
        return Path(self.cfg.config.controller.data_path) / "arfea-controller" / "migrations"

    def _run_migration(self, version: str, phase: str, from_version: str) -> None:
        """Esegue migrations/<version>/<phase>.sh se presente. phase = pre|post.
        Lo script è responsabile di impostare owner 9001:9001 sui file OpenHAB.
        Eccezione se lo script esce con codice != 0 (→ rollback)."""
        script = self._migrations_dir() / version / f"{phase}.sh"
        if not script.is_file():
            return
        logger.info("Migrazione %s/%s.sh in esecuzione...", version, phase)
        env = {
            **os.environ,
            "DATA_PATH": self.cfg.config.controller.data_path,
            "OH_CONF": str(Path(self.cfg.config.controller.data_path) / "openhab" / "conf"),
            "FROM_VERSION": from_version,
            "TO_VERSION": version,
        }
        result = subprocess.run(
            ["bash", str(script)],
            env=env, capture_output=True, text=True, timeout=600,
        )
        if result.stdout:
            logger.info("Migrazione %s/%s stdout: %s", version, phase, result.stdout.strip())
        if result.returncode != 0:
            raise RuntimeError(
                f"Migrazione {version}/{phase}.sh fallita (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _recreate_in_order(self, service_names: list[str]) -> None:
        """Ricrea i servizi indicati rispettando l'ordine delle dipendenze.
        I servizi non in startup order (disabilitati) sono ignorati: l'immagine
        aggiornata si applicherà al prossimo avvio."""
        order = [s for s in self.cfg.get_startup_order() if s in service_names]
        for name in order:
            res = self.docker.recreate_service(name)
            if not res.success:
                raise RuntimeError(f"Recreate '{name}' fallito: {res.message}")

    def run_apply(self, selected: Optional[list[str]] = None) -> ReleaseUpdateStatus:
        """Applica l'aggiornamento verso ``latest``. Se ``selected`` è dato, aggiorna
        solo quei servizi (conferma software-per-software). Bloccante: usare come
        background task."""
        if self.status.state not in (
            ReleaseUpdateState.IDLE,
            ReleaseUpdateState.COMPLETED,
            ReleaseUpdateState.FAILED,
            ReleaseUpdateState.ROLLED_BACK,
        ):
            return self.status

        try:
            manifest = self._fetch_manifest()
        except Exception as exc:
            self.status = ReleaseUpdateStatus(
                state=ReleaseUpdateState.FAILED,
                message=f"Manifest non raggiungibile: {exc}",
                completed_at=datetime.now(),
            )
            return self.status

        current = self.cfg.config.controller.release or self._infer_current(manifest["releases"])
        path, latest = self._build_path(manifest, current)
        target_images = self._target_images(manifest, latest)
        all_pending = self._pending(target_images)

        pending = dict(all_pending)
        if selected is not None:
            pending = {svc: img for svc, img in all_pending.items() if svc in selected}

        self.status = ReleaseUpdateStatus(
            state=ReleaseUpdateState.IDLE,
            current_release=current,
            target_release=latest,
            started_at=datetime.now(),
        )

        if not pending:
            self.status.state = ReleaseUpdateState.COMPLETED
            self.status.message = "Nessun aggiornamento da applicare"
            self.status.completed_at = datetime.now()
            return self.status

        # Upgrade completo = si stanno aggiornando TUTTI i servizi con diff pendente.
        # Solo in quel caso girano le migrazioni (pensate per l'intero set release).
        full_upgrade = set(pending.keys()) == set(all_pending.keys())

        # Guard controller_min sulla release finale
        from .main import VERSION  # import locale: evita ciclo all'import
        target_rel = self._release_by_version(manifest, latest) or {}
        min_ctrl = target_rel.get("controller_min")
        if min_ctrl and _version_lt(VERSION, min_ctrl):
            self._fail(
                f"La release {latest} richiede controller >= {min_ctrl} "
                f"(attuale {VERSION}). Aggiorna prima il controller."
            )
            return self.status

        # Backup come punto di ripristino
        self.status.state = ReleaseUpdateState.BACKUP
        self.status.message = "Backup pre-aggiornamento in corso..."
        backup_status = self.backup.run_backup()
        if backup_status.state.value == "failed":
            self._fail(f"Backup pre-aggiornamento fallito: {backup_status.message}")
            return self.status

        prev_images = {svc: self.cfg.config.services[svc].image for svc in pending}
        migrate = full_upgrade and bool(path)

        try:
            # 1) migrazioni pre (una per release attraversata), solo upgrade completo
            if migrate:
                self.status.state = ReleaseUpdateState.MIGRATING_PRE
                self.status.message = "Preparazione (migrazioni pre)..."
                prev = current
                for v in path:
                    self._run_migration(v, "pre", prev)
                    prev = v

            # 2) pull (fail-fast prima di toccare i container)
            self.status.state = ReleaseUpdateState.PULLING
            self.status.message = "Scaricamento nuove immagini..."
            for svc, img in pending.items():
                self.status.step = svc
                res = self.docker.pull_image(img)
                if not res.success:
                    raise RuntimeError(res.message)

            # 3) scrittura chirurgica dei soli tag in arfea.yml
            self.cfg.set_service_images(pending)

            # 4) recreate + health-gate
            self.status.state = ReleaseUpdateState.RECREATING
            self.status.message = "Riavvio servizi aggiornati..."
            self._recreate_in_order(list(pending.keys()))

            # 5) migrazioni post, solo upgrade completo
            if migrate:
                self.status.state = ReleaseUpdateState.MIGRATING_POST
                self.status.message = "Finalizzazione (migrazioni post)..."
                prev = current
                for v in path:
                    self._run_migration(v, "post", prev)
                    prev = v

        except Exception as exc:
            logger.error("Aggiornamento fallito: %s", exc)
            self._rollback(prev_images)
            self._fail(
                f"Aggiornamento fallito: {exc}. Tag immagine ripristinati. "
                f"Backup disponibile per ripristino manuale."
            )
            return self.status

        # Marker avanza solo se TUTTE le immagini della release ora combaciano
        if all(
            self.cfg.config.services[svc].image == img
            for svc, img in target_images.items()
        ):
            self.cfg.set_release(latest)
            self.status.current_release = latest
            done_msg = f"Aggiornamento completato: sistema alla versione {latest}"
        else:
            done_msg = (
                "Aggiornamento parziale completato. Alcuni software non sono stati "
                "aggiornati: le migrazioni di versione verranno applicate al "
                "completamento dell'upgrade."
            )

        self.status.state = ReleaseUpdateState.COMPLETED
        self.status.message = done_msg
        self.status.step = ""
        self.status.completed_at = datetime.now()
        logger.info("Apply completato (servizi: %s)", list(pending.keys()))
        return self.status

    # ------------------------------------------------------------------
    # Rollback / fail helpers
    # ------------------------------------------------------------------

    def _rollback(self, prev_images: dict[str, str]) -> None:
        """Riporta i tag immagine ai valori pre-apply e ricrea i servizi."""
        try:
            changed = self.cfg.set_service_images(prev_images)
            for name in [s for s in self.cfg.get_startup_order() if s in changed]:
                self.docker.recreate_service(name)
        except Exception as exc:
            logger.error("Rollback parzialmente fallito: %s", exc)

    def _fail(self, message: str) -> None:
        self.status.state = ReleaseUpdateState.FAILED
        self.status.message = message
        self.status.completed_at = datetime.now()


def _version_lt(a: str, b: str) -> bool:
    """True se la versione a < b (confronto numerico per componenti, es. 1.2.1)."""
    def parts(v: str) -> list[int]:
        out = []
        for p in v.split("."):
            num = "".join(ch for ch in p if ch.isdigit())
            out.append(int(num) if num else 0)
        return out
    return parts(a) < parts(b)
