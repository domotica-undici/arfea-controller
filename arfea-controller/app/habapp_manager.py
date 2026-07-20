"""Provisioning di HABApp: funzioni attive, sorgenti, token, params.

Modello di proprieta' dei file sotto openhab/conf/habapp (= /habapp/config nel
container):

    rules/<funzione>/   CONTROLLER  risincronizzati dai sorgenti ad ogni avvio
    lib/<funzione>/     CONTROLLER  (cosi' un OTA aggiorna davvero le regole)
    lib/system/         CONTROLLER  base comune
    config.yml          MISTO       scritto solo se manca il token (vedi sotto)
    logging.yml         UTENTE      deployato se manca, mai sovrascritto
    params/*.yml        UTENTE      configurazione dell'impianto, mai sovrascritta

Il controller tocca SOLO le cartelle che spedisce lui: le regole specifiche
cliente gia' presenti su un impianto (accessControl/, infraRed/, aasystem/...)
non vengono ne' aggiornate ne' rimosse.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .config import ConfigManager
from .models import HABAppFunctionInfo, HABAppStatus

if TYPE_CHECKING:  # evita l'import circolare docker_manager <-> habapp_manager
    from .docker_manager import DockerManager

logger = logging.getLogger(__name__)

# UID/GID con cui gira il container HABApp: l'immagine spacemanspiff2007/habapp
# ha USER_ID=9001/GROUP_ID=9001 di default, gli stessi di OpenHAB — ed e' il
# motivo per cui la sua config puo' stare sotto openhab/conf/ senza litigi.
OH_UID = 9001
OH_GID = 9001

# Un API token OpenHAB ha forma oh.<nome>.<segreto>
_TOKEN_RE = re.compile(r"^oh\.[A-Za-z0-9._-]+$")

_URL_PLACEHOLDER = "__ARFEA_OH_URL__"
_TOKEN_PLACEHOLDER = "__ARFEA_OH_TOKEN__"

# Le funzioni attivabili. La chiave e' quella che finisce in arfea.yml.
# NB: per la termoregolazione la chiave ("thermo", che deve combaciare con
# HABApp.DictParameter('thermo') -> params/thermo.yml) NON coincide col nome
# della cartella delle regole ("thermostats").
# Deve restare allineato a script/habapp-subset.sh, che decide cosa spedire.
_FUNCTIONS: dict[str, dict] = {
    "thermo": {
        "label": "Termoregolazione",
        "description": "Termostati, valvole, fancoil, radiatori, finestre",
        "rules": "thermostats",
        "libs": ["thermostats"],
        "params": "thermo",
    },
    "irrigation": {
        "label": "Irrigazione",
        "description": "Zone, pompe, elettrovalvole, sensori pioggia",
        "rules": "irrigation",
        "libs": ["irrigation"],
        "params": "irrigation",
    },
    "loads": {
        "label": "Controllo carichi",
        "description": "Distacco dei carichi al superamento della potenza disponibile",
        "rules": "loads",
        "libs": ["loads"],
        "params": "loads",
    },
}

# Librerie comuni a tutte le funzioni (system.utils.Utils)
_BASE_LIBS = ["system"]
_ROOT_FILES = ["config.yml", "logging.yml"]


def function_names() -> list[str]:
    return list(_FUNCTIONS)


# Esito dell'ultimo provisioning. Vive a livello di modulo perche' chi lo esegue
# (docker_manager, alla creazione del container) e chi deve riferirlo all'utente
# (le API in main.py) usano due istanze diverse del manager.
_last_provision: tuple[bool, str] = (True, "")


def last_provision() -> tuple[bool, str]:
    """(ok, messaggio) dell'ultimo provisioning eseguito in questo processo."""
    return _last_provision


class HABAppManager:
    """Stateless: si costruisce dove serve (main.py per le API, docker_manager
    alla creazione del container)."""

    def __init__(self, config_manager: ConfigManager, docker_manager: "DockerManager | None" = None):
        self.cfg = config_manager
        self.docker = docker_manager

    # ------------------------------------------------------------------
    # Percorsi
    # ------------------------------------------------------------------

    @property
    def _data_path(self) -> Path:
        return Path(self.cfg.config.controller.data_path)

    def source_dir(self) -> Path | None:
        """Sorgenti HABApp portati dall'OTA: arfea-controller/habapp/<versione>."""
        base = self._data_path / "arfea-controller" / "habapp"
        if not base.is_dir():
            return None
        versions = [d for d in base.iterdir() if d.is_dir()]
        if not versions:
            return None

        def sort_key(p: Path) -> tuple:
            try:
                return (1, tuple(int(x) for x in p.name.split(".")))
            except ValueError:
                return (0, ())

        return max(versions, key=sort_key)

    def source_version(self) -> str:
        src = self.source_dir()
        return src.name if src else ""

    def config_dir(self) -> Path:
        """openhab/conf/habapp — montata su /habapp/config nel container."""
        return self._data_path / "openhab" / "conf" / "habapp"

    def params_dir(self) -> Path:
        return self.config_dir() / "params"

    def params_file(self, function: str) -> Path:
        return self.params_dir() / f"{_FUNCTIONS[function]['params']}.yml"

    # ------------------------------------------------------------------
    # Funzioni attive
    # ------------------------------------------------------------------

    def selected_functions(self) -> list[str]:
        """Funzioni attive. Se non sono mai state configurate le deduce
        dall'impianto (senza persistere: lo fa provision)."""
        configured = self.cfg.config.habapp.functions
        if configured is None:
            return self._infer_functions()
        out = []
        for f in configured:
            if f in _FUNCTIONS:
                out.append(f)
            else:
                logger.warning("Funzione HABApp sconosciuta in arfea.yml, ignorata: '%s'", f)
        return out

    def _infer_functions(self) -> list[str]:
        """Deduce le funzioni da cio' che e' gia' installato.

        Serve agli impianti che esistevano prima di questa feature: centraline
        aggiornate via OTA (arfea.yml e' protetto, quindi non ha la sezione
        habapp) e migrazioni da HABApp nativo, dove le regole del cliente sono
        gia' in openhab/conf/habapp. Senza deduzione partirebbero da "nessuna
        funzione" e il primo provisioning spegnerebbe un impianto che lavora.

        Indizi: la cartella delle regole (cio' che girava davvero) oppure un
        params non vuoto. In caso di dubbio si include: una funzione di troppo
        con params vuoti non fa nulla (tutte le regole fanno `if 'x' in cfg`),
        una in meno ferma l'impianto.
        """
        conf = self.config_dir()
        found = []
        for name, spec in _FUNCTIONS.items():
            if (conf / "rules" / spec["rules"]).is_dir() or self._params_has_content(name):
                found.append(name)
        return found

    def _params_has_content(self, function: str) -> bool:
        pf = self.params_file(function)
        if not pf.is_file():
            return False
        try:
            return bool(yaml.safe_load(pf.read_text()))
        except (yaml.YAMLError, OSError):
            # Un params illeggibile e' comunque il segno che qualcuno l'ha usato
            return True

    def set_functions(self, functions: list[str]) -> list[str]:
        """Persiste la scelta in arfea.yml. Rifiuta i nomi sconosciuti invece di
        scartarli in silenzio: una funzione scritta male sparirebbe senza che
        nessuno se ne accorga."""
        unknown = [f for f in functions if f not in _FUNCTIONS]
        if unknown:
            raise ValueError(
                f"Funzioni sconosciute: {', '.join(unknown)}. "
                f"Ammesse: {', '.join(_FUNCTIONS)}"
            )
        # dedup preservando l'ordine canonico
        cleaned = [f for f in _FUNCTIONS if f in functions]
        self.cfg.set_habapp_functions(cleaned)
        return cleaned

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def provision(self) -> tuple[bool, str]:
        """Prepara openhab/conf/habapp per le funzioni scelte.

        Idempotente: la si chiama ad ogni creazione del container. Ritorna
        (ok, messaggio): un fallimento NON deve impedire l'avvio del container
        (HABApp partira' senza regole, e la UI mostra il perche').
        """
        global _last_provision

        src = self.source_dir()
        if src is None:
            msg = ("Sorgenti HABApp non trovati in arfea-controller/habapp/: "
                   "aggiorna il controller (OTA) per riceverli")
            logger.warning(msg)
            _last_provision = (False, msg)
            return _last_provision

        dest = self.config_dir()
        dest.mkdir(parents=True, exist_ok=True)

        # Auto-migrazione: impianto mai configurato -> deduci e fissa la scelta,
        # cosi' la deduzione avviene una volta sola e da qui in poi comanda
        # arfea.yml (altrimenti disattivare TUTTE le funzioni sarebbe impossibile:
        # al riavvio successivo le regole ancora a terra le farebbero "tornare").
        if self.cfg.config.habapp.functions is None:
            inferred = self._infer_functions()
            self.cfg.set_habapp_functions(inferred)
            logger.warning(
                "HABApp: funzioni non configurate, dedotte dall'impianto esistente: %s "
                "(scritte in arfea.yml; modificabili dalla Web UI)",
                inferred or "(nessuna)",
            )

        selected = self.selected_functions()
        self._sync_code(src, dest, selected)
        self._ensure_params(selected)
        self._ensure_logging(src, dest)
        ok, msg = self._ensure_config(src, dest)

        _chown_tree(dest)

        if not selected:
            logger.info("HABApp: nessuna funzione attiva, deployata la sola base")
        else:
            logger.info("HABApp %s: funzioni deployate %s", src.name, selected)
        _last_provision = (ok, msg)
        return _last_provision

    def _sync_code(self, src: Path, dest: Path, selected: list[str]) -> None:
        """Risincronizza rules/ e lib/ delle funzioni gestite dal controller."""
        wanted_rules = {_FUNCTIONS[f]["rules"] for f in selected}
        wanted_libs = set(_BASE_LIBS)
        for f in selected:
            wanted_libs.update(_FUNCTIONS[f]["libs"])

        # Rimuovi SOLO le cartelle che spediamo noi e che non servono piu': una
        # funzione disattivata deve smettere di girare, ma le regole custom
        # dell'impianto (accessControl/, infraRed/, ...) non si toccano.
        managed_rules = {spec["rules"] for spec in _FUNCTIONS.values()}
        managed_libs = {lib for spec in _FUNCTIONS.values() for lib in spec["libs"]}

        for name in managed_rules - wanted_rules:
            _rm_dir(dest / "rules" / name, f"regole '{name}' (funzione disattivata)")
        for name in managed_libs - wanted_libs:
            _rm_dir(dest / "lib" / name, f"libreria '{name}' (funzione disattivata)")

        for name in sorted(wanted_rules):
            _replace_tree(src / "rules" / name, dest / "rules" / name)
        for name in sorted(wanted_libs):
            _replace_tree(src / "lib" / name, dest / "lib" / name)

    def _ensure_params(self, selected: list[str]) -> None:
        """Crea i params mancanti vuoti. Mai sovrascritti: sono la
        configurazione dell'impianto. Le regole reggono il dict vuoto (tutte
        fanno `if '<chiave>' in cfg`), e infatti irrigation.yml/loads.yml
        nascono cosi'."""
        pdir = self.params_dir()
        pdir.mkdir(parents=True, exist_ok=True)
        for f in selected:
            pf = self.params_file(f)
            if not pf.exists():
                pf.write_text("{}\n")
                logger.info("HABApp: creato params/%s vuoto", pf.name)

    def _ensure_logging(self, src: Path, dest: Path) -> None:
        """logging.yml (livello WARN di default) deployato solo se manca: se un
        tecnico alza il livello a DEBUG per diagnosticare un impianto, un
        restart del container non deve riportarlo a WARN sotto il naso."""
        dst = dest / "logging.yml"
        if dst.exists():
            return
        template = src / "logging.yml"
        if not template.is_file():
            logger.warning("HABApp: logging.yml assente nei sorgenti, non creato")
            return
        shutil.copy2(template, dst)
        logger.info("HABApp: creato logging.yml (livello di default WARN)")

    def _ensure_config(self, src: Path, dest: Path) -> tuple[bool, str]:
        """Garantisce un config.yml con un token admin valido.

        Se il token c'e' gia', il file NON si tocca: e' anche il modo in cui un
        impianto migrato si tiene il suo config.yml scritto a mano (location,
        mqtt, ...). Senza token il file va rigenerato dal template: prima se ne
        salva una copia .bak, cosi' niente va perso davvero.

        Il file va rigenerato anche quando c'e' ma senza token, ed e' il caso
        normale dopo un provisioning fallito: HABApp, se non trova config.yml,
        se ne scrive uno suo di default che punta a localhost:8080 — dentro il
        suo container, dove non c'e' nessun OpenHAB.
        """
        dst = dest / "config.yml"
        if self._read_token(dst):
            return (True, "")

        template = src / "config.yml"
        if not template.is_file():
            msg = "HABApp: config.yml assente nei sorgenti"
            logger.warning(msg)
            return (False, msg)

        token, err = self._mint_token()
        if not token:
            msg = (f"Impossibile generare il token admin OpenHAB per HABApp: {err}. "
                   f"Senza token HABApp non puo' creare item.")
            logger.warning(msg)
            return (False, msg)

        text = (template.read_text()
                .replace(_URL_PLACEHOLDER, self._openhab_url())
                .replace(_TOKEN_PLACEHOLDER, token))

        if dst.exists():
            backup = dst.with_name(f"config.yml.bak-{int(time.time())}")
            shutil.copy2(dst, backup)
            logger.warning(
                "HABApp: config.yml senza token valido, rigenerato dal template "
                "(copia del precedente in %s)", backup.name,
            )

        dst.write_text(text)
        logger.info("HABApp: config.yml scritto con un nuovo token admin")
        return (True, "")

    def _openhab_url(self) -> str:
        """OpenHAB visto dal container HABApp.

        HABApp sta sulla rete docker 'domotica', openhab e' network_mode: host:
        il gateway della rete E' l'host, dove la 8080 e' in ascolto. Preso da
        arfea.yml invece che hardcodato (172.17.0.1 era il bridge docker0, che
        funziona solo per il caso fortuito che l'host abbia anche quell'IP).
        """
        return f"http://{self.cfg.config.network.gateway}:8080"

    def _read_token(self, cfg_file: Path) -> str:
        if not cfg_file.is_file():
            return ""
        try:
            data = yaml.safe_load(cfg_file.read_text()) or {}
            user = ((data.get("openhab") or {}).get("connection") or {}).get("user")
        except (yaml.YAMLError, OSError, AttributeError) as exc:
            logger.warning("HABApp: config.yml illeggibile (%s), sara' rigenerato", exc)
            return ""
        user = str(user or "")
        return user if _TOKEN_RE.match(user) else ""

    def _mint_token(self) -> tuple[str, str]:
        if self.docker is None:
            return ("", "controller senza accesso a Docker")
        return self.docker.mint_oh_token("HABApp")

    def needs_config(self) -> bool:
        """True se HABApp sta girando (o partira') con una config che non
        abbiamo scritto noi, cioe' senza token: non creera' nessun item."""
        return not self._read_token(self.config_dir() / "config.yml")

    # ------------------------------------------------------------------
    # Params (editor Web UI)
    # ------------------------------------------------------------------

    def read_params(self, function: str) -> str:
        if function not in _FUNCTIONS:
            raise KeyError(function)
        pf = self.params_file(function)
        return pf.read_text() if pf.is_file() else "{}\n"

    def write_params(self, function: str, content: str) -> None:
        """Valida e salva un file params. Solleva ValueError con l'errore YAML
        da mostrare in UI: meglio rifiutare qui che far ripartire HABApp con un
        file rotto (le regole non caricherebbero e l'impianto resta fermo)."""
        if function not in _FUNCTIONS:
            raise KeyError(function)
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML non valido: {exc}") from exc
        if data is not None and not isinstance(data, dict):
            raise ValueError(
                f"Il file params deve contenere una mappa YAML (chiave: valore), "
                f"trovato {type(data).__name__}"
            )

        pf = self.params_file(function)
        pf.parent.mkdir(parents=True, exist_ok=True)
        if pf.is_file():
            shutil.copy2(pf, pf.with_name(f"{pf.name}.bak"))

        # Scrittura atomica: HABApp sta guardando questo file, non deve mai
        # vederlo a meta'.
        tmp = pf.with_name(f".{pf.name}.tmp")
        tmp.write_text(content if content.endswith("\n") else content + "\n")
        _chown(tmp)
        tmp.replace(pf)
        logger.info("HABApp: params/%s salvato", pf.name)

    # ------------------------------------------------------------------
    # Stato per la Web UI
    # ------------------------------------------------------------------

    def status(self) -> HABAppStatus:
        src = self.source_dir()
        selected = self.selected_functions()
        svc = self.cfg.config.services.get("habapp")
        return HABAppStatus(
            version=src.name if src else "",
            installed=src is not None,
            service_enabled=bool(svc and svc.enabled),
            token_ok=bool(self._read_token(self.config_dir() / "config.yml")),
            functions=[
                HABAppFunctionInfo(
                    name=name,
                    label=spec["label"],
                    description=spec["description"],
                    enabled=name in selected,
                    params_file=f"{spec['params']}.yml",
                )
                for name, spec in _FUNCTIONS.items()
            ],
        )


# ----------------------------------------------------------------------
# Helper filesystem
# ----------------------------------------------------------------------

def _replace_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        logger.warning("HABApp: sorgente '%s' assente, salto", src)
        return
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # __pycache__ dei sorgenti: inutile portarselo dietro (e sarebbe di un'altra
    # versione di Python rispetto a quella del container)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _rm_dir(path: Path, why: str) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        logger.info("HABApp: rimosse %s", why)


def _chown(path: Path) -> None:
    try:
        os.chown(path, OH_UID, OH_GID)
    except OSError as exc:
        logger.warning("HABApp: chown di %s fallito: %s", path, exc)


def _chown_tree(root: Path) -> None:
    _chown(root)
    for p in root.rglob("*"):
        _chown(p)
