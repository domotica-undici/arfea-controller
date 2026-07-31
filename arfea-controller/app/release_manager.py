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
    from .habapp_manager import HABAppManager

logger = logging.getLogger(__name__)

# Il codice HABApp (regole+librerie) viaggia in releases.json come blocco
# habapp_code e si comporta, nel diff e nella selezione software-per-software,
# come uno pseudo-servizio con questo nome. NON e' un servizio in arfea.yml: e'
# un artefatto di dati versionato a parte (source_dir() su disco).
HABAPP_CODE = "habapp-code"


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
        habapp_manager: "HABAppManager",
    ):
        self.cfg = config_manager
        self.docker = docker_manager
        self.backup = backup_manager
        self.habapp = habapp_manager
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
        """Identifica la release corrente: la piu' recente i cui componenti
        (immagini + codice HABApp) combaciano con lo stato dell'impianto.
        Ritorna "" se nessuna combacia esattamente."""
        for rel in reversed(releases):
            if self._release_matches(rel):
                return rel.get("version", "")
        return ""

    def _release_matches(self, rel: dict) -> bool:
        """True se questa release e' interamente installata: tutti i suoi tag
        immagine combaciano con arfea.yml E (se dichiara habapp_code) la versione
        del codice HABApp installata su disco combacia. Il vincolo sul codice e'
        cio' che rende visibile un bump di SOLO codice (immagini identiche): senza,
        _infer_current scambierebbe la release nuova per gia' installata."""
        services = self.cfg.config.services
        images = rel.get("images", {})
        if not images:
            return False
        if not all(
            svc in services and services[svc].image == img
            for svc, img in images.items()
        ):
            return False
        code_ver = self._release_code_version(rel)
        if code_ver and code_ver != self._installed_code_version():
            return False
        return True

    # ------------------------------------------------------------------
    # Codice HABApp (blocco habapp_code nel manifest)
    # ------------------------------------------------------------------

    def _installed_code_version(self) -> str:
        """Versione DEPLOYATA, non quella a bordo: l'OTA del controller porta i
        sorgenti come pavimento, e prima bastava quello a far risultare la
        release installata mentre l'impianto girava ancora con le regole
        vecchie. Se il marker non c'e' (provisioning fatto da un controller
        precedente) si ricade sui sorgenti, cioe' sul comportamento di prima:
        meglio che dichiarare l'impianto senza codice."""
        return self.habapp.deployed_version() or self.habapp.source_version()

    @staticmethod
    def _release_habapp_code(rel: dict) -> Optional[dict]:
        """Blocco habapp_code {version,url,sha256} di una release, o None."""
        hc = rel.get("habapp_code")
        if isinstance(hc, dict) and hc.get("version"):
            return hc
        return None

    def _release_code_version(self, rel: dict) -> str:
        hc = self._release_habapp_code(rel)
        return hc["version"] if hc else ""

    def _target_habapp_code(self, manifest: dict, latest: str) -> Optional[dict]:
        rel = self._release_by_version(manifest, latest) or {}
        return self._release_habapp_code(rel)

    def _habapp_enabled(self) -> bool:
        svc = self.cfg.config.services.get("habapp")
        return bool(svc and svc.enabled)

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

        # Codice HABApp come componente aggiuntivo (pseudo-servizio "habapp-code").
        # Riusa ServiceUpdateInfo: i campi *_image portano qui le versioni codice.
        target_code = self._target_habapp_code(manifest, latest)
        installed_code = self._installed_code_version()
        if target_code and target_code["version"] != installed_code:
            services_diff.append(
                ServiceUpdateInfo(
                    name=HABAPP_CODE,
                    current_image=installed_code or "(nessuno)",
                    target_image=target_code["version"],
                )
            )

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

        # Codice HABApp: pseudo-componente selezionabile accanto ai servizi.
        target_code = self._target_habapp_code(manifest, latest)
        installed_code = self._installed_code_version()
        code_pending = bool(target_code and target_code["version"] != installed_code)

        pending = dict(all_pending)
        apply_code = code_pending
        if selected is not None:
            pending = {svc: img for svc, img in all_pending.items() if svc in selected}
            apply_code = code_pending and HABAPP_CODE in selected

        self.status = ReleaseUpdateStatus(
            state=ReleaseUpdateState.IDLE,
            current_release=current,
            target_release=latest,
            started_at=datetime.now(),
        )

        if not pending and not apply_code:
            self.status.state = ReleaseUpdateState.COMPLETED
            self.status.message = "Nessun aggiornamento da applicare"
            self.status.completed_at = datetime.now()
            return self.status

        # Upgrade completo = si stanno aggiornando TUTTI i componenti con diff
        # pendente (immagini + codice). Solo allora girano le migrazioni (pensate
        # per l'intero set release).
        all_keys = set(all_pending) | ({HABAPP_CODE} if code_pending else set())
        sel_keys = set(pending) | ({HABAPP_CODE} if apply_code else set())
        full_upgrade = sel_keys == all_keys

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
        prev_code = installed_code       # versione codice pre-apply (per rollback)
        code_installed = False
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

            # 2b) codice HABApp: scarica+verifica+unpack+provision (NON ricrea ancora
            # il container: lo fa il passo 4, cosi' un solo recreate applica anche
            # l'eventuale nuovo tag immagine habapp).
            if apply_code:
                self.status.step = HABAPP_CODE
                self.status.message = f"Installazione codice HABApp {target_code['version']}..."
                ok, msg = self.habapp.install_code(
                    target_code["version"],
                    target_code.get("url", ""),
                    target_code.get("sha256", ""),
                )
                if not ok:
                    raise RuntimeError(msg)
                code_installed = True

            # 3) scrittura chirurgica dei soli tag in arfea.yml
            if pending:
                self.cfg.set_service_images(pending)

            # 4) recreate + health-gate. Se ho installato codice ma habapp non e' tra
            # i servizi con tag nuovo (bump di solo codice), va comunque ricreato per
            # caricare le regole nuove.
            recreate_targets = list(pending.keys())
            if code_installed and self._habapp_enabled() and "habapp" not in recreate_targets:
                recreate_targets.append("habapp")
            self.status.state = ReleaseUpdateState.RECREATING
            self.status.message = "Riavvio servizi aggiornati..."
            self._recreate_in_order(recreate_targets)

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
            if code_installed:
                self._rollback_code(target_code["version"], prev_code)
            self._fail(
                f"Aggiornamento fallito: {exc}. Tag immagine e codice HABApp "
                f"ripristinati. Backup disponibile per ripristino manuale."
            )
            return self.status

        # Marker avanza solo se TUTTE le immagini della release ora combaciano E la
        # versione del codice HABApp bersaglio (se dichiarata) e' quella installata.
        images_ok = all(
            self.cfg.config.services[svc].image == img
            for svc, img in target_images.items()
        )
        code_ok = target_code is None or self._installed_code_version() == target_code["version"]
        if images_ok and code_ok:
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
        applied = list(pending.keys()) + ([HABAPP_CODE] if code_installed else [])
        logger.info("Apply completato (componenti: %s)", applied)
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

    def _rollback_code(self, new_version: str, prev_version: str) -> None:
        """Annulla l'installazione del codice HABApp appena spacchettato: rimuove
        la dir <new_version> (source_dir() torna a prev_version, o a nulla se non
        c'era codice prima), ri-provisiona col codice precedente e ricrea habapp."""
        try:
            self.habapp.remove_code_version(new_version)
            self.habapp.provision()
            if self._habapp_enabled():
                self.docker.recreate_service("habapp")
            logger.info("Codice HABApp ripristinato a '%s'", prev_version or "(nessuno)")
        except Exception as exc:
            logger.error("Rollback codice HABApp parzialmente fallito: %s", exc)

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
