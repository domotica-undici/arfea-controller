"""Pacchetto degli addon OpenHAB a bordo, per installarli senza internet.

Di suo OpenHAB scarica il binding da internet nel momento in cui lo si installa:
su una centralina senza linea — o con la linea giu' proprio quel giorno, o
riavviata in un capannone dove non c'e' campo — l'installazione fallisce in
silenzio e l'impianto resta senza quel pezzo. Le installazioni native risolvono
la cosa col pacchetto 'openhab-addons'; qui, che OpenHAB gira in container,
l'equivalente e' il kar della distribuzione ufficiale (openhab-addons-X.Y.Z.kar)
lasciato in openhab/addons: Karaf lo estrae e da li' in poi TUTTI gli addon si
installano offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from .config import ConfigManager
from .models import AddonsKarState, AddonsKarStatus

logger = logging.getLogger(__name__)

# UID/GID del container OpenHAB: tutto cio' che finisce sotto openhab/ e' suo.
_OH_UID = 9001
_OH_GID = 9001

# Versione OpenHAB dedotta dal tag immagine: 5.2.0, 5.2.0-debian, 5.3.0.M1.
# Tag senza numero (latest, snapshot, milestone) non dicono QUALE kar serve, e un
# kar di versione diversa dal runtime non risolve le feature: meglio non scaricare
# nulla che riempire mezzo giga di disco con un pacchetto inutilizzabile.
_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+(?:\.M\d+)?)")

_KAR_GLOB = "openhab-addons-*.kar"

# Karaf estrae il kar in userdata/tmp/kar, quindi a regime occupa il doppio del
# file scaricato; il margine sopra il doppio e' per il resto dell'impianto.
_DISK_MARGIN = 500 * 1024 * 1024

_MB = 1024 * 1024

# Ogni quanto si ricontrolla che il kar a bordo sia quello della versione in uso.
_WATCH_SECONDS = 3600


class AddonsKarManager:
    """Tiene in openhab/addons il kar con tutti gli addon della versione in uso.

    Il download e' in background e non blocca nulla: il kar pesa ~600 MB e
    OpenHAB non deve aspettarlo per partire, tanto Karaf lo carica a caldo appena
    compare nella cartella addons. Chi lo chiede (avvio del controller, creazione
    del container openhab, pulsante nella Web UI) ottiene subito una risposta e
    segue l'avanzamento da ``status()``.
    """

    def __init__(self, cfg: ConfigManager):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._downloaded = 0
        self._total = 0
        self._error = ""
        self._watch: Optional[threading.Thread] = None

    def start_watch(self, interval: int = _WATCH_SECONDS) -> None:
        """Ricontrolla periodicamente che a bordo ci sia il kar della versione in uso.

        All'avvio del controller e alla (ri)creazione del container openhab ci
        pensa gia' ensure(): e' cosi' che il kar segue un aggiornamento di
        OpenHAB. Qui si copre il download fallito — linea giu' proprio mentre si
        aggiornava, disco pieno poi liberato — che prima si ritentava solo al
        riavvio successivo del controller, e intanto la centralina restava col kar
        della versione vecchia, inutilizzabile col runtime nuovo. Con il kar
        giusto a bordo il giro costa uno stat."""
        if self._watch is not None:
            return

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    version = self.wanted_version()
                    if version and not self.kar_path(version).is_file():
                        ok, msg = self.ensure()
                        logger.info("Addon OpenHAB, controllo periodico: %s", msg)
                except Exception as exc:
                    logger.warning("Addon OpenHAB, controllo periodico fallito: %s", exc)

        self._watch = threading.Thread(target=loop, name="addons-kar-watch", daemon=True)
        self._watch.start()

    # ------------------------------------------------------------------
    # Cosa serve e dove sta
    # ------------------------------------------------------------------

    def addons_dir(self) -> Path:
        return Path(self.cfg.config.controller.data_path) / "openhab" / "addons"

    def wanted_version(self) -> str:
        """Versione del kar da avere a bordo, dedotta dal tag di openhab.

        Vuota se il tag non ha un numero di versione riconoscibile."""
        svc = self.cfg.config.services.get("openhab")
        if svc is None or ":" not in svc.image:
            return ""
        tag = svc.image.rsplit(":", 1)[-1]
        match = _VERSION_RE.match(tag)
        return match.group(1) if match else ""

    def kar_path(self, version: str) -> Path:
        return self.addons_dir() / f"openhab-addons-{version}.kar"

    def _url(self, version: str) -> str:
        template = self.cfg.config.controller.addons_kar_url
        return template.replace("{version}", version) if template else ""

    def _disabled_reason(self, version: str) -> str:
        if not self.cfg.config.controller.addons_kar_url:
            return "Pacchetto addon offline disattivato (controller.addons_kar_url vuoto in arfea.yml)"
        if not version:
            svc = self.cfg.config.services.get("openhab")
            image = svc.image if svc else "n/d"
            return (f"Versione OpenHAB non deducibile dal tag immagine ({image}): "
                    f"il pacchetto addon va installato a mano in openhab/addons")
        return ""

    # ------------------------------------------------------------------
    # Stato
    # ------------------------------------------------------------------

    def status(self) -> AddonsKarStatus:
        version = self.wanted_version()
        st = AddonsKarStatus(version=version)

        reason = self._disabled_reason(version)
        if reason:
            st.state = AddonsKarState.DISABLED
            st.message = reason
            return st

        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            downloaded, total, error = self._downloaded, self._total, self._error

        if running:
            st.state = AddonsKarState.DOWNLOADING
            st.size_mb = round(downloaded / _MB, 1)
            st.total_mb = round(total / _MB, 1)
            st.progress = int(downloaded * 100 / total) if total else 0
            st.message = (f"Scaricamento addon {version} in corso "
                          f"({st.size_mb:.0f} di {st.total_mb:.0f} MB)")
            return st

        kar = self.kar_path(version)
        if kar.is_file():
            st.state = AddonsKarState.PRESENT
            st.file = kar.name
            st.size_mb = round(kar.stat().st_size / _MB, 1)
            st.message = (f"Addon OpenHAB {version} a bordo ({st.size_mb:.0f} MB): "
                          f"si installano anche senza internet")
            return st

        if error:
            st.state = AddonsKarState.FAILED
            st.message = error
            return st

        st.state = AddonsKarState.MISSING
        st.message = (f"Pacchetto addon {version} non presente: senza internet i "
                      f"binding non si possono installare")
        return st

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def ensure(self, force: bool = False) -> tuple[bool, str]:
        """Avvia il download del kar se manca. Ritorna subito (non aspetta).

        Idempotente: se il kar giusto c'e' gia' — o se un download e' in corso —
        non fa nulla. ``force`` riscarica anche un kar gia' presente (serve al
        pulsante della Web UI dopo un file corrotto o cancellato a mano).
        """
        version = self.wanted_version()
        reason = self._disabled_reason(version)
        if reason:
            return (False, reason)

        if self.kar_path(version).is_file() and not force:
            return (True, f"Addon OpenHAB {version} gia' a bordo")

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return (True, "Download del pacchetto addon gia' in corso")
            self._downloaded = 0
            self._total = 0
            self._error = ""
            self._thread = threading.Thread(
                target=self._download, args=(version,), daemon=True
            )
            self._thread.start()

        logger.info("Addon OpenHAB %s: download avviato in background", version)
        return (True, f"Scaricamento del pacchetto addon {version} avviato "
                      f"(~600 MB, prosegue in background)")

    def _download(self, version: str) -> None:
        url = self._url(version)
        dest = self.kar_path(version)
        # Il file di lavoro e' nascosto e non finisce in .kar: Karaf guarda la
        # cartella addons e caricherebbe un pacchetto ancora a meta'. Solo la
        # rename finale, atomica, glielo mette davanti completo.
        part = dest.with_name(f".{dest.name}.part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _chown_openhab(dest.parent)

            with httpx.stream("GET", url, timeout=httpx.Timeout(30.0, read=120.0),
                              follow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                expected_sha = resp.headers.get("x-checksum-sha256", "")
                with self._lock:
                    self._total = total

                if not self._enough_disk(total, version):
                    return

                digest = hashlib.sha256()
                written = 0
                with open(part, "wb") as fh:
                    for chunk in resp.iter_bytes(1024 * 1024):
                        fh.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        with self._lock:
                            self._downloaded = written

            # Una linea che cade a meta' non da' errore: da' un file corto. Senza
            # questi controlli finirebbe in addons e Karaf ci si romperebbe sopra.
            if total and written != total:
                raise RuntimeError(
                    f"download incompleto: {written} byte su {total} attesi"
                )
            if expected_sha and digest.hexdigest().lower() != expected_sha.lower():
                raise RuntimeError("sha256 del pacchetto non combacia con quello dichiarato")

            os.replace(part, dest)
            _chown_openhab(dest)
            logger.info("Addon OpenHAB %s installati in %s (%.0f MB)",
                        version, dest, written / _MB)
            self._remove_other_kars(dest)

        except Exception as exc:
            # Il .part resta li' a occupare spazio: via, il prossimo tentativo
            # riparte da zero (non c'e' resume: non tutti i mirror lo reggono).
            part.unlink(missing_ok=True)
            msg = f"Download del pacchetto addon {version} fallito: {exc}"
            logger.error("%s", msg)
            with self._lock:
                self._error = msg
        finally:
            with self._lock:
                self._downloaded = 0
                self._total = 0

    def _enough_disk(self, needed: int, version: str) -> bool:
        """Ferma il download se il disco non regge kar + estrazione di Karaf.

        Riempire l'eMMC di una centralina e' peggio del non avere gli addon
        offline: si ferma tutto, OpenHAB compreso."""
        if not needed:
            return True
        required = needed * 2 + _DISK_MARGIN
        free = shutil.disk_usage(self.addons_dir()).free
        if free >= required:
            return True
        msg = (f"Spazio insufficiente per gli addon {version}: servono "
               f"{required / _MB:.0f} MB (il kar viene anche estratto da Karaf), "
               f"liberi {free / _MB:.0f} MB")
        logger.error("%s", msg)
        with self._lock:
            self._error = msg
        return False

    def _remove_other_kars(self, keep: Path) -> None:
        """Toglie i kar delle versioni precedenti (dopo un aggiornamento immagine).

        Prima si scarica il nuovo, poi si toglie il vecchio: al contrario si
        resterebbe senza addon per tutta la durata del download."""
        for old in self.addons_dir().glob(_KAR_GLOB):
            if old == keep:
                continue
            try:
                old.unlink()
                logger.info("Rimosso il pacchetto addon della versione precedente: %s", old.name)
            except OSError as exc:
                logger.warning("Non riesco a rimuovere %s: %s", old, exc)


def _chown_openhab(path: Path) -> None:
    """I file sotto openhab/ devono essere dell'utente del container (9001:9001)."""
    try:
        os.chown(path, _OH_UID, _OH_GID)
    except OSError as exc:
        logger.warning("chown %s a %s:%s fallito: %s", path, _OH_UID, _OH_GID, exc)


# Istanza unica: lo stato del download e' condiviso fra chi lo avvia (avvio del
# controller, creazione del container openhab, Web UI) e chi lo mostra. Con
# un'istanza per chiamante due download potrebbero partire insieme e la UI
# guarderebbe comunque lo stato sbagliato.
_instance: Optional[AddonsKarManager] = None
_instance_lock = threading.Lock()


def manager(cfg: ConfigManager) -> AddonsKarManager:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = AddonsKarManager(cfg)
        return _instance
