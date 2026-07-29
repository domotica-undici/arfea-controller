#!/usr/bin/env python3
###############################################################################
# Riallinea i tag semantici degli item HABApp gia' presenti su un impianto.
#
# Perche' serve: THUtils.create_item() (lib/thermostats/utils.py) inizia con
#   if self.openhab.item_exists(itemName): return
# e thermo_commons.py avvolge le sue create_item in "if not item_exists".
# La guardia evita il 405 sugli item definiti nei .items testuali, ma comporta
# che correggere i tag nel sorgente NON abbia effetto dove l'item esiste gia':
# la correzione vale solo per installazioni nuove. Questo script chiude il buco
# sugli impianti gia' installati.
#
# Cosa NON tocca:
#   - item con "editable": false, cioe' definiti nei .items del cliente. Sono
#     di competenza dell'installatore e OpenHAB risponderebbe 405.
#   - qualsiasi item il cui set di tag non corrisponda ESATTAMENTE a una delle
#     combinazioni sbagliate prodotte dalle versioni precedenti (sotto).
#     Nel dubbio non si tocca: meglio un item non corretto che uno rovinato.
#
# Uso:
#   ./fix-semantic-tags.py                      # dry-run (default), non scrive
#   ./fix-semantic-tags.py --apply              # applica
#   ./fix-semantic-tags.py --url http://IP:8080 --token XXXX --apply
#
# Il token deve essere ADMIN: creare item e scrivere tag sono endpoint riservati
# (le richieste anonime hanno il solo ruolo USER e prenderebbero 403). Se non lo
# si passa, lo script prova a riusare quello che il controller ha gia' coniato
# per HABApp in openhab/conf/habapp/config.yml.
###############################################################################

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Regole di rimappatura.
#
# 1) Per NOME esatto: gruppi aggregatori globali. Non sono equipment fisici e
#    non stanno in nessuna Location, quindi restano fuori dal modello semantico
#    (il tag Equipment sta sui device reali che contengono).
#    HABApp li ricrea ad ogni avvio senza guardia, quindi si riallineano anche
#    da soli: qui servono solo a non dover aspettare il riavvio.
# ---------------------------------------------------------------------------
AGGREGATORI_GLOBALI = [
    "gLoads",
    "gIrrigation_pumps",
    "gIrrigation_valves",
    "onoffvalves_heat",
    "onoffvalves_cool",
    "gLights",
    "gShutters",
    "gPlugs",
    "gDoorWindowSensors",
    "gMotionSensors",
    "gAlarms",
    "gSmokeSensors",
    "gFloodSensors",
]

# ---------------------------------------------------------------------------
# 2) Per SET DI TAG esatto: sono gli item creati dai rami protetti da guardia,
#    che non si riallineano da soli. Il nome dipende dalla configurazione
#    dell'impianto (thermostati, zone), quindi si riconoscono dalla firma dei
#    tag sbagliati, non dal nome.
#
#    (tag_attuali, tipo_item, tag_nuovi, motivo)
#    tipo_item None = qualsiasi tipo.
# ---------------------------------------------------------------------------
REGOLE_TAG = [
    (
        {"Status", "Valve"}, "Group", ["Valve"],
        "Point + Equipment insieme: 'Status' e' un Point e non puo' stare su un Equipment",
    ),
    (
        {"OpenLevel"}, "Group", ["Valve"],
        "'OpenLevel' e' una Property: da sola non e' un tag valido. Il gruppo e' l'equipment valvola",
    ),
    (
        {"OpenLevel"}, "Number", ["Control", "OpenLevel"],
        "una Property richiede sempre un tag Point che la qualifichi",
    ),
    (
        {"Equipment", "Valve"}, None, ["Valve"],
        "due tag Equipment: 'Valve' e' gia' Equipment_Valve",
    ),
    (
        {"Equipment", "Pump"}, None, ["Pump"],
        "due tag Equipment: 'Pump' e' gia' Equipment_Pump",
    ),
    (
        {"Control", "Switch"}, "Number", ["Control"],
        "due tag Point: 'Switch' e' Point_Control_Switch",
    ),
]

# ---------------------------------------------------------------------------
# 3) Regola strutturale: i contatti finestra creati da windowsGroup avevano il
#    solo 'Status', che non dice cosa si sta osservando. Si aggiunge la Property
#    OpenState. Vincolata all'appartenenza a un gruppo *_windows per non toccare
#    i contatti generici dell'impianto, che hanno legittimamente il solo Status.
# ---------------------------------------------------------------------------
RE_GRUPPO_FINESTRE = re.compile(r"_windows$")

# Campi accettati dal PUT (ItemDTO). Gli altri (state, link, members, editable,
# metadata) sono derivati: rispedirli e' inutile e in alcuni casi rifiutato.
CAMPI_PUT = ("type", "name", "label", "category", "tags", "groupNames", "groupType", "function")


def leggi_token_habapp():
    """Riusa il token admin che il controller ha gia' coniato per HABApp."""
    for path in (
        "/opt/docker_store/openhab/conf/habapp/config.yml",
        "/etc/openhab/habapp/config.yml",
    ):
        try:
            with open(path, encoding="utf-8") as fh:
                testo = fh.read()
        except OSError:
            continue
        # config.yml di HABApp: openhab: user: <token>. Niente PyYAML: sull'impianto
        # questo script gira col python di sistema, che potrebbe non averlo.
        m = re.search(r"^\s*user:\s*['\"]?(oh\.[^'\"\s]+)", testo, re.M)
        if m:
            return m.group(1), path
    return None, None


def chiama(url, token, path, metodo="GET", payload=None):
    dati = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=dati, method=metodo)
    req.add_header("Accept", "application/json")
    if dati is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        corpo = resp.read().decode()
        return json.loads(corpo) if corpo.strip() else None


def calcola_correzione(item):
    """(tag_nuovi, motivo) se l'item va corretto, altrimenti (None, None)."""
    nome = item["name"]
    attuali = set(item.get("tags") or [])
    tipo = item.get("type", "")

    if nome in AGGREGATORI_GLOBALI:
        if attuali:
            return [], "aggregatore globale: non e' un equipment fisico e non sta in una Location"
        return None, None

    for tag_attesi, tipo_atteso, nuovi, motivo in REGOLE_TAG:
        if attuali == tag_attesi and (tipo_atteso is None or tipo == tipo_atteso):
            return nuovi, motivo

    if tipo == "Contact" and attuali == {"Status"}:
        if any(RE_GRUPPO_FINESTRE.search(g) for g in item.get("groupNames") or []):
            return ["Status", "OpenState"], "contatto finestra: 'Status' da solo non dice cosa si osserva"

    return None, None


def main():
    ap = argparse.ArgumentParser(
        description="Riallinea i tag semantici degli item HABApp gia' presenti sull'impianto."
    )
    ap.add_argument("--url", default=os.environ.get("OH_URL", "http://localhost:8080"),
                    help="URL di openHAB (default: %(default)s)")
    ap.add_argument("--token", default=os.environ.get("OH_TOKEN"),
                    help="API token ADMIN. Se assente si prova quello di HABApp.")
    ap.add_argument("--apply", action="store_true",
                    help="applica le correzioni (senza questo flag e' un dry-run)")
    args = ap.parse_args()

    token = args.token
    if not token:
        token, origine = leggi_token_habapp()
        if token:
            print(f"Token admin riusato da {origine}\n")

    try:
        items = chiama(args.url, token, "/rest/items?fields=name,type,tags,groupNames,label,category,groupType,function,editable")
    except urllib.error.URLError as e:
        sys.exit(f"ERRORE: openHAB non raggiungibile su {args.url}: {e}")

    da_correggere, bloccati = [], []
    for item in items:
        nuovi, motivo = calcola_correzione(item)
        if nuovi is None:
            continue
        (bloccati if not item.get("editable", False) else da_correggere).append((item, nuovi, motivo))

    if not da_correggere and not bloccati:
        print(f"Nessun item da correggere ({len(items)} esaminati): i tag sono gia' allineati.")
        return

    if da_correggere:
        print(f"Item da correggere ({len(da_correggere)} su {len(items)} esaminati):\n")
        for item, nuovi, motivo in da_correggere:
            attuali = sorted(item.get("tags") or [])
            print(f"  {item['name']}  [{item['type']}]")
            print(f"      {attuali or '[]'}  ->  {sorted(nuovi) or '[]'}")
            print(f"      {motivo}")
        print()

    if bloccati:
        print(f"Saltati: definiti nei .items del cliente, non modificabili via REST ({len(bloccati)}):")
        for item, _, _ in bloccati:
            print(f"  {item['name']}  {sorted(item.get('tags') or [])}")
        print("  -> vanno corretti a mano nel .items, oppure lasciati come sono.\n")

    if not args.apply:
        print("DRY-RUN: nessuna modifica scritta. Rilancia con --apply per applicare.")
        return

    ok = 0
    for item, nuovi, _ in da_correggere:
        # Si rispedisce l'item completo: un PUT parziale perderebbe label,
        # category, gruppi di appartenenza e - sui Group - groupType/function,
        # cioe' l'aggregazione OR/AND su cui girano le regole.
        payload = {k: item[k] for k in CAMPI_PUT if k in item and item[k] is not None}
        payload["tags"] = nuovi
        try:
            chiama(args.url, token, "/rest/items/" + item["name"], "PUT", payload)
            print(f"  OK   {item['name']}")
            ok += 1
        except urllib.error.HTTPError as e:
            dettaglio = "token admin mancante o non valido" if e.code in (401, 403) else e.reason
            print(f"  FALLITO {item['name']}: HTTP {e.code} ({dettaglio})")

    print(f"\nCorretti {ok}/{len(da_correggere)} item.")
    if ok:
        print("Verifica in Impostazioni > Stato del sistema > Modello semantico.")


if __name__ == "__main__":
    main()
