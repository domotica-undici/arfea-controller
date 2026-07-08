# ARFEA Controller

Orchestratore per un sistema domotico basato su **OpenHAB**. `arfea-controller`
è un'applicazione **FastAPI/Python** che gestisce i container Docker dell'impianto
(OpenHAB, MQTT, Z-Wave, Zigbee, HABApp, ...) tramite API REST e una web UI, con
backup, aggiornamenti OTA e gestione delle versioni certificate.

## Caratteristiche

- **Orchestrazione Docker** dei servizi domotici via API REST (porta 8888) e web UI.
- **Aggiornamento del controller** via OTA (tarball) con hash-check e rebuild sicuro.
- **Aggiornamento versioni immagini** ("release certificate"): manifest
  `releases.json`, conferma software-per-software, migrazioni versione-per-versione,
  health-gate e rollback.
- **Backup/restore** dell'intero stato su WebDAV.
- **Setup guidato** della centralina (script a due fasi per Armbian/Ubuntu).
- **Integrazione OpenHAB**: items, regole JS, widget e pagine UI di gestione.

## Architettura

```
Host (Armbian/Ubuntu)
  arfea-controller (FastAPI, porta 8888)
    ├── Docker API → crea/gestisce i container
    ├── openhab (core)          ├── zwave-js-ui (opzionale)
    ├── samba (core)            ├── zigbee2mqtt (opzionale)
    ├── mosquitto               └── node-red (opzionale)
    └── habapp (opzionale)
```

Tutti i dati persistenti stanno in `/opt/docker_store/`. Il controller è l'unico
servizio nel docker-compose; gli altri container sono creati dinamicamente.

## Avvio rapido

```bash
# Build e avvio del controller
cd arfea-controller && docker compose build && docker compose up -d

# Health check e API
curl http://localhost:8888/api/health
# Docs interattive: http://localhost:8888/docs
```

Per l'installazione completa di una centralina da zero, gli aggiornamenti e la
certificazione di nuove versioni, vedi **[MANUALE.md](MANUALE.md)**.

## Struttura del repository

| Percorso | Contenuto |
|---|---|
| `arfea-controller/` | App FastAPI, Dockerfile, docker-compose, template `arfea.yml` |
| `skeleton-openhab/` | File da deployare in OpenHAB: items, regole JS, sitemap, widget/pagine UI |
| `script/` | Setup centralina (2 fasi), build del tarball OTA, import UI, migrazione |
| `templates/` | Config predefinite per servizi opzionali (zigbee2mqtt, zwave-js-ui) |
| `migrations/` | Script di migrazione per gli aggiornamenti di versione |
| `ota/` | Template del manifest `releases.json` |

## Configurazione

Il file centrale è [`arfea-controller/config/arfea.yml`](arfea-controller/config/arfea.yml).
È un **template**: i valori sensibili (API key, credenziali WebDAV, URL del proprio
server OTA) vanno impostati in fase di installazione. Vedi il [MANUALE.md](MANUALE.md).

## Licenza

Vedi il file [LICENSE](LICENSE).
