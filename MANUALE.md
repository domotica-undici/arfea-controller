# ARFEA — Manuale unico

Documentazione operativa completa del sistema domotico ARFEA: installazione,
aggiornamenti, uso dei componenti, comandi Docker e risoluzione dei problemi.
Questo è **l'unico manuale**: se cerchi istruzioni, sono qui.

> Convenzione: i comandi contrassegnati `# sulla board` vanno eseguiti sulla
> centralina (via SSH o console); quelli `# sul PC` sul computer di chi pubblica
> le release.

## Indice

1. [Cos'è e architettura](#1-cosè-e-architettura)
2. [Installazione da zero](#2-installazione-da-zero)
3. [Configurazione: arfea.yml](#3-configurazione-arfeayml)
4. [Aggiornamenti](#4-aggiornamenti)
5. [Comandi Docker comuni](#5-comandi-docker-comuni)
6. [Uso dei componenti](#6-uso-dei-componenti)
7. [Interfaccia web e API REST](#7-interfaccia-web-e-api-rest)
8. [Backup e ripristino](#8-backup-e-ripristino)
9. [Sicurezza](#9-sicurezza)
10. [Risoluzione problemi comuni](#10-risoluzione-problemi-comuni)
11. [Struttura file e riferimenti](#11-struttura-file-e-riferimenti)

---

## 1. Cos'è e architettura

**ARFEA Controller** è un orchestratore Docker leggero (FastAPI/Python) per
centraline domotiche basate su OpenHAB. Gestisce ciclo di vita dei servizi,
backup/ripristino, rete, reboot dell'OS e aggiornamenti, via REST API e web UI.

```
  Host (ODROID-C4 / Armbian o Ubuntu Server)
  ┌─────────────────────────────────────────────────────────┐
  │  docker-compose.yml  →  SOLO il controller               │
  │  ┌────────────────────┐                                  │
  │  │ arfea-controller   │  porta 8888                      │
  │  │ (FastAPI)          │  monta /var/run/docker.sock       │
  │  └─────────┬──────────┘  pid: host + nsenter (host ops)  │
  │            │ Docker API                                   │
  │            ▼  crea/gestisce dinamicamente                 │
  │  openhab(8080,host) · samba(139/445) · mosquitto(1883)   │
  │  habapp · zwave-js-ui(8091) · zigbee2mqtt(8090)          │
  │  node-red(1880) · otbr(Thread/Matter)                    │
  └─────────────────────────────────────────────────────────┘
  Dati persistenti: /opt/docker_store/
```

> ⚠️ **Punto chiave da cui derivano molte cose:** l'unico servizio nel
> `docker-compose.yml` è il **controller**. Tutti gli altri container li crea e
> gestisce il controller via Docker API a partire da `arfea.yml`. Quindi
> `docker compose restart openhab` **non funziona** (openhab non è nel compose):
> per gestirlo usa la API del controller o i comandi `docker` diretti (vedi
> [§5](#5-comandi-docker-comuni)).

---

## 2. Installazione da zero

### 2.1 Prerequisiti (centralina già preparata)

L'installer assume un host **già pronto**:
- Docker + plugin `docker compose` installati e attivi.
- Cartella dati `/opt/docker_store`; per OpenHAB i file devono essere di
  proprietà **UID/GID 9001**.
- Eventuali **porte seriali** (Z-Wave/Zigbee/Modbus/Thread) già identificate.
- `curl`, `tar`/`xz`, `python3`; esecuzione come **root** (sudo).
- Rete con uscita internet (pull immagini Docker).

> **Provisioning host**: se parti da una board vergine devi prima preparare OS,
> utenti (`openhab` uid/gid 9001), Docker ed eventuale VPN. Questa fase è
> specifica dell'hardware e **non** è coperta da questo repository.

### 2.2 Installazione (`install.sh`)

Da una copia del repository, sulla centralina preparata:

```bash
sudo ./script/install.sh
```

L'installer chiede (o legge da variabili d'ambiente): **API Key** (generata se
non fornita), **URL OTA** e **manifest `releases.json`** (opzionali; vuoti =
funzione OTA disattivata), **WebDAV** per il backup (opzionale), **servizi
opzionali** (habapp, zwave-js-ui, zigbee2mqtt, node-red, otbr) e i relativi
**device seriali**. Poi crea le cartelle, configura `arfea.yml`, deploya lo
skeleton OpenHAB, avvia lo stack, crea l'utente admin OpenHAB (password
**generata** e salvata solo in locale in
`/opt/docker_store/arfea-controller/.credentials`, chmod 600) e importa i widget UI.

**Installazione non-interattiva** (automazioni/provisioning):

```bash
sudo ARFEA_NONINTERACTIVE=1 \
     ARFEA_SERVICES="habapp,zwave-js-ui" \
     ARFEA_UPDATE_URL="https://YOUR-SERVER/ota/arfea-controller.tar.xz" \
     ARFEA_RELEASES_URL="https://YOUR-SERVER/ota/releases.json" \
     ARFEA_ZWAVE_DEVICE="/dev/ttyACM0" \
     ./script/install.sh
```

Variabili: `ARFEA_API_KEY`, `ARFEA_SERVICES`, `ARFEA_UPDATE_URL`,
`ARFEA_RELEASES_URL`, `ARFEA_WEBDAV_URL/USER/PASS`, `ARFEA_ZWAVE_DEVICE`,
`ARFEA_ZIGBEE_DEVICE`, `ARFEA_MODBUS_DEVICE`, `ARFEA_OTBR_DEVICE`,
`ARFEA_OTBR_INFRA_IF`, `ARFEA_DATA_PATH`.

> **OTBR (Thread/Matter)** richiede preparazione host aggiuntiva (IPv6, avahi/
> bluez, `chip-tool`) non gestita dall'installer.

### 2.3 Installazione manuale

```bash
# sulla board
# 1. Cartelle
mkdir -p /opt/docker_store/arfea-controller/{config,backups}
mkdir -p /opt/docker_store/mosquitto/{config,data,log}
mkdir -p /opt/docker_store/openhab /opt/docker_store/zwave-js-ui /opt/docker_store/node-red

# 2. Copia i file del controller (dal repo o dal tarball estratto)
cp -r arfea-controller/* /opt/docker_store/arfea-controller/

# 3. Personalizza arfea.yml (api_key unica, WebDAV, servizi abilitati, seriali)
nano /opt/docker_store/arfea-controller/config/arfea.yml

# 4. Build e avvio (solo il controller: crea lui gli altri container)
cd /opt/docker_store/arfea-controller
docker compose build && docker compose up -d
```

Il controller crea la rete Docker `domotica` e avvia i servizi abilitati.

> Utente generico della repo (senza cloud ARFEA): imposta i tuoi URL in
> `arfea.yml` (`update_url`, `releases_url`, `webdav_*`) **prima** di generare il
> tarball, oppure modificali sulla board.

---

## 3. Configurazione: arfea.yml

`/opt/docker_store/arfea-controller/config/arfea.yml` è il cuore della
configurazione. **Non viene mai sovrascritto dall'OTA** (vedi [§4](#4-aggiornamenti))
ed è protetto anche da un backup off-config automatico (controller ≥ 1.4.0).

### Sezioni

**controller** — impostazioni del controller
```yaml
controller:
  port: 8888
  data_path: /opt/docker_store
  log_level: info
  api_key: "CAMBIARE-CON-CHIAVE-UNICA"   # richiesta per accesso da LAN
  update_url:   "https://.../ota/arfea-controller.tar.xz"  # self-update codice
  releases_url: "https://.../ota/releases.json"            # versioni immagini
  release: "2026.06"
```

**network** — rete Docker dei servizi
```yaml
network:
  name: domotica
  subnet: 172.11.0.0/24
  gateway: 172.11.0.1
```

**backup** — WebDAV per l'upload degli archivi
```yaml
backup:
  webdav_url: "https://cloud.example.com/dav/..."
  webdav_user: "user"
  webdav_password: "password"
  exclude_paths:
    - "/opt/docker_store/arfea-controller/backups"
```

**dependencies** — auto-dipendenze tra servizi
```yaml
dependencies:
  - when_any_enabled: [zwave-js-ui, zigbee2mqtt]
    then_enable: mosquitto
```

**services** — definizione di ogni container (`image`, `volumes`, `ports`,
`environment`, `devices`, `group_add`, `cap_add`, ...).

### Tipi di servizio

| Tipo | Comportamento |
|---|---|
| **core** (`core: true`) | Sempre attivo, non disattivabile (openhab, samba) |
| **auto-dipendenza** | Attivato dalle regole `dependencies` (mosquitto) |
| **opzionale** | Attivabile/disattivabile dall'utente (habapp, zwave, zigbee, node-red, otbr) |

### Device seriali (importante)

I device fisici vanno mappati nel container che li usa, con `devices:`.
`network_mode: host` **non** condivide i device: vanno passati comunque.

| Uso | Nome device nel container | Note |
|---|---|---|
| Z-Wave | `/dev/zwave` | symlink/nome libero, mappato dall'host |
| Zigbee | `/dev/zigbee` | symlink/nome libero |
| Thread/Matter (otbr) | `/dev/ttyTHREAD` | regola udev nRF52840 |
| **Modbus (OpenHAB)** | **`/dev/ttyXXX`** (es. `/dev/ttyUSB0`) | **DEVE** essere un vero `/dev/ttyXXX`: la libreria seriale di OpenHAB (nrjavaserial) non accetta symlink arbitrari |

Esempio (servizio openhab):
```yaml
    group_add:
      - "20"                              # gruppo dialout dell'host
    devices:
      - "/dev/ttyUSB0:/dev/ttyUSB0"       # Modbus RTU
    environment:
      EXTRA_JAVA_OPTS: "-Duser.timezone=Europe/Rome -Djna.nosys=true -Dgnu.io.rxtx.SerialPorts=/dev/ttyUSB0"
```

> Modificare `devices`/`environment` richiede un **recreate** del container, non
> un semplice restart (vedi [§5.4](#54-restart-vs-recreate)).

---

## 4. Aggiornamenti

### 4.1 I due canali (capirli è fondamentale)

Il sistema ha **due canali di aggiornamento distinti**, da non confondere:

| | **Aggiorna controller** | **Aggiorna versione (release)** |
|---|---|---|
| Cosa aggiorna | Codice di arfea-controller + file skeleton OpenHAB (items, regole JS, widget) | Le **immagini Docker** dei software (OpenHAB, HABApp, Z-Wave JS UI, ...) |
| Come | Scarica `arfea-controller.tar.xz` da `update_url` | Legge il manifest `releases.json` da `releases_url` |
| Cosa NON tocca | **Mai** `arfea.yml` | Solo i tag `image:` in `arfea.yml` (scrittura chirurgica), su conferma |
| Trigger UI | pulsante *"Aggiorna controller"* | card *"Aggiornamenti"* → *"Applica aggiornamento"* |
| Endpoint | `POST /api/system/update` | `POST /api/system/releases/apply` |

> ⚠️ **"Aggiorna controller" NON aggiorna OpenHAB.** Per portare OpenHAB da
> 5.1.x a 5.2.0 si usa il secondo canale (release certificate).

### 4.2 Aggiornare il controller

**Prerequisito:** `update_url` valorizzato in `arfea.yml`.

- **Automatico all'avvio:** il controller scarica il tarball, confronta l'hash
  SHA256 con l'ultimo applicato (`.update_hash`) e, se diverso, lo applica e si
  riavvia. Se l'hash è invariato non fa nulla.
- **Manuale da UI:** pulsante *"Aggiorna Controller"* nella sezione Sistema.
- **Manuale da API:**
  ```bash
  # sulla board
  curl -X POST http://localhost:8888/api/system/update
  journalctl -t arfea-update -f          # segui il rebuild
  ```

**Processo:** download → confronto hash → estrazione **saltando `config/`** →
copia file → salva hash → `docker compose up -d --build --force-recreate` via
`nsenter` (in unità **systemd transitoria**, così il rebuild sopravvive al
riavvio del container stesso) → riavvio col nuovo codice.

### 4.3 Aggiornamento manuale via tar sull'host (procedura affidabile)

Da usare se il self-update automatico non è praticabile, o come recovery. È il
metodo **più affidabile** perché bypassa `nsenter`.

```bash
# sulla board
cd /opt/docker_store/arfea-controller

# 1. METTI AL SICURO la config (l'estrazione manuale sovrascrive TUTTO, config/ inclusa!)
cp config/arfea.yml /tmp/arfea.yml.bak

# 2. estrai il nuovo tarball sopra l'installazione
tar -xJf /tmp/new.tar.xz --strip-components=1 -C /opt/docker_store/arfea-controller/

# 3. RIPRISTINA la tua config (con chiavi/credenziali/servizi reali)
cp /tmp/arfea.yml.bak config/arfea.yml

# 4. allinea l'hash così l'auto-update non ritenta inutilmente
sha256sum /tmp/new.tar.xz | awk '{print $1}' > .update_hash

# 5. rebuild e restart
docker compose up -d --build --force-recreate
docker logs -f arfea-controller
```

> ⚠️ **Differenza cruciale:** l'estrazione manuale sovrascrive **tutto, `config/`
> compresa**; l'auto-update invece salta `config/`. Per questo il passo 1 e 3
> sono obbligatori. Dal controller **≥ 1.4.0** esiste anche una rete di sicurezza:
> una copia di `arfea.yml` è tenuta fuori da `config/`
> (`/opt/docker_store/arfea-controller/.arfea.yml.bak`) e viene **ripristinata
> automaticamente all'avvio** se `config/arfea.yml` risulta mancante.

### 4.4 Aggiornare le versioni software (dall'app, non tecnico)

1. La card **"Aggiornamenti"** mostra la versione attuale e, se disponibile,
   *"Nuova versione disponibile"*.
2. Per ogni software con update appare un **interruttore** (acceso di default):
   spegni quelli che **non** vuoi aggiornare.
3. Premi **"Applica aggiornamento"**. Sequenza: backup → (migrazioni) → pull nuove
   immagini → riavvio servizi aggiornati → verifica ripartenza. In caso di
   problema fa **rollback** dei tag.

Da riga di comando (equivalente, da localhost senza API key):
```bash
# sulla board
curl -s localhost:8888/api/system/releases/check          # cosa è disponibile
curl -s -X POST localhost:8888/api/system/releases/apply  # aggiorna tutto
curl -s -X POST 'localhost:8888/api/system/releases/apply?services=openhab,habapp'  # solo alcuni
watch -n5 'curl -s localhost:8888/api/system/releases/status'
```

### 4.5 Certificare e pubblicare una nuova release (interno)

1. Scegli le versioni nuove (es. `openhab/openhab:5.2.0`).
2. **Leggi i breaking change** dai repo ufficiali (OpenHAB, HABApp, zwave-js-ui,
   zigbee2mqtt) per il salto.
3. Se servono fix, scrivi gli script in `migrations/<versione>/pre.sh` (e/o
   `post.sh`) — contratto in [migrations/README.md](migrations/README.md).
   **Rigenera e ripubblica il tarball** del controller (le migrazioni viaggiano lì).
4. **Collauda su una centralina di test** (apply reale).
5. Porta `latest` alla nuova versione in `releases.json` e pubblicalo.

Il manifest `releases.json` (su `releases_url`): `releases` è la lista ordinata
dalla più vecchia alla più recente; `latest` è la versione bersaglio; ogni release
elenca i tag certificati e opzionalmente `controller_min` e `migrations`.

### 4.6 Adozione su centraline già in produzione
- **`releases_url` mancante:** dal controller **1.3.0** viene iniettato da solo
  all'avvio (derivato da `update_url`). Basta aggiornare il controller e riavviarlo.
- **Widget "Aggiornamenti" mancante:** dalla 1.3.0 viene reimportato da solo dopo
  un OTA. Per forzare: `curl -s -X POST localhost:8888/api/system/import-ui`.

### 4.7 Migrazione da centralina esistente → arfea-controller

Lo script [script/migrate-to-controller.sh](script/migrate-to-controller.sh) porta
una centralina **già esistente** dentro la struttura `arfea-controller`. Riconosce
da solo il punto di partenza e agisce di conseguenza:

- **Sorgente DOCKER** — vecchio stack `docker-compose-arfea-2.yml` (openhab e servizi
  già in container).
- **Sorgente NATIVO** — OpenHAB installato "nativo" sul sistema operativo (apt/deb),
  **senza Docker**. Cartelle tipiche:
  - fino alla 2.5.x: `/etc/openhab2`, `/var/lib/openhab2`, `/usr/share/openhab2/addons`
  - dalla 3.x in poi: `/etc/openhab`, `/var/lib/openhab`, `/usr/share/openhab/addons`

```bash
# sulla board (da eseguire come root)
sudo bash migrate-to-controller.sh                    # rileva da solo la sorgente
sudo bash migrate-to-controller.sh /path/old-compose.yml /path/tarball.tar.xz
sudo MIGRATE_MODE=native bash migrate-to-controller.sh   # forza la modalità
```

**Sequenza (comune):** rileva la sorgente → backup dei dati → estrae il tarball
`arfea-controller` → configura `arfea.yml` (API key generata, `update_url`
disattivato al primo boot) → build + avvio dello stack.

**In più per la sorgente NATIVA:**
1. **Installa Docker** se assente (repo apt Ubuntu/Debian). Se il daemon non parte
   senza riavvio, lo script esce chiedendo un **reboot + ri-esecuzione** (le cartelle
   native non vengono toccate, quindi riprende da capo senza danni).
2. Copia `conf`/`userdata`/`addons` in `/opt/docker_store/openhab/` con owner
   **9001:9001** (escludendo `cache`/`tmp`/`logs`). **Le cartelle native NON vengono
   cancellate: restano come backup.**
3. **Servizi companion** (verificati sull'OS con `systemctl`/`pgrep`):
   - `habapp`, `mosquitto`, `samba` → **abilitati sul controller** + `stop`+`disable` nativo;
   - `frontail` → **solo `stop`+`disable`** (non più necessario, nessun servizio controller);
   - la config HABApp viene individuata (da `ExecStart --config` o path comuni) e copiata
     in `openhab/conf/habapp`.
4. **Porte seriali USB** (zwave/modbus): rilevate da `EXTRA_JAVA_OPTS`, dalle
   `things`/jsondb e dai nodi presenti. Mappate **1:1** nel container openhab (non
   rimappate su `/dev/zwave`, così le config dei binding nativi restano valide);
   aggiorna `gnu.io.rxtx.SerialPorts` e il GID di `dialout`. Vengono mappati solo i
   device **fisicamente presenti**; quelli referenziati ma assenti vengono segnalati.
5. **Solo se openhab risulta in esecuzione** dopo l'avvio → `systemctl disable` dei
   servizi nativi (così al boot parte **solo** lo stack Docker). In caso contrario
   NON disabilita nulla e stampa le istruzioni di rollback.
6. **Pulizia banner openhabian**: disattiva gli script di login in `/etc/profile.d` e
   `/etc/update-motd.d`, svuota `/etc/motd`, rimuove le righe `FireMotD`/`version.properties`
   da `bashrc`/`profile` — elimina gli errori al login SSH (`FireMotD: command not found`,
   `sed: can't read .../version.properties`, welcome ASCII di openHAB). Tutto
   **reversibile**: i file toccati sono copiati in `arfea-controller/backups/login-banners-*`.

> ⚠️ **Salto di major (2.x → 5.x):** i dati vengono comunque copiati e l'immagine
> OpenHAB 5.x prova l'upgrade automatico dell'userdata, ma da OpenHAB 2.x può
> servire una **revisione manuale** di things/binding. Lo script lo segnala e va
> sempre verificato il funzionamento dopo l'avvio. Verifica anche i parametri di
> connessione (OpenHAB/MQTT) in `openhab/conf/habapp/config.yml`.

> Lo script è **incluso nel tarball** (`arfea-controller/script/migrate-to-controller.sh`):
> se lo lanci dalla directory del repo/`script/` con il tarball accanto lo usa
> direttamente, altrimenti lo rigenera al volo con `build-update-tarball.sh`.

---

## 5. Comandi Docker comuni

### 5.1 Il compose (SOLO il controller)

I comandi `docker compose` vanno eseguiti in
`/opt/docker_store/arfea-controller/` e riguardano **solo il controller**.
```bash
# sulla board, in /opt/docker_store/arfea-controller
docker compose ps                       # stato del controller
docker compose up -d                     # avvia (usa l'immagine esistente)
docker compose up -d --no-build          # avvia senza rebuild
docker compose up -d --build --force-recreate   # rebuild + ricrea
docker compose restart arfea-controller  # riavvia il controller
docker compose logs -f arfea-controller  # log del controller
docker compose down                      # ferma e rimuove il controller
```

### 5.2 I container gestiti dal controller (modo corretto: API)

openhab, samba, mosquitto, habapp, zwave-js-ui, zigbee2mqtt, node-red, otbr **non**
sono nel compose: gestiscili dalla web UI, dal widget OpenHAB o via API.
```bash
# sulla board (localhost = niente API key)
curl -s localhost:8888/api/services                       # lista + stato
curl -s localhost:8888/api/services/openhab               # stato singolo
curl -X POST localhost:8888/api/services/openhab/start    # avvia
curl -X POST localhost:8888/api/services/openhab/restart  # riavvia (env invariate)
curl -X POST localhost:8888/api/services/openhab/recreate # RICREA (applica arfea.yml)
curl -X PUT  localhost:8888/api/services/node-red/enable  # abilita+avvia+dipendenze
curl -X PUT  localhost:8888/api/services/node-red/disable # disabilita+ferma
```

### 5.3 Comandi `docker` diretti sui container gestiti

Utili per ispezione/debug. Funzionano perché sono normali container Docker.
```bash
# sulla board
docker ps                                  # container attivi
docker ps -a                               # anche fermi/Created/Exited
docker logs -f openhab                      # log runtime
docker restart openhab                       # riavvio semplice (NON riapplica arfea.yml)
docker inspect openhab                        # config completa (device, env, mount, rete)
docker inspect openhab --format '{{json .HostConfig.Devices}}'   # device mappati
docker exec -it openhab bash                  # shell dentro il container
docker exec -u openhab openhab id             # esegui come utente openhab (uid 9001)
docker stats --no-stream                       # CPU/RAM per container
docker exec openhab printenv EXTRA_JAVA_OPTS   # variabile d'ambiente effettiva
```

### 5.4 restart vs recreate

| | `restart` (`docker restart` / `/restart`) | `recreate` (`/recreate`) |
|---|---|---|
| Cosa fa | Riavvia lo stesso container | Distrugge e **ricrea** il container da `arfea.yml` |
| Env/device/volumi | **Restano quelli vecchi** | **Riletti da `arfea.yml`** |
| Quando | Solo per far ripartire il servizio | Dopo aver cambiato `image`, `devices`, `environment`, `volumes`, ecc. |

> Regola pratica: se hai modificato `arfea.yml`, serve **recreate** (via API
> `/recreate` o dalla UI), non un semplice restart. `docker restart` mantiene le
> vecchie variabili d'ambiente e non applica le modifiche.

---

## 6. Uso dei componenti

### 6.1 OpenHAB (core, porta 8080, `network_mode: host`)
Cuore domotico. Regole in JS Scripting (`conf/automation/js/`). File deployati
dallo skeleton: `arfea.items`, `arfea_controller.js`, `linphone_call.sh`, widget.
La regola JS aggiorna gli stati verso il controller ogni 60s (cron).

### 6.2 Samba (core, porte 139/445)
Condivisione file per accesso ai `conf/` di OpenHAB da rete. Attivo di default.

### 6.3 Mosquitto (auto-dipendenza, porta 1883)
Broker MQTT. Avviato **automaticamente** quando abiliti zwave-js-ui o zigbee2mqtt;
fermato quando nessuno dei due lo richiede più. Host del broker per i client: `mosquitto`.

### 6.4 HABApp (opzionale)
Engine di automazione Python (termoregolazione, irrigazione, carichi). Regole in
`habapp/<versione>/`. Attivabile da UI/widget.

### 6.5 Z-Wave JS UI (opzionale, porta 8091)
Nella pagina di onboarding:
- porta = `/dev/zwave`
- host broker MQTT = `mosquitto`
- definire le **chiavi di crittografia** (S0/S2)
- zona/region = **Europe**

### 6.6 Zigbee2MQTT (opzionale, porta 8090)
Nella pagina di onboarding → serial:
- nome porta = `/dev/zigbee`
- stack = `ember`

### 6.7 Node-RED (opzionale, porta 1880)
Flussi low-code. Attivabile da UI/widget.

### 6.8 OTBR / Thread + Matter (opzionale)
OpenThread Border Router. Richiede la configurazione IPv6/udev fatta dallo script
di setup. Device Thread: `/dev/ttyTHREAD`. Commissioning Matter con `chip-tool`.

### 6.9 Chiamata di emergenza (linphone)
OpenHAB può fare una **chiamata vocale di emergenza** via SIP con messaggio TTS
**offline**.

- linphone (`linphone-cli`) e il TTS girano **dentro il container OpenHAB**
  (`network_mode: host` → SIP/RTP senza problemi di NAT).
- TTS: default `espeak-ng` (sempre disponibile, offline); se presente `pico2wave`
  (voce migliore) viene preferito automaticamente.
- Installazione/registrazione al boot in `cont-init.d/20-arfea-custom`, **solo se
  abilitato**. **Dopo la prima abilitazione serve un restart del container OpenHAB.**

Configurazione dalla web UI (card **Telefono di emergenza**) o `PUT /api/linphone/config`:

| Campo | Descrizione |
|---|---|
| `enabled` | Attiva la funzione |
| `sip_host` | Server SIP (es. `voip.eutelia.it`) |
| `sip_username` / `sip_password` | Credenziali SIP |
| `emergency_number` | Numero di default |
| `message` | Testo letto durante la chiamata |
| `call_timeout` | Secondi di attesa risposta |
| `repeat` | Ripetizioni del messaggio |

Trigger da OpenHAB:
- Item `arfea_emergency_call` (Switch): `ON` → chiama con i default.
- Item `arfea_emergency_message` (String): se valorizzato, sovrascrive il messaggio.
- Da regole JS: `doEmergencyCall('messaggio', '+39...')`.

### 6.10 Integrazione OpenHAB (file skeleton)

| File | Percorso in OpenHAB | Funzione |
|---|---|---|
| `arfea.items` | `conf/items/arfea.items` | Item stati servizi, rete, backup, emergenza |
| `arfea_controller.js` | `conf/automation/js/arfea_controller.js` | Comunicazione col controller |
| `linphone_call.sh` | `conf/scripts/linphone_call.sh` | Chiamata emergenza (TTS + SIP) |
| `widget_arfea_controller.yaml` | Widget (import da UI) | Pannello amministrazione |

> Ownership: ogni file sotto `/opt/docker_store/openhab/` DEVE restare
> `9001:9001` (UID/GID del container OpenHAB).

---

## 7. Interfaccia web e API REST

### Web UI
`http://<IP_CENTRALINA>:8888` — servizi (stato + restart), rete, backup/ripristino,
sistema (hostname, uptime, versione, aggiorna controller, reboot), telefono di
emergenza. Richiede la API key (salvata nel browser, auto-login). Solo LAN/VPN.

### API REST
Base: `http://<IP>:8888/api` — documentazione interattiva su `http://<IP>:8888/docs`.

**Autenticazione:**

| Origine | Auth |
|---|---|
| Localhost / Docker bridge | Nessuna (trusted) |
| VPN (10.x, 11.x) | Nessuna (trusted) |
| LAN (192.168.x) | Header `X-API-Key` |
| IP pubblici | Bloccato (403) |

**Endpoint principali:**

| Metodo | Endpoint | Funzione |
|---|---|---|
| GET | `/api/health` | Healthcheck (no auth) |
| GET | `/api/services` · `/api/services/{n}` | Lista / stato servizio |
| POST | `/api/services/{n}/start` · `/restart` · `/recreate` | Avvia / riavvia / ricrea |
| PUT | `/api/services/{n}/enable` · `/disable` | Abilita / disabilita (+dipendenze) |
| GET | `/api/system/network` · `/api/system/info` | Rete / hostname, uptime, versione |
| POST | `/api/system/reboot` | Riavvia l'OS |
| POST | `/api/system/update` | Self-update del controller |
| GET/POST | `/api/system/releases/check` · `/apply` · `/status` | Aggiornamento immagini |
| POST | `/api/system/import-ui` | Reimporta widget/pages |
| POST | `/api/backup/run` · GET `/status` · `/list` | Backup |
| POST | `/api/backup/restore?backup_name=...` | Ripristino |
| GET/PUT | `/api/linphone/config` · GET `/status` · POST `/call?number=&message=` | Emergenza |

---

## 8. Backup e ripristino

**Backup** (UI "Esegui Backup" o `POST /api/backup/run`):
1. Ferma tutti i container (tranne il controller)
2. Crea un `tar.gz` di `/opt/docker_store`
3. Carica su WebDAV (se configurato)
4. Riavvia i container che erano attivi

**Ripristino** (UI o `POST /api/backup/restore?backup_name=<FILE>`):
1. Ferma tutti i container
2. Estrae l'archivio in `/opt/docker_store`
3. Ricarica `arfea.yml`
4. Riavvia i servizi abilitati

Se il file non è locale ma il WebDAV è configurato, viene scaricato prima del
ripristino. Assicurati che `exclude_paths` includa la cartella `backups` per non
gonfiare l'archivio.

---

## 9. Sicurezza

**Principi:** minima superficie (solo IP privati); nessuna credenziale nei file
OpenHAB (le regole JS chiamano da localhost); API key solo per LAN; VPN e Docker
bridge trusted; reboot da remoto via OpenHAB Cloud → regola JS → localhost.

**Checklist nuova installazione:**
- [ ] `api_key` unica in `arfea.yml`
- [ ] Credenziali WebDAV configurate (o vuote per disabilitare l'upload)
- [ ] Porta 8888 **non** esposta su internet (no port forwarding)
- [ ] OpenVPN/WireGuard configurato per l'accesso remoto
- [ ] Se usi il self-update, `update_url` punta a un server **fidato** (il tarball
      viene estratto ed eseguito)

---

## 10. Risoluzione problemi comuni

### 10.1 Aggiornamenti / controller

- **"Aggiorna controller" ma OpenHAB non cambia versione** → normale: usa la card
  *"Aggiornamenti"* / `releases/apply` ([§4.1](#41-i-due-canali-capirli-è-fondamentale)).
- **`releases/check` risponde `error`** → `releases_url` non impostato o manifest
  non raggiungibile ([§4.6](#46-adozione-su-centraline-già-in-produzione)).
- **Nessun pulsante "Applica aggiornamento"** → widget non importato
  (`POST /api/system/import-ui`) o nessun aggiornamento disponibile.
- **Upgrade fallito** → lo stato riporta l'errore, i tag immagine vengono
  ripristinati e resta un backup: `POST /api/backup/restore?backup_name=...`.
- **L'upgrade "torna indietro" da solo (downgrade)** → risolto dalla 1.4.0. Prima,
  se OpenHAB al primo avvio impiegava più del timeout a diventare `healthy`, il
  rollback lo scambiava per fallimento. Ora un container che **gira** ma non è
  ancora healthy non innesca rollback (scatta solo su crash/exit).
- **Immagine con tag inesistente** → il pull fallisce *prima* di toccare i
  container: il servizio resta sulla versione precedente.

- **Controller resta a una versione vecchia dopo l'update / config sparita.**
  Sintomo tipico di un OTA morto a metà (rebuild fuori da systemd nelle versioni
  vecchie): restano **due container**, il vecchio `Exited` e uno nuovo `Created`
  mai avviato — e se a fare l'update era il codice vecchio, `config/arfea.yml`
  può risultare cancellata. Diagnosi e recovery:
  ```bash
  # sulla board
  docker ps -a --filter name=arfea-controller   # vedi Created/Exited doppi
  ls -la /opt/docker_store/arfea-controller/config/          # arfea.yml c'è?
  ls -la /opt/docker_store/arfea-controller/.arfea.yml.bak   # backup off-config (≥1.4.0)
  # se la config manca, ripristinala dal backup off-config (o da /tmp/arfea.yml.bak):
  cp /opt/docker_store/arfea-controller/.arfea.yml.bak \
     /opt/docker_store/arfea-controller/config/arfea.yml
  # rimuovi i container rotti e riavvia dall'immagine buildata:
  docker rm -f <nome_created> arfea-controller
  cd /opt/docker_store/arfea-controller && docker compose up -d --no-build
  curl -s localhost:8888/api/health          # verifica
  docker exec arfea-controller grep -m1 '^VERSION' /app/app/main.py   # versione runtime
  ```

### 10.2 Servizi

- **Il controller non parte** → `docker compose logs arfea-controller`. Causa
  comune: errore di sintassi in `arfea.yml`.
- **Un servizio non si avvia** → `curl localhost:8888/api/services/<nome>`,
  controlla `state`. Se un **device USB mappato non è presente sull'host**, la
  creazione del container fallisce.
- **OpenHAB non comunica col controller** →
  `docker exec openhab grep ARFEA /openhab/userdata/logs/openhab.log`.
- **Modifiche a `arfea.yml` ignorate** → hai fatto un `restart` invece di un
  `recreate` ([§5.4](#54-restart-vs-recreate)).

### 10.3 Seriale / Modbus (`Could not get port identifier`)

Errore tipico: `ModbusSlaveConnectionFactoryImpl ... Could not get port
identifier, maybe insufficient permissions`. Il messaggio "insufficient
permissions" è **spesso fuorviante**. Diagnostica **in ordine**, dal software
all'hardware:

1. **Device mappato nel container?**
   ```bash
   docker exec openhab ls -l /dev/ttyUSB0     # deve esistere DENTRO il container
   docker inspect openhab --format '{{json .HostConfig.Devices}}'
   ```
   Se manca: aggiungi `devices:` in `arfea.yml` e **recreate** ([§3](#device-seriali-importante)).
2. **Permessi dell'utente `openhab` (9001)?**
   ```bash
   docker exec -u openhab openhab id          # deve avere 20(dialout)
   docker exec -u openhab openhab sh -c 'test -r /dev/ttyUSB0 && test -w /dev/ttyUSB0 && echo OK'
   ```
   Se manca il gruppo: `group_add: ["20"]` nel servizio openhab + recreate.
3. **Contesa di porta?** Nessun altro processo deve tenere aperta la porta:
   ```bash
   sudo fuser -v /dev/ttyUSB0 ; sudo lsof /dev/ttyUSB0
   ```
4. **Registrazione porta in nrjavaserial** → aggiungi
   `-Dgnu.io.rxtx.SerialPorts=/dev/ttyUSB0` a `EXTRA_JAVA_OPTS` + recreate.
5. **Problema HARDWARE/driver** (se i punti sopra sono tutti OK e fallisce ancora).
   Test decisivo, anche **fuori da Docker**, sull'host:
   ```bash
   sudo stty -F /dev/ttyUSB0                   # "Input/output error" = guasto HW/driver
   sudo dmesg | grep -iE 'cp210|ftdi|ttyUSB|usb .*error'
   ```
   Sintomi di guasto fisico (adattatore CP210x/FTDI): `cp210x_open - Unable to
   enable UART`, `failed set request ... status: -32`, `can't set config #1,
   error -32` (EPIPE). **Nessun software lo risolve.** Rimedi:
   - Scollega e ricollega fisicamente l'adattatore (power-cycle del chip).
   - Collegalo a una **porta USB diretta della board**, non tramite hub
     (l'alimentazione instabile dell'hub è causa tipica di EPIPE sugli SBC).
   - Controlla cavo e cablaggio RS485 (A/B/GND); un guasto sul lato seriale può
     bloccare il chip.
   - Reset USB software (a volte recupera): `echo <busid> | sudo tee
     /sys/bus/usb/drivers/usb/unbind` poi `.../bind` (trova `<busid>` in
     `/sys/bus/usb/devices/`).
   - Dopo il ripristino hardware, **recreate** di openhab così riprende il device fresco.

### 10.4 Backup

- **Backup troppo grande** → verifica `exclude_paths` con la cartella `backups`.

### 10.5 Self-update

- Verifica che `update_url` sia raggiungibile: `curl -fsSL <URL> -o /dev/null`.
- Log: `docker compose logs arfea-controller | grep -i update` e
  `journalctl -t arfea-update -f`.
- Cause comuni: URL non raggiungibile, tarball malformato, spazio disco insufficiente.

---

## 11. Struttura file e riferimenti

```
/opt/docker_store/arfea-controller/
├── docker-compose.yml     # SOLO il controller
├── Dockerfile             # Python 3.11-slim + dbus, curl, iproute2
├── requirements.txt       # FastAPI, uvicorn, docker, pyyaml, httpx, pydantic
├── MANUALE.md             # questo manuale (deployato con il tarball)
├── config/
│   └── arfea.yml          # config principale (persistente, non toccata dall'OTA)
├── .arfea.yml.bak         # backup off-config (auto-restore, controller ≥1.4.0)
├── .update_hash           # hash ultimo OTA applicato
├── backups/               # archivi backup tar.gz
├── migrations/            # script di migrazione per versione
└── app/
    ├── main.py            # FastAPI + endpoint + sicurezza + self-update
    ├── models.py          # modelli Pydantic
    ├── config.py          # load YAML + dipendenze + ordine avvio
    ├── docker_manager.py  # lifecycle container Docker
    ├── backup.py          # backup/restore + WebDAV
    └── static/index.html  # web UI
```

**File nel repo:**

| File | Ruolo |
|---|---|
| [script/install.sh](script/install.sh) | Installer autonomo del controller (host già preparato) |
| [script/migrate-to-controller.sh](script/migrate-to-controller.sh) | Migra una centralina esistente (docker-compose o OpenHAB nativo) → controller |
| [script/build-update-tarball.sh](script/build-update-tarball.sh) | Genera `arfea-controller.tar.xz` |
| [ota/releases.json](ota/releases.json) | Template manifest versioni certificate |
| [migrations/README.md](migrations/README.md) | Contratto script di migrazione |
| [arfea-controller/config/arfea.yml](arfea-controller/config/arfea.yml) | Config centrale (protetta dall'OTA) |
| [arfea-controller/app/release_manager.py](arfea-controller/app/release_manager.py) | Logica check/apply versioni |
| [CLAUDE.md](CLAUDE.md) | Istruzioni per l'assistente AI (struttura repo) |
</content>
</invoke>
