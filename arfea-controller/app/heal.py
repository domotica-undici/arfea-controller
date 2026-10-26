"""Riparazioni automatiche dei guasti visti nelle migrazioni (Redmine #335).

Nelle migrazioni al controller del 30/09-02/10 alcuni guasti si sono ripetuti
da un impianto all'altro, quasi tutti senza un segno evidente: broker MQTT in
loop di riavvio, item MQTT a NULL dopo un riavvio di OpenHAB, binding mai
installati dopo l'arrivo del pacchetto addon, eventi degli Shelly mandati a un
bridge docker. Il controller li cerca a ogni avvio (quindi anche alla prima
applicazione di un OTA) e li corregge da solo, solo quando li trova: una
centralina sana non viene toccata. Ogni correzione va nel log («Riparazione
automatica») e in GET /api/system/repairs.

Due tempi:
  - repair_configs(): config dei servizi, lette dai container solo all'avvio.
    Gira prima di start_all_enabled; un container acceso si ferma, si corregge
    il file e si riavvia (zigbee2mqtt e zwave-js-ui riscrivono le loro config);
  - OpenHABWatch: indirizzo primario di OpenHAB, ffmpeg per ipcamera, tag della
    vecchia aasystem/arfea.py sui gruppi aggregatori e un controllo ogni 5
    minuti che riavvia OpenHAB quando resta bloccato.

Le correzioni di HABApp (url di OpenHAB, broker MQTT, regole superate come
aasystem/arfea.py) stanno in habapp_manager e le chiama _heal_habapp di main.py.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

logger = logging.getLogger(__name__)

_MAX_RECORDS = 50
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Registro delle riparazioni
# ---------------------------------------------------------------------------

def _state_file(cfg) -> Path:
    return Path(cfg.config.controller.data_path) / "arfea-controller" / ".repairs.json"


def _load_state(cfg) -> dict:
    try:
        return json.loads(_state_file(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state: dict) -> None:
    try:
        _state_file(cfg).write_text(json.dumps(state, indent=1))
    except OSError as exc:
        logger.warning("Riparazioni: stato non salvato: %s", exc)


def record(cfg, area: str, message: str) -> None:
    """Annota una riparazione fatta: log e storico (ultime 50)."""
    logger.warning("Riparazione automatica (%s): %s", area, message)
    with _lock:
        state = _load_state(cfg)
        items = state.get("repairs", [])
        items.append({"at": datetime.now().isoformat(timespec="seconds"),
                      "area": area, "message": message})
        state["repairs"] = items[-_MAX_RECORDS:]
        _save_state(cfg, state)


def repairs(cfg) -> list[dict]:
    """Storico delle riparazioni, la piu' recente per prima."""
    with _lock:
        return list(reversed(_load_state(cfg).get("repairs", [])))


# ---------------------------------------------------------------------------
# Config dei servizi
# ---------------------------------------------------------------------------

def _container_ip_host(cfg, host: str) -> bool:
    """Un indirizzo che da un container della rete del controller non e' un
    broker stabile: loopback (e' il container stesso), un IP della rete del
    controller diverso dal gateway (i container non hanno IP fissi) o della
    bridge docker0 (i vecchi stack)."""
    host = host.strip().lower()
    if host in ("localhost", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    net = cfg.config.network
    if str(ip) == net.gateway:
        return False
    nets = [ipaddress.ip_network("172.17.0.0/16")]
    try:
        nets.append(ipaddress.ip_network(net.subnet, strict=False))
    except ValueError:
        pass
    return any(ip in n for n in nets)


def _tcp_ok(docker, service: str, host: str, port: int) -> bool | None:
    """Il servizio raggiunge host:port? Prova con node dall'interno del suo
    container (zwave-js-ui e zigbee2mqtt sono node). None se non si sa."""
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        return None
    js = (f"const s=require('net').connect({{host:'{host}',port:{int(port)}}},"
          f"()=>process.exit(0));s.on('error',()=>process.exit(1));"
          f"setTimeout(()=>process.exit(1),4000)")
    rc, _ = docker.service_exec(service, ["node", "-e", js])
    if rc == -1:
        return None
    return rc == 0


def _edit_stopped(docker, service: str, edit: Callable[[], bool]) -> bool:
    """Esegue edit() col container del servizio fermo e poi lo riavvia, se
    era acceso: zwave-js-ui e zigbee2mqtt riscrivono le loro config, e una
    modifica a caldo potrebbe sparire alla loro chiusura."""
    container = docker.container_of(service)
    running = False
    if container is not None:
        container.reload()
        running = container.status in ("running", "restarting")
    if running:
        container.stop(timeout=30)
    try:
        return edit()
    finally:
        if running:
            container.start()


def _fix_mosquitto_listener(cfg, docker, data: Path) -> None:
    """Listener legato all'IP fisso del vecchio container: il broker non lo apre
    piu' («Address not available») e resta in loop di riavvio (Redmine #324)."""
    pattern = re.compile(r"(?m)^(\s*listener\s+\d+)\s+(\d+\.\d+\.\d+\.\d+)\s*$")
    changed: list[str] = []

    def edit() -> bool:
        for f in sorted((data / "mosquitto" / "config").rglob("*.conf")):
            text = f.read_text()
            new = pattern.sub(lambda m: m.group(0) if m.group(2) in ("0.0.0.0", "127.0.0.1")
                              else m.group(1) + " 0.0.0.0", text)
            if new != text:
                shutil.copy2(f, f.with_name(f.name + f".bak-{int(time.time())}"))
                f.write_text(new)
                changed.append(f.name)
        return bool(changed)

    if not any(m.group(2) not in ("0.0.0.0", "127.0.0.1")
               for f in (data / "mosquitto" / "config").rglob("*.conf")
               for m in pattern.finditer(f.read_text())):
        return
    if _edit_stopped(docker, "mosquitto", edit):
        record(cfg, "mosquitto", f"listener legato a un IP fisso portato a 0.0.0.0 in {', '.join(changed)}")


def _fix_zwave(cfg, docker, data: Path, mosquitto_on: bool) -> None:
    """zwave-js-ui: broker su un indirizzo di container (#273) e pubblicazione
    senza retain. Senza retain, dopo un riavvio di OpenHAB gli item MQTT
    restano NULL finche' ogni nodo non ritrasmette: ore, per le testine a
    batteria (105 item su un impianto). Col retain il broker, che dalla 1.8.11
    li tiene su disco, li ridà a OpenHAB appena si ricollega."""
    path = data / "zwave-js-ui" / "settings.json"
    if not path.is_file():
        return
    try:
        settings = json.loads(path.read_text())
    except ValueError:
        return
    mqtt = settings.get("mqtt")
    if not isinstance(mqtt, dict) or mqtt.get("disabled"):
        return
    host = str(mqtt.get("host", ""))
    fix_host = (mosquitto_on and _container_ip_host(cfg, host)
                and _tcp_ok(docker, "zwave-js-ui", host, int(mqtt.get("port") or 1883)) is not True)
    fix_retain = mqtt.get("retain") is not True
    if not (fix_host or fix_retain):
        return
    done: list[str] = []

    def edit() -> bool:
        current = json.loads(path.read_text())
        m = current.setdefault("mqtt", {})
        if fix_host:
            m["host"] = "mosquitto"
            done.append(f"broker {host} -> mosquitto")
        if fix_retain:
            m["retain"] = True
            done.append("retain acceso")
        shutil.copy2(path, path.with_name(f"settings.json.bak-{int(time.time())}"))
        path.write_text(json.dumps(current, indent=2))
        return True

    if _edit_stopped(docker, "zwave-js-ui", edit):
        record(cfg, "zwave-js-ui", ", ".join(done))


def _fix_zigbee2mqtt(cfg, docker, data: Path, mosquitto_on: bool) -> None:
    """zigbee2mqtt col broker su un indirizzo di container (#273): non si
    collega ed esce, in loop di riavvio."""
    path = data / "zigbee2mqtt" / "data" / "configuration.yaml"
    if not (mosquitto_on and path.is_file()):
        return
    text = path.read_text()
    m = re.search(r"(server:\s*['\"]?mqtts?://)([A-Za-z0-9.-]+)(:(\d+))?", text)
    if not m or not _container_ip_host(cfg, m.group(2)):
        return
    if _tcp_ok(docker, "zigbee2mqtt", m.group(2), int(m.group(4) or 1883)) is True:
        return
    old = m.group(2)

    def edit() -> bool:
        current = path.read_text()
        mm = re.search(r"(server:\s*['\"]?mqtts?://)([A-Za-z0-9.-]+)", current)
        if not mm or mm.group(2) != old:
            return False
        shutil.copy2(path, path.with_name(f"configuration.yaml.bak-{int(time.time())}"))
        path.write_text(current[:mm.start(2)] + "mosquitto" + current[mm.end(2):])
        return True

    if _edit_stopped(docker, "zigbee2mqtt", edit):
        record(cfg, "zigbee2mqtt", f"broker {old} -> mosquitto")


# ---------------------------------------------------------------------------
# Rete Zigbee (Redmine #387)
# ---------------------------------------------------------------------------
#
# Senza network_key, pan_id ed ext_pan_id nella config zigbee2mqtt usa i suoi
# valori predefiniti (lib/util/settings.ts), uguali su ogni installazione: la
# chiave e' pubblica e tutti gli impianti ARFEA nati dal template di prima
# avevano la stessa rete. Due centraline in laboratorio si sono prese i
# dispositivi a vicenda appena un coordinatore si e' fermato. Il template ora
# ha GENERATE (valori casuali al primo avvio). Una rete predefinita gia' in
# esercizio NON si tocca da soli: si segnala (log all'avvio, Web UI) e si
# rifa' solo su richiesta di chi e' sull'impianto, perche' vuol dire
# riassociare tutti i dispositivi.

_Z2M_DEFAULTS = {
    "network_key": [1, 3, 5, 7, 9, 11, 13, 15, 0, 2, 4, 6, 8, 10, 12, 13],
    "pan_id": 0x1A62,
    "ext_pan_id": [0xDD] * 8,
}
# Stato della rete: si spostano nella copia quando si fa una rete nuova.
_Z2M_NETWORK_FILES = ("database.db", "database.db.backup", "coordinator_backup.json", "state.json")


def _z2m_dir(data: Path) -> Path:
    return data / "zigbee2mqtt" / "data"


def _z2m_devices(d: Path) -> int | None:
    """Dispositivi associati (coordinatore escluso) nel database di
    zigbee2mqtt: una riga JSON per dispositivo. None se non si legge."""
    db = d / "database.db"
    if not db.exists():
        return 0
    n = 0
    try:
        for line in db.read_text().splitlines():
            if line.strip() and json.loads(line).get("type") != "Coordinator":
                n += 1
    except (OSError, ValueError, AttributeError):
        return None
    return n


def zigbee_network(data: Path) -> dict:
    """Rete di zigbee2mqtt, per la Web UI e per le riparazioni. La chiave non
    esce mai: solo se e' quella predefinita.

    config: c'e' configuration.yaml; default: la chiave e' quella predefinita
    (None se la config non si legge); fields: campi coi valori predefiniti;
    pan_id, ext_pan_id: come stanno nella config (GENERATE prima del primo
    avvio); devices: dispositivi associati (None se non si sa)."""
    d = _z2m_dir(data)
    path = d / "configuration.yaml"
    res = {"config": path.is_file(), "default": None, "fields": [],
           "pan_id": None, "ext_pan_id": None, "devices": _z2m_devices(d)}
    if not path.is_file():
        return res
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return res
    doc = {} if doc is None else doc
    if not isinstance(doc, dict):
        return res
    adv = doc.get("advanced") or {}
    if not isinstance(adv, dict):
        return res
    res["fields"] = [f for f, dflt in _Z2M_DEFAULTS.items() if adv.get(f) in (None, dflt)]
    res["default"] = "network_key" in res["fields"]
    pan, ext = adv.get("pan_id", _Z2M_DEFAULTS["pan_id"]), adv.get("ext_pan_id", _Z2M_DEFAULTS["ext_pan_id"])
    res["pan_id"] = f"0x{pan:04X}" if isinstance(pan, int) else str(pan)
    if isinstance(ext, list) and all(isinstance(b, int) for b in ext):
        res["ext_pan_id"] = "0x" + "".join(f"{b:02x}" for b in ext)
    else:
        res["ext_pan_id"] = str(ext)
    return res


def _z2m_set_generate(text: str, fields: list[str]) -> str | None:
    """Il testo della config con GENERATE ai campi dati, sotto advanced (i
    valori che avevano si tolgono, anche se su piu' righe). Modifica di testo,
    non un dump di PyYAML: il resto del file resta com'e'. None se il risultato
    non e' quello atteso (advanced scritto in linea, YAML insolito)."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if re.fullmatch(r"advanced:\s*(#.*)?", line.rstrip("\r\n"))), None)
    if start is None:
        if re.search(r"(?m)^advanced\s*:", text):
            return None
        sep = "" if not text or text.endswith("\n") else "\n"
        new = text + sep + "advanced:\n" + "".join(f"  {f}: GENERATE\n" for f in fields)
    else:
        end, indent = start + 1, None
        while end < len(lines):
            s = lines[end].strip()
            if s and not s.startswith("#"):
                ind = len(lines[end]) - len(lines[end].lstrip(" "))
                if ind == 0:
                    break
                indent = indent or ind
            end += 1
        indent = indent or 2
        kept, i = [], start + 1
        while i < end:
            line = lines[i]
            m = re.match(r"( *)([A-Za-z_]+)\s*:", line)
            if m and len(m.group(1)) == indent and m.group(2) in fields:
                i += 1
                # righe del valore: piu' rientrate, o elementi di lista allo stesso livello
                while i < end:
                    s = lines[i].strip()
                    ind = len(lines[i]) - len(lines[i].lstrip(" "))
                    if s and not (ind > indent or (ind == indent and s.startswith("-"))):
                        break
                    i += 1
                continue
            kept.append(line)
            i += 1
        new = "".join(lines[:start + 1] + [f"{' ' * indent}{f}: GENERATE\n" for f in fields]
                      + kept + lines[end:])
    try:
        before = yaml.safe_load(text) or {}
        after = yaml.safe_load(new) or {}
    except yaml.YAMLError:
        return None
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return None
    adv_b, adv_a = before.pop("advanced", None) or {}, after.pop("advanced", None) or {}
    if not (isinstance(adv_b, dict) and isinstance(adv_a, dict)):
        return None
    if any(adv_a.pop(f, None) != "GENERATE" for f in fields):
        return None
    for f in fields:
        adv_b.pop(f, None)
    return new if (before == after and adv_b == adv_a) else None


def _z2m_new_network(d: Path, keep: Path) -> bool:
    """Config con GENERATE a chiave, PAN e PAN esteso; database e stato della
    rete vecchia spostati in keep, con una copia della config (il backup del
    coordinatore, rimasto li', ricreerebbe la rete vecchia su alcuni adattatori)."""
    path = d / "configuration.yaml"
    new = _z2m_set_generate(path.read_text(), list(_Z2M_DEFAULTS))
    if new is None:
        logger.warning("zigbee2mqtt: %s non modificabile in sicurezza, rete non rifatta", path)
        return False
    keep.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, keep / "configuration.yaml")
    for name in _Z2M_NETWORK_FILES:
        if (d / name).exists():
            (d / name).rename(keep / name)
    tmp = path.with_name(".configuration.yaml.tmp")
    tmp.write_text(new)
    shutil.copymode(path, tmp)
    st = path.stat()
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    tmp.replace(path)
    return True


def warn_zigbee_network(data: Path) -> None:
    """All'avvio: rete Zigbee predefinita solo segnalata, mai cambiata da
    soli (anche senza dispositivi: la centralina e' in esercizio)."""
    st = zigbee_network(data)
    if st["default"]:
        n = "?" if st["devices"] is None else st["devices"]
        logger.warning("zigbee2mqtt: rete Zigbee predefinita (chiave pubblica, PAN 0x1A62), %s "
                       "dispositivi associati. Non la cambio da solo: Web UI, Impianto, Rete Zigbee "
                       "(Redmine #387)", n)


def new_zigbee_network(cfg, docker, data: Path) -> tuple[bool, str]:
    """Rete Zigbee nuova anche con dispositivi associati (POST
    /api/system/zigbee-network/new, pulsante della Web UI): si staccano tutti e
    vanno riassociati. I nomi restano (zigbee2mqtt li lega all'IEEE nella
    sezione devices della config); database e stato della rete vecchia vanno in
    una cartella accanto, per tornare indietro."""
    st = zigbee_network(data)
    if not st["config"]:
        return False, "zigbee2mqtt non ha ancora una configurazione"
    d = _z2m_dir(data)
    keep = d / f"rete-precedente-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    devices = st["devices"]

    def edit() -> bool:
        return _z2m_new_network(d, keep)

    if not _edit_stopped(docker, "zigbee2mqtt", edit):
        return False, "configuration.yaml di zigbee2mqtt non modificabile in sicurezza: rete non cambiata"
    record(cfg, "zigbee2mqtt", f"rete Zigbee nuova su richiesta (era {'predefinita' if st['default'] else 'propria'}, "
                               f"PAN {st['pan_id']}); rete vecchia in {keep.name}")
    n = "?" if devices is None else devices
    return True, (f"Rete Zigbee nuova creata. I {n} dispositivi vanno riassociati uno per uno "
                  f"(permit join e reset del dispositivo): i nomi restano. Rete vecchia in {keep}")


def repair_configs(cfg, docker) -> None:
    """Config dei servizi: gira all'avvio, prima di start_all_enabled. Un
    errore non ferma l'avvio del controller."""
    data = Path(cfg.config.controller.data_path)
    try:
        effective = cfg.resolve_effective_enabled()
    except Exception:
        effective = {}
    mosquitto_on = bool(effective.get("mosquitto"))
    steps = [
        ("mosquitto", lambda: _fix_mosquitto_listener(cfg, docker, data)),
        ("zwave-js-ui", lambda: _fix_zwave(cfg, docker, data, mosquitto_on)),
        ("zigbee2mqtt", lambda: _fix_zigbee2mqtt(cfg, docker, data, mosquitto_on)),
        ("zigbee2mqtt", lambda: warn_zigbee_network(data)),
    ]
    for name, step in steps:
        if not effective.get(name):
            continue
        try:
            step()
        except Exception as exc:
            logger.warning("Riparazioni %s: controllo fallito: %s", name, exc)


# ---------------------------------------------------------------------------
# OpenHAB
# ---------------------------------------------------------------------------

_WATCH_INTERVAL = 300
# OpenHAB acceso da almeno tanto prima di giudicarlo: su un ODROID un avvio
# con l'installazione degli addon dura anche 5-8 minuti.
_SETTLE_SECONDS = 900
# Un sintomo deve restare per almeno tanto (due giri) prima del riavvio.
_PERSIST_SECONDS = 540
# Al massimo un riavvio di OpenHAB ogni 6 ore.
_RESTART_GAP = 6 * 3600

# Regole JS dello skeleton: file -> una regola che definisce.
_JS_RULES = {
    "arfea_system.js": "arfea_time_slot_manager",
    "arfea_controller.js": "arfea_controller",
}

_IGNORED_IFACES = ("docker", "br-", "veth", "tun", "tap", "wg", "virbr", "zt", "lo")

_FFMPEG_SCRIPT = """#!/bin/bash
# ffmpeg per il binding ipcamera (istantanee e GIF delle telecamere generiche).
# Scritto dal controller (Redmine #322, #335): sui vecchi stack lo installava uno
# script dell'host montato in /etc/cont-init.d. Mai fatale: l'entrypoint gira
# sotto set -e, e senza rete OpenHAB deve partire lo stesso.
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "Installazione di ffmpeg"
    if command -v apt-get >/dev/null 2>&1; then
        { apt-get update && apt-get install -y --no-install-recommends ffmpeg; } || echo "ffmpeg non installato (manca la rete?)"
    elif command -v apk >/dev/null 2>&1; then
        apk add --no-cache ffmpeg || echo "ffmpeg non installato (manca la rete?)"
    fi
fi
"""
_FFMPEG_NAME = "30-arfea-ffmpeg"

# Tag semantici che la vecchia regola aasystem/arfea.py (tutte le versioni)
# metteva sui gruppi aggregatori: ogni membro risultava anche parte di un
# equipment che non esiste (una luce dentro «Luci» oltre che nella sua stanza).
# Gli aggregatori restano gruppi funzionali, senza tag (Redmine #519).
_LEGACY_AGGREGATOR_TAGS = {
    "gLights": ("Lightbulb",),
    "gShutters": ("Blinds",),
    "gPlugs": ("PowerOutlet",),
    "gDoorWindowSensors": ("Window",),
    "gMotionSensors": ("Motion",),
    "gAlarms": ("Alarm",),
    "gSmokeSensors": ("Smoke", "Alarm"),
    "gFloodSensors": ("Water", "Alarm"),
}


def _host(cmd: list[str], timeout: int = 15) -> str:
    try:
        res = subprocess.run(["nsenter", "-t", "1", "-m", "-n", "--", *cmd],
                             capture_output=True, text=True, timeout=timeout)
        return res.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def lan_address(cfg) -> tuple[str, list[str]]:
    """(indirizzo/prefisso della scheda della route di default, indirizzi delle
    schede vere dell'host). Bridge docker, VPN e loopback esclusi."""
    real: list[tuple[str, str]] = []
    for line in _host(["ip", "-4", "-o", "addr", "show", "scope", "global"]).splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        dev = parts[1]
        if dev.startswith(_IGNORED_IFACES):
            continue
        real.append((dev, parts[3]))
    routes = []
    for line in _host(["ip", "-4", "route", "show", "default"]).splitlines():
        m = re.search(r"\bdev\s+(\S+)", line)
        metric = re.search(r"\bmetric\s+(\d+)", line)
        if m:
            routes.append((int(metric.group(1)) if metric else 0, m.group(1)))
    for _, dev in sorted(routes):
        for d, cidr in real:
            if d == dev:
                return cidr, [c for _, c in real]
    return "", [c for _, c in real]


class OpenHABWatch:
    """Controlli su OpenHAB, al primo avvio utile e poi ogni 5 minuti."""

    def __init__(self, cfg, docker, rest: Callable, rest_ready: Callable[[], bool],
                 busy: Callable[[], str], legacy_rule_active: Callable[[], bool]):
        self.cfg = cfg
        self.docker = docker
        self.rest = rest            # (method, path, body=None) -> (code, text), con token admin
        self.rest_ready = rest_ready
        self.busy = busy            # motivo per non toccare nulla ("" = libero)
        self.legacy_rule_active = legacy_rule_active    # aasystem/arfea.py puo' ancora girare
        self._seen: dict[str, float] = {}
        self._ffmpeg_done = False
        self._aggregators_done = False

    def start(self) -> None:
        threading.Thread(target=self._loop, name="openhab-watch", daemon=True).start()

    def _loop(self) -> None:
        time.sleep(120)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("Riparazioni OpenHAB: giro fallito: %s", exc)
            time.sleep(_WATCH_INTERVAL)

    # -- giro ---------------------------------------------------------------

    def _uptime(self) -> float:
        container = self.docker.container_of("openhab")
        if container is None:
            return 0.0
        container.reload()
        if container.status != "running":
            return 0.0
        started = container.attrs.get("State", {}).get("StartedAt", "")[:19]
        try:
            t0 = datetime.strptime(started, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return 0.0
        return (datetime.now(timezone.utc) - t0).total_seconds()

    def run_once(self) -> None:
        if self.busy():
            self._seen.clear()
            return
        uptime = self._uptime()
        if uptime < _SETTLE_SECONDS:
            self._seen.clear()
            return
        if self.rest_ready():
            self._primary_address()
            self._aggregator_tags()
            things = self._things()
            self._ffmpeg(things)
        else:
            things = None
        self._check_stuck(things)

    def _things(self) -> list[dict] | None:
        code, text = self.rest("GET", "/rest/things")
        if code != "200":
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    # -- OpenHAB bloccato -----------------------------------------------------

    def _symptoms(self, things: list[dict] | None) -> list[str]:
        if not self.rest_ready():
            return ["la REST non risponde"]
        found: list[str] = []
        if things is not None:
            available = self._available_bindings()
            missing: dict[str, int] = {}
            for t in things:
                info = t.get("statusInfo") or {}
                if info.get("statusDetail") != "HANDLER_MISSING_ERROR":
                    continue
                binding = str(t.get("UID", "")).split(":", 1)[0]
                if available is None or binding in available:
                    missing[binding] = missing.get(binding, 0) + 1
            found += [f"binding {b} non caricato ({n} thing)" for b, n in sorted(missing.items())]
        js_dir = Path(self.cfg.config.controller.data_path) / "openhab" / "conf" / "automation" / "js"
        for fname, uid in _JS_RULES.items():
            if (js_dir / fname).is_file():
                code, _ = self.rest("GET", f"/rest/rules/{uid}")
                if code == "404":
                    found.append(f"regole di {fname} non caricate")
        return found

    def _available_bindings(self) -> set[str] | None:
        """Binding che OpenHAB sa installare (pacchetto addon a bordo o
        repository). None se non si riesce a saperlo."""
        code, text = self.rest("GET", "/rest/addons?serviceId=karaf")
        if code != "200":
            return None
        try:
            return {a.get("id", "") for a in json.loads(text) if a.get("type") == "binding"}
        except (ValueError, AttributeError):
            return None

    def _check_stuck(self, things: list[dict] | None) -> None:
        """Dopo l'arrivo del pacchetto addon OpenHAB e' rimasto bloccato su piu'
        impianti: REST in 404 («Observer»), nessun binding installato (66 thing
        HANDLER_MISSING_ERROR per un'ora), regole JS ARFEA non caricate
        («Failed to get any services»). Ogni volta un riavvio ha risolto."""
        now = time.time()
        symptoms = self._symptoms(things)
        self._seen = {s: self._seen.get(s, now) for s in symptoms}
        lasting = sorted(s for s, t0 in self._seen.items() if now - t0 >= _PERSIST_SECONDS)
        state = _load_state(self.cfg)
        last = state.get("openhab_restart") or {}
        if not symptoms:
            if last.get("unresolved"):
                last["unresolved"] = []
                state["openhab_restart"] = last
                _save_state(self.cfg, state)
            return
        if not lasting:
            return
        # Gia' visti dopo un riavvio fatto per loro: il riavvio non li risolve
        # (binding tolto apposta, thing di un addon non ufficiale). Niente loop.
        if set(lasting) <= set(last.get("unresolved", [])):
            return
        if now - float(last.get("at", 0)) < _RESTART_GAP:
            # Sintomi rimasti dopo il riavvio di poco fa: da qui in poi si ignorano.
            if last.get("symptoms") and not last.get("unresolved"):
                last["unresolved"] = lasting
                state["openhab_restart"] = last
                _save_state(self.cfg, state)
                logger.warning("Riparazioni OpenHAB: il riavvio non ha risolto (%s): "
                               "non riprovo, va guardato a mano", "; ".join(lasting))
            return
        reason = self.busy()
        if reason:
            return
        record(self.cfg, "openhab", f"OpenHAB bloccato ({'; '.join(lasting)}): lo riavvio")
        state = _load_state(self.cfg)
        state["openhab_restart"] = {"at": now, "symptoms": lasting, "unresolved": []}
        _save_state(self.cfg, state)
        self._seen.clear()
        self.docker.restart_service("openhab")
        self._after_openhab_restart()

    def _after_openhab_restart(self) -> None:
        """Riavviato OpenHAB, HABApp perde la sincronizzazione dei thing e va
        riavviato quando la REST e' di nuovo su."""
        deadline = time.time() + 900
        time.sleep(60)
        while time.time() < deadline and not self.rest_ready():
            time.sleep(15)
        self._restart_if_running("habapp", "dopo il riavvio di OpenHAB")

    def _restart_if_running(self, service: str, why: str) -> None:
        if not self.cfg.resolve_effective_enabled().get(service):
            return
        container = self.docker.container_of(service)
        if container is None:
            return
        container.reload()
        if container.status != "running":
            return
        res = self.docker.restart_service(service)
        logger.info("Riparazioni: %s riavviato %s: %s", service, why, res.message)

    # -- indirizzo primario ---------------------------------------------------

    def _primary_address(self) -> None:
        """OpenHAB in rete host vede anche i bridge docker e, senza
        primaryAddress, puo' prendere 172.11.0.1 scartando la LAN: gli Shelly e
        gli altri binding che ricevono eventi mandano le chiamate li' (#317).
        Si imposta la scheda della route di default se manca, o se quello
        impostato non e' piu' su nessuna scheda vera dell'host."""
        code, text = self.rest("GET", "/rest/services/org.openhab.network/config")
        if code != "200":
            return
        try:
            current = str((json.loads(text) or {}).get("primaryAddress") or "").strip()
        except (ValueError, AttributeError):
            return
        wanted, real = lan_address(self.cfg)
        if not wanted:
            return
        if current:
            try:
                net = ipaddress.ip_network(current, strict=False)
                if any(ipaddress.ip_interface(c).ip in net for c in real):
                    return
            except ValueError:
                pass
        code, text = self.rest("PUT", "/rest/services/org.openhab.network/config",
                               {"primaryAddress": wanted})
        if code not in ("200", "204"):
            logger.warning("Riparazioni OpenHAB: primaryAddress non impostato (HTTP %s): %s",
                           code, text[:200])
            return
        record(self.cfg, "openhab", f"indirizzo primario {current or '(automatico)'} -> {wanted}")
        time.sleep(30)
        self._restart_if_running("habapp", "dopo il cambio di indirizzo di OpenHAB")

    # -- tag della vecchia aasystem/arfea.py ----------------------------------

    def _aggregator_tags(self) -> None:
        """Toglie dai gruppi aggregatori i tag di _LEGACY_AGGREGATOR_TAGS. Solo
        sui gruppi gestiti via REST (quelli di un .items sono una scelta di chi
        li ha scritti) e solo quei tag. Si aspetta che la regola non possa piu'
        girare: a ogni avvio di HABApp li rimetteva. DELETE del singolo tag:
        OpenHAB modifica il gruppo sul posto, e lo stato resta."""
        if self._aggregators_done or self.legacy_rule_active():
            return
        code, text = self.rest("GET", "/rest/items?type=Group&fields=name,tags,editable")
        if code != "200":
            return
        try:
            groups = {g["name"]: g for g in json.loads(text)}
        except (ValueError, KeyError, TypeError):
            return
        for name, legacy in _LEGACY_AGGREGATOR_TAGS.items():
            group = groups.get(name)
            if not group or not group.get("editable"):
                continue
            stale = [t for t in legacy if t in (group.get("tags") or [])]
            removed: list[str] = []
            for tag in stale:
                code, text = self.rest("DELETE", f"/rest/items/{name}/tags/{tag}")
                if code not in ("200", "204"):
                    logger.warning("Riparazioni OpenHAB: tag %s di %s non tolto (HTTP %s): %s",
                                   tag, name, code, text[:200])
                    break
                removed.append(tag)
            if removed:
                record(self.cfg, "openhab", f"gruppo {name}: tolti i tag {', '.join(removed)} "
                       "della vecchia regola aasystem/arfea.py")
            if len(removed) < len(stale):
                return
        self._aggregators_done = True

    # -- ffmpeg per ipcamera -------------------------------------------------

    def _ffmpeg(self, things: list[dict] | None) -> None:
        """Binding ipcamera senza ffmpeg nel container: le telecamere generiche
        vanno OFFLINE («FFmpeg Snapshots Stopped»). Sui vecchi stack lo
        installava uno script dell'host che col controller non c'e' piu' (#322)."""
        if self._ffmpeg_done or not things:
            return
        if not any(str(t.get("UID", "")).startswith("ipcamera:") for t in things):
            self._ffmpeg_done = True
            return
        rc, _ = self.docker.openhab_exec(["sh", "-c", "command -v ffmpeg"])
        if rc == 0:
            self._ffmpeg_done = True
            return
        if rc == -1:
            return
        data = Path(self.cfg.config.controller.data_path)
        cont = data / "openhab" / "cont-init.d"
        script = cont / _FFMPEG_NAME
        if cont.is_dir() and not any("ffmpeg" in f.read_text(errors="ignore")
                                     for f in cont.iterdir() if f.is_file()):
            script.write_text(_FFMPEG_SCRIPT)
            script.chmod(0o755)
            try:
                os.chown(script, 9001, 9001)
            except OSError:
                pass
        if shutil.disk_usage(data).free < 1024 * 1024 * 1024:
            logger.warning("Riparazioni OpenHAB: ffmpeg per ipcamera non installato, meno di 1 GB libero")
            self._ffmpeg_done = True
            return
        self._ffmpeg_done = True
        rc, out = self.docker.openhab_exec(["bash", "-c", _FFMPEG_SCRIPT])
        rc2, _ = self.docker.openhab_exec(["sh", "-c", "command -v ffmpeg"])
        if rc2 == 0:
            record(self.cfg, "openhab", f"installato ffmpeg per il binding ipcamera "
                                        f"(e {_FFMPEG_NAME} in cont-init.d per i prossimi avvii)")
        else:
            logger.warning("Riparazioni OpenHAB: ffmpeg non installato: %s", out.strip()[-300:])
