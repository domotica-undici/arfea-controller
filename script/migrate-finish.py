#!/usr/bin/env python3
"""Completamento dopo migrate-to-controller.sh (Redmine #267): gira sulla centralina.

Al primo avvio dopo una migrazione OpenHAB e' lento (upgrade dell'userdata, addon,
ricaricamento di Karaf) e il controller non riprova da solo: i servizi accesi ma
rimasti fermi (tipico HABApp) e l'import della UI restano indietro. Qui si
aspetta che controller e OpenHAB siano davvero pronti, poi:
  - si avviano i servizi abilitati ma fermi (un enable li fa partire tutti);
  - si reimporta la UI ARFEA (widget e pagine);
  - la vecchia page_amministrazione (se c'e') lascia il posto alla pagina del
    controller, nella stessa posizione del menu, con una copia in
    /root/arfea-migrate/page_amministrazione.json;
  - si riavvia Node-RED, se acceso: i suoi nodi openHAB, dopo un 401 preso
    mentre OpenHAB partiva, non riprovano piu'.
Il token admin per l'API dei componenti UI si conia via Karaf e si revoca alla fine.

Uso (da root):  python3 migrate-finish.py
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 127.0.0.1 e non localhost: con ::1 bloccato dal firewall ogni chiamata aspettava 20 s (Redmine #300)
C = "http://127.0.0.1:8888/api"
O = "http://127.0.0.1:8080/rest"
TOKEN_NAME = "arfeamigrazione"      # niente trattini: Karaf li rifiuta nel nome


def http(method, url, body=None, tok=None, timeout=30):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method, data=data, headers=h),
                                    timeout=timeout) as r:
            t = r.read()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:  # noqa: BLE001 - connessione rifiutata, timeout...
        return 0, str(e)


def kar(cmd):
    """Un comando alla console Karaf del container openhab (via stdin, non argomento)."""
    p = subprocess.run(["docker", "exec", "-i", "openhab", "/openhab/runtime/bin/client", "-p", "habopen"],
                       input=cmd + "\n", capture_output=True, text=True, timeout=120)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]", "", p.stdout).replace("\r", "")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # anche con l'uscita in un file (nohup)
    for _ in range(120):
        if http("GET", C + "/health")[0] == 200 and http("GET", O + "/")[0] == 200:
            break
        time.sleep(5)
    # pronto = la console esegue i comandi openhab:* e l'API dei componenti UI risponde
    for _ in range(90):
        if "Status=" in kar("openhab:things list") and http("GET", O + "/ui/components/ui:widget")[0] in (200, 401):
            break
        time.sleep(10)
    print("controller e OpenHAB pronti", time.strftime("%H:%M:%S"))

    c, svcs = http("GET", C + "/services")
    svcs = svcs if isinstance(svcs, list) else []
    ferme = [s["name"] for s in svcs if s.get("effectively_enabled") and s.get("state") != "running"]
    if ferme:
        c, r = http("PUT", C + f"/services/{ferme[0]}/enable", timeout=600)
        print("servizi fermi", ferme, "->", c, (r or {}).get("details") if isinstance(r, dict) else r)
        # La risposta dell'enable non basta: su un impianto diceva «started» per un
        # HABApp mai creato («Conflict + cleanup failed», Redmine #331). Si guarda
        # lo stato vero, si riprova una volta e si dice chi non e' partito.
        time.sleep(10)
        for name in ferme:
            c, s = http("GET", C + f"/services/{name}")
            if isinstance(s, dict) and s.get("state") == "running":
                continue
            c, r = http("POST", C + f"/services/{name}/start", timeout=600)
            time.sleep(10)
            c, s = http("GET", C + f"/services/{name}")
            stato = s.get("state") if isinstance(s, dict) else s
            print(f"{name}: {'avviato al secondo tentativo' if stato == 'running' else f'NON PARTITO ({stato}): {r}'}")
    else:
        print("servizi: tutti in esecuzione")

    c, r = http("POST", C + "/system/import-ui", timeout=600)
    print("import UI:", c, r.get("message") if isinstance(r, dict) else r)

    if any(s["name"] == "node-red" and s.get("effectively_enabled") for s in svcs):
        c, r = http("POST", C + "/services/node-red/restart", timeout=120)
        print("node-red riavviato:", c)

    users = kar("openhab:users list")
    admin = next((m.group(1) for m in re.finditer(r"([A-Za-z0-9_.@-]+) \(administrator\)", users)), None)
    if not admin:
        print("nessun utente admin trovato: pagina non sostituita")
        return
    kar(f"openhab:users rmApiToken {admin} {TOKEN_NAME}")  # eventuale token rimasto da un giro precedente
    out = kar(f"openhab:users addApiToken {admin} {TOKEN_NAME} arfea")
    tok = next(iter(re.findall(rf"(oh\.{TOKEN_NAME}\.[A-Za-z0-9]+)", out)), None)
    if not tok:
        print("token non ottenuto: pagina non sostituita")
        return
    try:
        pages = []
        for _ in range(30):  # il registro dei componenti UI si carica dopo la REST
            c, pages = http("GET", O + "/ui/components/ui:page", tok=tok)
            if c == 200 and pages:
                break
            time.sleep(10)
        uids = {p["uid"]: p for p in pages} if c == 200 and isinstance(pages, list) else {}
        if "page_amministrazione" in uids and "page_arfeaController" in uids:
            old = uids["page_amministrazione"]
            with open("/root/arfea-migrate/page_amministrazione.json", "w") as fh:
                json.dump(old, fh, indent=1)
            new = uids["page_arfeaController"]
            new["config"]["order"] = old["config"].get("order", 99)
            new["config"]["sidebar"] = True
            print("pagina controller:", http("PUT", O + "/ui/components/ui:page/page_arfeaController", new, tok)[0],
                  "| vecchia tolta:", http("DELETE", O + "/ui/components/ui:page/page_amministrazione", tok=tok)[0])
        else:
            print("pagine presenti:", sorted(uids))
    finally:
        kar(f"openhab:users rmApiToken {admin} {TOKEN_NAME}")
        print("token revocato")


if __name__ == "__main__":
    sys.exit(main())
