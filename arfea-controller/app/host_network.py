"""Rete della centralina: LAN, wifi e access point di emergenza.

Il controller non configura la rete da se': la fa configurare a NetworkManager
sull'host, entrando nei suoi namespace con nsenter e usando nmcli/busctl/iw
dell'host (vedi script/arfea-network-nm.sh, che fa il passaggio da
netplan/systemd-networkd a NetworkManager). La configurazione vera vive quindi
nei profili di NetworkManager, che restano validi anche a controller spento;
in arfea.yml sta solo la configurazione dell'access point.

Profili gestiti:
  arfea-lan   scheda cablata (DHCP o statico), metrica 100: vince sempre sul wifi
  arfea-wifi  rete wifi a cui collegarsi, metrica 600
  arfea-ap    access point di emergenza (mai in autoconnect: lo accende il watchdog)

Watchdog: una sola radio non fa insieme client e access point, e in modalita'
AP NetworkManager smette di scansionare, quindi da solo non tornerebbe mai alla
rete configurata. Il watchdog accende l'AP quando la rete wifi manca da
grace_seconds (o quando non c'e' ne' wifi configurato ne' LAN), e con l'AP
acceso riprova la rete ogni retry_seconds, ma solo se nessuno e' collegato
all'AP: chi e' collegato sta configurando e non va buttato fuori.

Modifiche alla LAN: prima di applicarle si crea un checkpoint di NetworkManager
con rollback automatico. Se entro CONFIRM_SECONDS nessuno conferma (dal vecchio
o dal nuovo indirizzo), NetworkManager rimette da solo la configurazione di
prima: un gateway sbagliato non costa una centralina irraggiungibile.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

LAN_CON = "arfea-lan"
WIFI_CON = "arfea-wifi"
AP_CON = "arfea-ap"

CONFIRM_SECONDS = 120        # tempo per confermare una modifica alla LAN
TICK_SECONDS = 10            # passo del watchdog
MANUAL_AP_HOLD = 600         # un AP acceso a mano resta su almeno 10 minuti

_NM = "org.freedesktop.NetworkManager"
_NM_PATH = "/org/freedesktop/NetworkManager"

# Stati dispositivo di NetworkManager (NMDeviceState)
_ST_UNMANAGED = 10
_ST_DISCONNECTED = 30
_ST_ACTIVATED = 100


# ---------------------------------------------------------------------------
# Accesso all'host
# ---------------------------------------------------------------------------

def _host(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Esegue cmd con i binari e la rete dell'host (mount + network namespace)."""
    try:
        return subprocess.run(
            ["nsenter", "-t", "1", "-m", "-n", "--", *cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout dopo {timeout}s")


def _nmcli(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return _host(["nmcli", *args], timeout=timeout)


def _split_terse(line: str) -> list[str]:
    """Divide una riga di `nmcli -t` sui ':' non preceduti da backslash."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _kv(stdout: str) -> dict[str, str]:
    """Output `nmcli -t -f ... show` → {campo: valore}. I campi multipli
    (IP4.ADDRESS[1], IP4.DNS[2]...) vengono raccolti con '|'."""
    res: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        val = val.replace("\\:", ":").replace("\\\\", "\\")
        base = re.sub(r"\[\d+\]$", "", key)
        if base in res and res[base] and base != key:
            res[base] += "|" + val
        else:
            res[base] = val
    return res


def _state_code(state: str) -> int:
    """'100 (connected)' → 100."""
    try:
        return int(state.split()[0])
    except (ValueError, IndexError):
        return 0


def _err(res: subprocess.CompletedProcess) -> str:
    msg = (res.stderr or res.stdout or "").strip()
    return msg.replace("Error: ", "") or f"codice {res.returncode}"


# ---------------------------------------------------------------------------
# Lettura stato
# ---------------------------------------------------------------------------

def nm_available() -> tuple[bool, str]:
    res = _nmcli("-t", "-f", "RUNNING", "general", timeout=10)
    if res.returncode == 0 and res.stdout.strip() == "running":
        return True, ""
    if res.returncode in (126, 127) or "No such file" in (res.stderr or ""):
        return False, ("NetworkManager non installato sull'host: eseguire "
                       "sudo /opt/docker_store/arfea-controller/script/arfea-network-nm.sh")
    return False, f"NetworkManager non attivo sull'host ({_err(res)})"


def _devices() -> list[dict]:
    res = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", timeout=10)
    devs = []
    for line in res.stdout.splitlines():
        f = _split_terse(line)
        if len(f) >= 4:
            devs.append({"device": f[0], "type": f[1], "state": f[2], "connection": f[3]})
    return devs


def _device_show(dev: str) -> dict[str, str]:
    res = _nmcli("-t", "-f", "GENERAL,IP4", "device", "show", dev, timeout=10)
    return _kv(res.stdout)


def _con_show(name: str, fields: str) -> dict[str, str]:
    res = _nmcli("-t", "-f", fields, "connection", "show", name, timeout=10)
    return _kv(res.stdout) if res.returncode == 0 else {}


def _connections() -> list[dict]:
    res = _nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", timeout=10)
    out = []
    for line in res.stdout.splitlines():
        f = _split_terse(line)
        if len(f) >= 3:
            out.append({"name": f[0], "type": f[1], "device": f[2]})
    return out


def _client_profiles() -> list[dict]:
    """Profili wifi in modalita' client (qualunque nome: anche quelli fatti a mano)."""
    out = []
    for c in _connections():
        if c["type"] != "802-11-wireless" or c["name"] == AP_CON:
            continue
        s = _con_show(c["name"], "802-11-wireless.ssid,802-11-wireless.mode,"
                                 "802-11-wireless.hidden,connection.autoconnect-priority")
        if s.get("802-11-wireless.mode", "infrastructure") not in ("infrastructure", ""):
            continue
        out.append({
            "name": c["name"],
            "ssid": s.get("802-11-wireless.ssid", ""),
            "hidden": s.get("802-11-wireless.hidden") == "yes",
            "priority": int(s.get("connection.autoconnect-priority") or 0),
        })
    out.sort(key=lambda p: (p["name"] != WIFI_CON, -p["priority"]))
    return out


def wifi_device() -> Optional[str]:
    for d in _devices():
        if d["type"] == "wifi":
            return d["device"]
    return None


def _is_physical_wired(dev: str) -> bool:
    """Scheda cablata vera: ha un device fisico e non e' wifi (niente veth/bridge)."""
    base = f"/proc/1/root/sys/class/net/{dev}"
    return os.path.exists(f"{base}/device") and not os.path.exists(f"{base}/wireless")


def lan_device() -> Optional[str]:
    """La scheda del profilo arfea-lan, altrimenti la prima cablata fisica
    (gestita da NM se c'e', se no anche non gestita: la UI dira' di migrarla)."""
    s = _con_show(LAN_CON, "connection.interface-name")
    if s.get("connection.interface-name"):
        return s["connection.interface-name"]
    wired = [d for d in _devices() if d["type"] == "ethernet" and _is_physical_wired(d["device"])]
    for d in wired:
        if not d["state"].startswith("unmanaged"):
            return d["device"]
    return wired[0]["device"] if wired else None


def _lan_up() -> bool:
    """LAN collegata e con un IPv4, CHIUNQUE la gestisca. Non basta chiedere a
    NetworkManager: con la LAN ancora a systemd-networkd (centralina non
    migrata, o dopo un rollback) NM la vede 'unmanaged' e il watchdog la
    credeva scollegata, accendendo un AP che poi non spegneva piu'."""
    res = _host(["ip", "-4", "-o", "addr", "show", "scope", "global"], timeout=10)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        dev = parts[1]
        if not _is_physical_wired(dev):
            continue
        try:
            with open(f"/proc/1/root/sys/class/net/{dev}/operstate") as f:
                if f.read().strip() == "up":
                    return True
        except OSError:
            continue
    return False


def _dev_ip(show: dict[str, str]) -> dict:
    addrs = [a for a in show.get("IP4.ADDRESS", "").split("|") if a]
    dns = [a for a in show.get("IP4.DNS", "").split("|") if a]
    return {"addresses": addrs, "gateway": show.get("IP4.GATEWAY", ""), "dns": dns}


def _mac_suffix(dev: str) -> str:
    try:
        with open(f"/proc/1/root/sys/class/net/{dev}/address") as f:
            return f.read().strip().replace(":", "")[-4:].upper()
    except OSError:
        return "0000"


def _lan_subnet() -> Optional[ipaddress.IPv4Network]:
    dev = lan_device()
    if not dev:
        return None
    res = _host(["ip", "-4", "-o", "addr", "show", "dev", dev, "scope", "global"], timeout=10)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            return ipaddress.ip_interface(parts[3]).network
    return None


def ap_lan_conflict(address: str) -> Optional[str]:
    """Messaggio d'errore se la rete dell'AP si sovrappone a quella della LAN
    (due route per la stessa rete: la centralina non saprebbe dove rispondere)."""
    lan = _lan_subnet()
    ap = ipaddress.ip_interface(address).network
    if lan and lan.overlaps(ap):
        return f"la rete dell'access point ({ap}) si sovrappone a quella della LAN ({lan})"
    return None


# ---------------------------------------------------------------------------
# Checkpoint (rollback automatico delle modifiche alla LAN)
# ---------------------------------------------------------------------------

def _busctl(*args: str) -> subprocess.CompletedProcess:
    return _host(["busctl", *args], timeout=15)


def _busctl_value(res: subprocess.CompletedProcess) -> str:
    """'o "/org/..."' → '/org/...'; 'x 123' → '123'."""
    parts = res.stdout.strip().split(None, 1)
    return parts[1].strip('"') if len(parts) == 2 else ""


def _checkpoints() -> list[dict]:
    res = _busctl("get-property", _NM, _NM_PATH, _NM, "Checkpoints")
    paths = [p.strip('"') for p in res.stdout.split()[2:]]   # "ao N path..."
    try:
        with open("/proc/uptime") as f:
            now_ms = float(f.read().split()[0]) * 1000   # CLOCK_BOOTTIME, come Created
    except OSError:
        now_ms = 0
    out = []
    for p in paths:
        created = _busctl_value(_busctl("get-property", _NM, p, f"{_NM}.Checkpoint", "Created"))
        timeout = _busctl_value(_busctl("get-property", _NM, p, f"{_NM}.Checkpoint", "RollbackTimeout"))
        try:
            left = int(int(timeout) - (now_ms - int(created)) / 1000)
        except ValueError:
            left = 0
        out.append({"path": p, "seconds_left": max(0, left)})
    return out


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class HostNetworkManager:
    def __init__(self, config_manager):
        self._cfgm = config_manager
        # Serializza le operazioni sulla radio: watchdog, connessione, scansione.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.mode = "unknown"            # client | ap | idle | disabled | no-wifi | no-nm
        self.note = ""
        self._down_since: Optional[float] = None
        self._ap_since: Optional[float] = None
        self._last_try = 0.0
        self._last_station = 0.0
        self._hold_until = 0.0
        self._scan: list[dict] = []
        self._scan_at = 0.0
        self.op = {"state": "idle", "message": ""}   # ultima operazione wifi/LAN

    # -- configurazione AP ---------------------------------------------------

    @property
    def ap_cfg(self):
        return self._cfgm.config.access_point

    def ensure_ap_password(self) -> None:
        """Al primo avvio genera la password dell'AP e la salva in arfea.yml."""
        if self.ap_cfg.password:
            return
        alphabet = "abcdefghjkmnpqrstuvwxyzACDEFGHJKLMNPQRSTUVWXYZ23456789"
        pwd = "".join(secrets.choice(alphabet) for _ in range(12))
        self._cfgm.set_access_point({"password": pwd})
        logger.info("Access point: generata la password (visibile nella Web UI, sezione Wifi)")

    def ap_ssid(self, dev: Optional[str] = None) -> str:
        if self.ap_cfg.ssid:
            return self.ap_cfg.ssid
        dev = dev or wifi_device()
        return f"ARFEA-{_mac_suffix(dev) if dev else '0000'}"

    def _ensure_ap_profile(self, dev: str) -> None:
        cfg = self.ap_cfg
        settings = [
            "connection.interface-name", dev,
            "connection.autoconnect", "no",
            "802-11-wireless.ssid", self.ap_ssid(dev),
            "802-11-wireless.mode", "ap",
            "802-11-wireless.band", "bg",
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", cfg.password,
            "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp",
            "wifi-sec.group", "ccmp",
            # shared = DHCP/DNS di NetworkManager (dnsmasq) sulla rete dell'AP.
            # Niente NAT: firewall-backend=none (vedi arfea-network-nm.sh).
            "ipv4.method", "shared",
            "ipv4.addresses", cfg.address,
            "ipv6.method", "disabled",
        ]
        exists = any(c["name"] == AP_CON for c in _connections())
        if exists:
            res = _nmcli("connection", "modify", AP_CON, *settings)
        else:
            res = _nmcli("connection", "add", "type", "wifi", "con-name", AP_CON,
                         "ifname", dev, "ssid", self.ap_ssid(dev), *settings)
        if res.returncode != 0:
            raise RuntimeError(f"profilo access point: {_err(res)}")

    def _ap_up(self, dev: str, reason: str) -> bool:
        conflict = ap_lan_conflict(self.ap_cfg.address)
        if conflict:
            self.note = f"Access point non avviato: {conflict}"
            logger.error("Access point non avviato: %s", conflict)
            return False
        try:
            self._ensure_ap_profile(dev)
        except RuntimeError as exc:
            self.note = f"Impossibile creare l'access point: {exc}"
            logger.error("Access point: %s", exc)
            return False
        res = _nmcli("--wait", "30", "connection", "up", AP_CON, "ifname", dev, timeout=40)
        if res.returncode != 0:
            self.note = f"Access point non avviato: {_err(res)}"
            logger.error("Access point non avviato: %s", _err(res))
            return False
        now = time.monotonic()
        self._ap_since = now
        self._last_try = now
        self._down_since = None
        self.mode = "ap"
        self.note = reason
        logger.warning("Access point %s acceso: %s", self.ap_ssid(dev), reason)
        return True

    def _ap_down(self) -> None:
        _nmcli("--wait", "15", "connection", "down", AP_CON, timeout=25)
        self._ap_since = None

    # -- stato ----------------------------------------------------------------

    def status(self) -> dict:
        ok, msg = nm_available()
        if not ok:
            return {"available": False, "message": msg}
        lan = self.lan_status()
        wifi = self.wifi_status()
        cps = _checkpoints()
        return {
            "available": True,
            "message": "",
            "lan": lan,
            "wifi": wifi,
            "access_point": {
                "enabled": self.ap_cfg.enabled,
                "active": wifi.get("mode") == "ap",
                "ssid": self.ap_ssid(wifi.get("device")) if wifi.get("device") else "",
                "password": self.ap_cfg.password,
                "address": self.ap_cfg.address,
                "grace_seconds": self.ap_cfg.grace_seconds,
                "retry_seconds": self.ap_cfg.retry_seconds,
                "clients": self._stations(wifi["device"]) if wifi.get("mode") == "ap" else 0,
            },
            "watchdog": {"mode": self.mode, "note": self.note},
            "pending_lan_change": cps[0] if cps else None,
            "operation": self.op,
        }

    def lan_status(self) -> dict:
        dev = lan_device()
        if not dev:
            return {"device": "", "managed": False, "connected": False}
        show = _device_show(dev)
        state = _state_code(show.get("GENERAL.STATE", ""))
        prof = _con_show(LAN_CON, "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns")
        return {
            "device": dev,
            "managed": state != _ST_UNMANAGED and bool(prof),
            "connected": state == _ST_ACTIVATED,
            "mac": show.get("GENERAL.HWADDR", ""),
            "method": "static" if prof.get("ipv4.method") == "manual" else "dhcp",
            "config": {
                "address": prof.get("ipv4.addresses", "").split(",")[0].strip(),
                "gateway": prof.get("ipv4.gateway", "").replace("--", ""),
                "dns": [d.strip() for d in prof.get("ipv4.dns", "").split(",") if d.strip()],
            },
            "current": _dev_ip(show),
        }

    def wifi_status(self) -> dict:
        dev = wifi_device()
        if not dev:
            return {"device": "", "mode": "none"}
        show = _device_show(dev)
        state = _state_code(show.get("GENERAL.STATE", ""))
        conn = show.get("GENERAL.CONNECTION", "")
        profiles = _client_profiles()
        if conn == AP_CON and state >= 40:
            mode = "ap"
        elif state == _ST_ACTIVATED:
            mode = "client"
        elif 40 <= state < _ST_ACTIVATED:
            mode = "connecting"
        else:
            mode = "disconnected"
        ssid, signal = "", None
        if mode == "client":
            ssid = next((p["ssid"] for p in profiles if p["name"] == conn), conn)
            res = _nmcli("-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list",
                         "ifname", dev, "--rescan", "no", timeout=10)
            for line in res.stdout.splitlines():
                f = _split_terse(line)
                if len(f) >= 2 and f[0] == "*":
                    signal = int(f[1] or 0)
        return {
            "device": dev,
            "mode": mode,
            "ssid": ssid,
            "signal": signal,
            "ip": _dev_ip(show) if mode == "client" else None,
            "configured": [p["ssid"] for p in profiles],
        }

    def _stations(self, dev: str) -> int:
        res = _host(["iw", "dev", dev, "station", "dump"], timeout=10)
        return sum(1 for line in res.stdout.splitlines() if line.startswith("Station"))

    # -- scansione --------------------------------------------------------------

    def _do_scan(self, dev: str) -> list[dict]:
        res = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi",
                     "list", "ifname", dev, "--rescan", "yes", timeout=40)
        best: dict[str, dict] = {}
        for line in res.stdout.splitlines():
            f = _split_terse(line)
            if len(f) < 5 or not f[1]:
                continue                              # reti nascoste: senza nome
            net = {"ssid": f[1], "signal": int(f[2] or 0), "security": f[3],
                   "channel": f[4], "in_use": f[0] == "*"}
            if f[1] not in best or net["signal"] > best[f[1]]["signal"]:
                best[f[1]] = net
        nets = sorted(best.values(), key=lambda n: -n["signal"])
        if nets:
            self._scan, self._scan_at = nets, time.time()
        return nets

    def scan(self, force: bool = False) -> dict:
        """Reti visibili. Con l'AP acceso la radio non scansiona: si restituisce
        l'ultima scansione, oppure (force) si spegne l'AP per qualche secondo."""
        dev = wifi_device()
        if not dev:
            return {"networks": [], "cached": False, "age": 0, "message": "Nessuna scheda wifi"}
        with self._lock:
            ap_on = self.wifi_status()["mode"] == "ap"
            if ap_on and not force:
                return {"networks": self._scan, "cached": True,
                        "age": int(time.time() - self._scan_at) if self._scan_at else None,
                        "message": "Access point acceso: elenco dell'ultima scansione"}
            if ap_on:
                self._ap_down()
                time.sleep(2)
                nets = self._do_scan(dev)
                self._ap_up(dev, self.note or "scansione completata")
            else:
                nets = self._do_scan(dev)
            return {"networks": nets, "cached": False, "age": 0, "message": ""}

    # -- wifi: connessione ----------------------------------------------------

    def connect_wifi(self, ssid: str, password: str, hidden: bool) -> tuple[bool, str]:
        if not ssid or len(ssid.encode()) > 32:
            return False, "Nome rete (SSID) non valido"
        if password and not 8 <= len(password) <= 63:
            return False, "La password wifi deve avere da 8 a 63 caratteri"
        dev = wifi_device()
        if not dev:
            return False, "Nessuna scheda wifi"
        if self.op.get("state") == "running":
            return False, "Operazione gia' in corso"
        self.op = {"state": "running", "message": f"Connessione a {ssid}..."}
        threading.Thread(target=self._connect_worker, args=(dev, ssid, password, hidden),
                         daemon=True).start()
        return True, f"Connessione a {ssid} avviata"

    def _connect_worker(self, dev: str, ssid: str, password: str, hidden: bool) -> None:
        try:
            self._connect(dev, ssid, password, hidden)
        except Exception as exc:
            logger.exception("Wifi: connessione a %s interrotta", ssid)
            self.op = {"state": "failed", "message": f"Connessione a {ssid} interrotta: {exc}"}

    def _connect(self, dev: str, ssid: str, password: str, hidden: bool) -> None:
        tmp = WIFI_CON + "-nuova"
        with self._lock:
            was_ap = self.wifi_status()["mode"] == "ap"
            sec = next((n["security"] for n in self._scan if n["ssid"] == ssid), "")
            args = ["connection", "add", "type", "wifi", "ifname", dev, "con-name", tmp,
                    "ssid", ssid, "autoconnect", "yes",
                    "connection.autoconnect-priority", "50",
                    # La LAN (metrica 100) vince sempre sul wifi
                    "ipv4.route-metric", "600", "ipv6.route-metric", "600",
                    "ipv4.dns-priority", "100", "ipv6.dns-priority", "100",
                    "802-11-wireless.hidden", "yes" if hidden else "no"]
            if password:
                mgmt = "sae" if ("WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec) else "wpa-psk"
                args += ["wifi-sec.key-mgmt", mgmt, "wifi-sec.psk", password]
            _nmcli("connection", "delete", tmp)
            res = _nmcli(*args)
            if res.returncode != 0:
                self.op = {"state": "failed", "message": f"Profilo non creato: {_err(res)}"}
                return
            if was_ap:
                self._ap_down()
                time.sleep(2)
            res = _nmcli("--wait", "45", "connection", "up", tmp, "ifname", dev, timeout=60)
            if res.returncode == 0:
                for p in _client_profiles():
                    if p["name"] != tmp:
                        _nmcli("connection", "delete", p["name"])
                _nmcli("connection", "modify", tmp, "connection.id", WIFI_CON)
                ip = ", ".join((self.wifi_status().get("ip") or {}).get("addresses", []))
                self.mode, self.note = "client", f"Collegata a {ssid}"
                self._down_since = None
                self.op = {"state": "ok", "message": f"Collegata a {ssid} ({ip or 'IP in arrivo'})"}
                logger.info("Wifi: collegata a %s (%s)", ssid, ip)
                return
            err = _err(res)
            if "Secrets were required" in err or "802.1X supplicant" in err:
                why = "password errata"
            elif "No network with SSID" in err or "not found" in err:
                why = "rete non trovata"
            else:
                why = err
            _nmcli("connection", "delete", tmp)
            self.op = {"state": "failed", "message": f"Connessione a {ssid} fallita: {why}"}
            logger.warning("Wifi: connessione a %s fallita: %s", ssid, err)
            if was_ap:
                self._ap_up(dev, f"connessione a {ssid} fallita ({why})")
            else:
                # torna alla rete di prima, se c'era
                for p in _client_profiles():
                    _nmcli("--wait", "30", "connection", "up", p["name"], "ifname", dev, timeout=40)
                    break

    def forget_wifi(self) -> tuple[bool, str]:
        with self._lock:
            names = [p["name"] for p in _client_profiles()]
            for n in names:
                _nmcli("connection", "delete", n)
            self._down_since = None
        if not names:
            return True, "Nessuna rete wifi configurata"
        return True, "Rete wifi dimenticata"

    # -- AP manuale ------------------------------------------------------------

    def start_ap(self) -> tuple[bool, str]:
        dev = wifi_device()
        if not dev:
            return False, "Nessuna scheda wifi"
        with self._lock:
            self._scan_before_ap(dev)
            if not self._ap_up(dev, "acceso a mano dalla Web UI"):
                return False, self.note
            self._hold_until = time.monotonic() + MANUAL_AP_HOLD
        return True, f"Access point {self.ap_ssid(dev)} acceso"

    def stop_ap(self) -> tuple[bool, str]:
        with self._lock:
            self._ap_down()
            self._hold_until = 0
            self._down_since = time.monotonic()   # se serve ancora, torna dopo grace
            self.mode, self.note = "idle", "Access point spento a mano"
            dev = wifi_device()
            for p in _client_profiles():
                if dev:
                    _nmcli("--wait", "30", "connection", "up", p["name"], "ifname", dev, timeout=40)
                break
        return True, "Access point spento"

    def reapply_ap(self) -> tuple[bool, str]:
        """Dopo un cambio di configurazione: se l'AP e' acceso lo riaccende con
        i valori nuovi (chi e' collegato dovra' ricollegarsi)."""
        dev = wifi_device()
        with self._lock:
            if dev and self.wifi_status()["mode"] == "ap":
                if not self.ap_cfg.enabled and time.monotonic() >= self._hold_until:
                    self._ap_down()
                    self.mode, self.note = "disabled", "Access point di emergenza disattivato"
                    return True, "Configurazione salvata, access point spento"
                ok = self._ap_up(dev, self.note or "configurazione aggiornata")
                return ok, ("Configurazione salvata, access point riavviato" if ok else self.note)
        return True, "Configurazione access point salvata"

    def _scan_before_ap(self, dev: str) -> None:
        """Ultima scansione prima di accendere l'AP (dopo la radio e' occupata)."""
        try:
            self._do_scan(dev)
        except Exception as exc:  # la scansione e' un di piu', non deve bloccare l'AP
            logger.debug("scansione pre-AP fallita: %s", exc)

    # -- LAN -------------------------------------------------------------------

    def apply_lan(self, method: str, address: str, gateway: str, dns: list[str]) -> dict:
        """Valida, crea il checkpoint e applica in background (la risposta HTTP
        deve partire PRIMA che l'indirizzo cambi)."""
        dev = lan_device()
        if not dev:
            raise ValueError("Nessuna scheda di rete cablata gestita da NetworkManager")
        if _checkpoints():
            raise ValueError("C'e' gia' una modifica alla LAN in attesa di conferma")
        settings: list[str]
        new_ip = ""
        if method == "dhcp":
            settings = ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", "",
                        "ipv4.dns", "", "ipv4.ignore-auto-dns", "no"]
        elif method == "static":
            try:
                iface = ipaddress.ip_interface(address if "/" in address else f"{address}/24")
            except ValueError:
                raise ValueError(f"Indirizzo non valido: {address}")
            if iface.version != 4 or iface.network.prefixlen in (0, 32):
                raise ValueError("Serve un indirizzo IPv4 con subnet (es. 192.168.1.50/24)")
            if iface.ip in (iface.network.network_address, iface.network.broadcast_address):
                raise ValueError("L'indirizzo coincide con quello di rete o di broadcast")
            try:
                gw = ipaddress.ip_address(gateway)
            except ValueError:
                raise ValueError(f"Gateway non valido: {gateway}")
            if gw not in iface.network:
                raise ValueError(f"Il gateway {gw} non e' nella rete {iface.network}")
            dns_ok = []
            for d in dns or [str(gw)]:
                try:
                    dns_ok.append(str(ipaddress.ip_address(d.strip())))
                except ValueError:
                    raise ValueError(f"DNS non valido: {d}")
            ap_net = ipaddress.ip_interface(self.ap_cfg.address).network
            if iface.network.overlaps(ap_net):
                raise ValueError(f"La rete {iface.network} si sovrappone a quella dell'access point ({ap_net})")
            new_ip = str(iface.ip)
            settings = ["ipv4.method", "manual", "ipv4.addresses", str(iface),
                        "ipv4.gateway", str(gw), "ipv4.dns", ",".join(dns_ok),
                        "ipv4.ignore-auto-dns", "yes"]
        else:
            raise ValueError("Metodo non valido: dhcp o static")

        dev_path = _busctl_value(_busctl("call", _NM, _NM_PATH, _NM, "GetDeviceByIpIface", "s", dev))
        cp = _busctl("call", _NM, _NM_PATH, _NM, "CheckpointCreate", "aouu",
                     "1", dev_path, str(CONFIRM_SECONDS), "0")
        if cp.returncode != 0:
            raise ValueError(f"Checkpoint non creato, modifica annullata: {_err(cp)}")
        self.op = {"state": "running", "message": "Applicazione della nuova configurazione LAN..."}
        threading.Thread(target=self._lan_worker, args=(dev, settings), daemon=True).start()
        logger.warning("LAN: nuova configurazione %s %s, da confermare entro %ss",
                       method, new_ip, CONFIRM_SECONDS)
        return {"confirm_within": CONFIRM_SECONDS, "new_ip": new_ip}

    def _lan_worker(self, dev: str, settings: list[str]) -> None:
        time.sleep(1.5)
        try:
            res = _nmcli("connection", "modify", LAN_CON, *settings)
            if res.returncode == 0:
                res = _nmcli("--wait", "30", "connection", "up", LAN_CON, "ifname", dev, timeout=40)
        except Exception as exc:
            res = subprocess.CompletedProcess([], 1, "", str(exc))
        if res.returncode == 0:
            self.op = {"state": "ok", "message": "Nuova configurazione LAN attiva: confermala per mantenerla"}
        else:
            self.op = {"state": "failed", "message": f"Configurazione LAN non applicata: {_err(res)}"}
            self.rollback_lan()

    def confirm_lan(self) -> tuple[bool, str]:
        cps = _checkpoints()
        if not cps:
            return False, "Nessuna modifica in attesa (gia' confermata o gia' annullata)"
        for c in cps:
            _busctl("call", _NM, _NM_PATH, _NM, "CheckpointDestroy", "o", c["path"])
        self.op = {"state": "ok", "message": "Configurazione LAN confermata"}
        logger.info("LAN: nuova configurazione confermata")
        return True, "Configurazione LAN confermata"

    def rollback_lan(self) -> tuple[bool, str]:
        cps = _checkpoints()
        if not cps:
            return False, "Nessuna modifica in attesa"
        for c in cps:
            _busctl("call", _NM, _NM_PATH, _NM, "CheckpointRollback", "o", c["path"])
        logger.warning("LAN: modifica annullata, ripristinata la configurazione precedente")
        return True, "Ripristinata la configurazione LAN precedente"

    # -- watchdog --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="wifi-watchdog", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        time.sleep(15)       # lascia a NM il tempo di riprendere la rete dopo un boot
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._tick()
            except Exception as exc:
                logger.warning("Watchdog wifi: %s", exc)
            self._stop.wait(TICK_SECONDS)

    def _tick(self) -> None:
        ok, msg = nm_available()
        if not ok:
            self.mode, self.note = "no-nm", msg
            return
        devs = _devices()
        wdev = next((d for d in devs if d["type"] == "wifi"), None)
        if not wdev:
            self.mode, self.note = "no-wifi", "Nessuna scheda wifi"
            return
        dev = wdev["device"]
        show = _device_show(dev)
        state = _state_code(show.get("GENERAL.STATE", ""))
        conn = show.get("GENERAL.CONNECTION", "")
        profiles = _client_profiles()
        names = {p["name"] for p in profiles}
        lan_up = _lan_up()
        cfg = self.ap_cfg
        now = time.monotonic()

        if state == _ST_ACTIVATED and conn in names:
            if self.mode != "client":
                logger.info("Wifi: collegata a %s", conn)
            self.mode, self.note = "client", ""
            self._down_since = None
            return

        if conn == AP_CON and state >= 40:
            self.mode = "ap"
            if self._ap_since is None:        # trovato acceso (es. controller riavviato)
                self._ap_since = now
                self._last_try = now
                self.note = self.note or "Access point acceso"
            if not cfg.enabled and now >= self._hold_until:
                self._ap_down()
                self.mode, self.note = "disabled", "Access point di emergenza disattivato"
                return
            if self._stations(dev):
                self._last_station = now
                return
            quiet = now - max(self._ap_since, self._last_station, self._last_try)
            if now < self._hold_until:
                return
            if profiles and quiet >= cfg.retry_seconds:
                self._try_client(dev, profiles)
            elif not profiles and lan_up and quiet >= cfg.grace_seconds:
                self._ap_down()
                self.mode, self.note = "idle", "LAN collegata e nessuna wifi da cercare: access point spento"
                logger.info("Access point spento: LAN collegata, nessuna rete wifi configurata")
            return

        # ne' collegata ne' access point
        if not cfg.enabled:
            self.mode, self.note = "disabled", "Access point di emergenza disattivato"
            self._down_since = None
            return
        if not profiles and lan_up:
            self.mode, self.note = "idle", ""
            self._down_since = None
            return
        if self._down_since is None:
            self._down_since = now
        waited = now - self._down_since
        target = ", ".join(p["ssid"] for p in profiles)
        self.mode = "idle"
        self.note = (f"Rete {target} non raggiungibile da {int(waited)} s" if profiles
                     else f"Nessuna rete configurata e LAN scollegata da {int(waited)} s")
        if waited >= cfg.grace_seconds:
            self._scan_before_ap(dev)
            reason = (f"rete {target} non trovata" if profiles
                      else "nessuna rete wifi configurata e LAN scollegata")
            self._ap_up(dev, reason)

    def _try_client(self, dev: str, profiles: list[dict]) -> None:
        """Con l'AP acceso e nessuno collegato: spegne l'AP, cerca la rete e se
        c'e' si collega; altrimenti riaccende l'AP."""
        self._last_try = time.monotonic()
        self._ap_down()
        time.sleep(2)
        nets = self._do_scan(dev)
        visible = {n["ssid"] for n in nets}
        refused = []
        for p in profiles:
            if not (p["hidden"] or p["ssid"] in visible):
                continue
            res = _nmcli("--wait", "45", "connection", "up", p["name"], "ifname", dev, timeout=60)
            if res.returncode == 0:
                self.mode, self.note = "client", f"Rete {p['ssid']} tornata: access point spento"
                self._down_since = None
                logger.warning("Wifi: rete %s tornata disponibile, access point spento", p["ssid"])
                return
            refused.append(p["ssid"])
            logger.warning("Wifi: %s visibile ma connessione fallita: %s",
                           p["ssid"], _err(res).splitlines()[-1])
        if refused:
            # La rete c'e' ma non ci fa entrare: quasi sempre password cambiata
            reason = f"rete {', '.join(refused)} visibile ma la connessione fallisce (password cambiata?)"
        else:
            reason = f"rete {', '.join(p['ssid'] for p in profiles)} ancora non trovata"
        self._ap_up(dev, reason)
