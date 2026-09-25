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
     ARFEA_UPDATE_URL="https://cloud.domoticaundici.it/ota/arfea-controller.tar.xz" \
     ARFEA_RELEASES_URL="https://cloud.domoticaundici.it/ota/releases.json" \
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
  # Condivisione pubblica Nextcloud: l'indirizzo WebDAV, non il link /s/<token>
  webdav_url: "https://cloud.example.com/public.php/dav/files/<token>"
  webdav_user: "<token>"
  webdav_password: "password della condivisione"
  exclude_paths:
    - "/opt/docker_store/arfea-controller/backups"
```

**dependencies** — auto-dipendenze tra servizi
```yaml
dependencies:
  - when_any_enabled: [zwave-js-ui, zigbee2mqtt]
    then_enable: mosquitto
```

**access_point** — access point wifi di emergenza (vedi [6.11](#611-rete-lan-wifi-e-access-point))
```yaml
access_point:
  enabled: true
  ssid: ""                     # vuoto = ARFEA-<ultime 4 cifre del MAC wifi>
  password: ""                 # vuoto = generata dal controller al primo avvio
  address: 192.168.200.1/24    # deve stare in 192.168.0.0/16
  grace_seconds: 120           # rete wifi assente da quanto prima di accendere l'AP
  retry_seconds: 300           # con l'AP acceso, ogni quanto riprova la rete
```
Si configura anche dalla Web UI. La LAN e la rete wifi invece **non** stanno
qui: vivono nei profili di NetworkManager sull'host (`arfea-lan`, `arfea-wifi`).

**services** — definizione di ogni container (`image`, `volumes`, `ports`,
`environment`, `devices`, `group_add`, `cap_add`, ...). I valori di
`environment` sono sempre stringhe: `true`/`8080` scritti senza virgolette
vengono convertiti (controller ≥ 1.8.0; prima mandavano il controller in crash
loop).

### Tipi di servizio

| Tipo | Comportamento |
|---|---|
| **core** (`core: true`) | Sempre attivo, non disattivabile (openhab) |
| **auto-dipendenza** | Attivato dalle regole `dependencies` (mosquitto) |
| **opzionale** | Attivabile/disattivabile dall'utente (habapp, zwave, zigbee, node-red, otbr, samba) |

### Device seriali (importante)

I device fisici vanno mappati nel container che li usa, con `devices:`.
`network_mode: host` **non** condivide i device: vanno passati comunque.

| Uso | Nome device nel container | Note |
|---|---|---|
| Z-Wave | `/dev/zwave` | symlink/nome libero, mappato dall'host |
| Zigbee | `/dev/zigbee` | symlink/nome libero |
| Thread/Matter (otbr) | `/dev/ttyTHREAD` | regola udev nRF52840 |
| **Modbus (OpenHAB)** | **`/dev/ttyXXX`** (es. `/dev/ttyUSB0`) | **DEVE** essere un vero `/dev/ttyXXX`: la libreria seriale di OpenHAB (nrjavaserial) non accetta symlink arbitrari |

La colonna qui sopra è il nome **dentro il container**. Dal lato dell'**host** si usa
sempre il nome stabile in `/dev/serial/by-id/`, mai `/dev/ttyUSB0`/`/dev/ttyACM0`:
quelli li assegna il kernel nell'ordine in cui trova gli adattatori, e con due
adattatori USB un riavvio può scambiarli. Su un impianto, dopo un riavvio, OpenHAB ha
parlato Modbus con la chiavetta Z-Wave e zwave-js-ui con il cavo RS485 per un mese
(Redmine #243). Il by-id è anche descrittivo
(`usb-FTDI_USB-RS485_Cable_…`, `usb-SONOFF_…_ZWave_Dongle_…`): si vede a colpo
d'occhio quale dispositivo va a quale servizio. `ls -l /dev/serial/by-id/` sull'host
dice a quale tty corrisponde oggi ciascuno.

Esempio (servizio openhab):
```yaml
    group_add:
      - "20"                              # gruppo dialout dell'host
    devices:
      # Modbus RTU: nome stabile sull'host, /dev/ttyUSB0 nel container
      - "/dev/serial/by-id/usb-FTDI_USB-RS485_Cable_ABC12345-if00-port0:/dev/ttyUSB0"
    environment:
      EXTRA_JAVA_OPTS: "-Duser.timezone=Europe/Rome -Djna.nosys=true -Dgnu.io.rxtx.SerialPorts=/dev/ttyUSB0"
```

Dal controller **1.8.8** la Web UI avvisa (card *Da guardare* e pagina
*Dispositivi*) quando un servizio acceso usa il nome del kernel e la porta ha un
by-id. Dice quale dispositivo c'è **oggi** dietro quel nome e propone *Usa il nome
stabile*. Prima di premerlo controlla che quel dispositivo sia quello giusto per il
servizio: se le porte si sono già scambiate, il by-id proposto sarebbe quello
sbagliato e va scelto l'altro. Lo stesso elenco arriva da
`GET /api/system/serial-devices/warnings`.

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
> 5.2.0 a 5.2.1 si usa il secondo canale (release certificate).

### 4.2 Aggiornare il controller

**Prerequisito:** `update_url` valorizzato in `arfea.yml`. Non deve **mai** restare
vuoto, perché una centralina senza non riceve più l'OTA e nessuno se ne accorge: dal
controller **1.8.2**, se lo trova vuoto all'avvio lo rimette al default
(`https://cloud.domoticaundici.it/ota/arfea-controller.tar.xz`) e lo salva. Su un
controller più vecchio col campo vuoto va scritto a mano in `arfea.yml`, seguito da
`docker compose restart arfea-controller`: senza URL non può scaricare la versione
che si ripara da sola.

Stesso discorso per il segnaposto `https://YOUR-SERVER.example.com/...`, che la repo
pubblica metteva al posto dell'host vero fino al 17/07/2026: chi ha installato da
quel clone se lo ritrova in `update_url` o `releases_url` (su un impianto era in
`releases_url`, che così non vedeva nessuna release). Dal controller **1.8.8** vale come
vuoto e all'avvio torna al default; prima va corretto a mano.

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
metodo **più affidabile** perché bypassa `nsenter`. È **obbligatorio** per un
controller più vecchio della **1.2.1**: il suo self-update ricostruisce l'immagine
dentro il cgroup del container, che muore con la recreate, e il controller nuovo
resta in `Created` senza mai partire (quei controller riportano la versione
«1.0.0», Redmine #239).

Prima controlla `docker buildx version`: il Dockerfile usa `$BUILDPLATFORM`, che
senza buildx non esiste, e la build fallisce (ora e a ogni OTA). Le centraline
installate ad aprile 2026 non lo hanno: `apt-get install -y docker-buildx-plugin`,
solo quel pacchetto.

```bash
# sulla board
cd /opt/docker_store/arfea-controller

# 1. METTI AL SICURO la config (l'estrazione manuale sovrascrive TUTTO, config/ inclusa!)
cp config/arfea.yml /tmp/arfea.yml.bak

# 2. estrai il nuovo tarball sopra l'installazione
tar -xJf /tmp/new.tar.xz --strip-components=1 -C /opt/docker_store/arfea-controller/

# 3. RIPRISTINA la tua config (con chiavi/credenziali/servizi reali)
cp /tmp/arfea.yml.bak config/arfea.yml

# 4. porta lo skeleton nella conf di OpenHAB (regole JS, item, script): il
#    self-update lo fa da solo, l'estrazione manuale no
cp -r skeleton-openhab/conf/. /opt/docker_store/openhab/conf/
cp skeleton-openhab/cont-init.d/* /opt/docker_store/openhab/cont-init.d/
chown -R 9001:9001 /opt/docker_store/openhab/conf /opt/docker_store/openhab/cont-init.d

# 5. allinea l'hash così l'auto-update non ritenta inutilmente
sha256sum /tmp/new.tar.xz | awk '{print $1}' > .update_hash

# 6. rebuild e restart, in un'unità systemd: sopravvive a una sessione SSH che cade
systemd-run --unit=arfea-selfupdate --collect bash -c \
  "cd /opt/docker_store/arfea-controller && docker compose up -d --build --force-recreate 2>&1 | logger -t arfea-update; docker image prune -f"
journalctl -t arfea-update -f
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
   spegni quelli che **non** vuoi aggiornare. Quello che non ha un interruttore
   (codice HABApp, mosquitto, otbr) si aggiorna insieme al resto (controller ≥ 1.8.8).
   Un servizio **spento** non compare: riceve solo la nuova versione in `arfea.yml`,
   senza download né riavvio, e la scarica quando lo si accende. Se cambiano solo
   servizi spenti, l'aggiornamento non fa nemmeno il backup.
3. Premi **"Applica aggiornamento"**. Sequenza: backup → (migrazioni) → pull nuove
   immagini → riavvio servizi aggiornati → verifica ripartenza. In caso di
   problema fa **rollback** dei tag.
4. **Segui l'avanzamento nella stessa card** (dal controller 1.8.3). Compare una riga
   *"Aggiornamento in corso"* con la fase (backup, scaricamento, riavvio, …), la
   percentuale, una barra e il dettaglio (versione di destinazione, componente, ora
   di inizio), aggiornata ogni 10 secondi. Il pulsante sparisce finché l'aggiornamento
   gira. Alla fine la riga dice *"Aggiornamento completato"* (verde) o *"Aggiornamento
   non riuscito"* (rosso) con il motivo.
   - Se si aggiorna OpenHAB, la pagina **sparisce per qualche minuto**, mentre OpenHAB
     riparte con la versione nuova: la riga lo annuncia prima. Quando la pagina torna,
     entro un minuto la riga riprende da dove era arrivato il controller.
   - Lo stesso avanzamento si vede nella Web UI del controller (`http://<IP>:8888`,
     card *"Aggiornamento software"*), che resta raggiungibile anche mentre OpenHAB
     si riavvia.

> ⚠️ **OpenHAB 5.2.0 e controller fino alla 1.8.2:** i pulsanti del widget ARFEA non
> fanno niente (la regola riceve l'azione vuota: nel log `ARFEA action=` e `unknown
> action ""`). Il controller 1.8.3 corregge la regola; nel frattempo l'aggiornamento
> si applica dalla Web UI del controller o dalla riga di comando qui sotto.

Da riga di comando (equivalente, da localhost senza API key):
```bash
# sulla board
curl -s localhost:8888/api/system/releases/check          # cosa è disponibile
curl -s -X POST localhost:8888/api/system/releases/apply  # aggiorna tutto
curl -s -X POST 'localhost:8888/api/system/releases/apply?services=openhab,habapp'  # solo alcuni
curl -s -X POST 'localhost:8888/api/system/releases/apply?exclude=zwave-js-ui'      # tutti tranne questi (1.8.8)
watch -n5 'curl -s localhost:8888/api/system/releases/status'   # fase, messaggio, progress (%)
cd /opt/docker_store/arfea-controller && docker compose logs -f --tail 50 arfea-controller   # il dettaglio
```

### 4.5 Certificare e pubblicare una nuova release (interno)

1. Scegli le versioni nuove (es. `openhab/openhab:5.2.0`). **Sempre tag esatti**, mai
   `:latest` né una major mobile come `:11`: l'apply scarica un'immagine solo quando il
   tag cambia, quindi un tag mobile resta per sempre all'immagine del giorno
   dell'installazione, diversa da impianto a impianto. Nel manifest vanno **tutte** le
   immagini (dalla 2026.09.02 anche mosquitto, zigbee2mqtt, node-red, otbr), non solo
   quelle dei servizi accesi: fuori resta solo samba. Controlla anche che il tag non sia
   più vecchio di quello che `:latest` dava già agli impianti (Node-RED: `latest` è la
   5.x dal 9/6/2026), per non fare un downgrade.
2. **Leggi i breaking change** dai repo ufficiali (OpenHAB, HABApp, zwave-js-ui,
   zigbee2mqtt, mosquitto, node-red) per il salto. Se prima il tag era mobile, la
   versione di partenza è sconosciuta: si legge dalla prima della major.
3. Se servono fix, scrivi gli script in `migrations/<versione>/pre.sh` (e/o
   `post.sh`) — contratto in [migrations/README.md](migrations/README.md).
   **Rigenera e ripubblica il tarball** del controller (le migrazioni viaggiano lì).
4. **Collauda su una centralina di test** (apply reale).
5. Porta `latest` alla nuova versione in `releases.json` e pubblicalo. La release nuova
   va **prima** di `2026.08-ESEMPIO`: l'ordine della lista è il percorso di upgrade, e
   dopo l'esempio si attraverserebbero le sue migrazioni.

Il manifest `releases.json` (su `releases_url`): `releases` è la lista ordinata
dalla più vecchia alla più recente; `latest` è la versione bersaglio; ogni release
elenca i tag certificati e opzionalmente `controller_min` e `migrations`.

### 4.6 Adozione su centraline già in produzione
- **`releases_url` mancante:** dal controller **1.3.0** viene iniettato da solo
  all'avvio (derivato da `update_url`). Basta aggiornare il controller e riavviarlo.
- **`update_url` vuoto:** dal controller **1.8.2** viene rimesso al default all'avvio.
  Le centraline migrate con `migrate-to-controller.sh` fino alla 1.8.1 lo avevano
  vuoto di proposito: vanno sistemate a mano (vedi §4.2).
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
sudo MIGRATE_SKIP_SPACE_CHECK=1 bash migrate-to-controller.sh   # salta il controllo dello spazio
```

**Sequenza (comune):** rileva la sorgente → **controlla lo spazio su disco** →
backup dei dati → estrae il tarball `arfea-controller` → configura `arfea.yml` (API
key generata, `update_url` e `releases_url` **sempre** valorizzati) → build + avvio
dello stack.

- **Spazio su disco:** prima di fermare qualunque servizio lo script stima per
  eccesso cosa scriverà (backup di `/opt/docker_store`, nel caso nativo la copia
  dei dati, il pacchetto addon di OpenHAB da ~1,8 GB fra download ed estrazione, le
  immagini Docker, un margine) e si ferma se non basta. Se la stima è troppo
  prudente: `MIGRATE_SKIP_SPACE_CHECK=1`.
- **OTA:** la centralina migrata resta sotto OTA. Se il controller pubblicato è più
  nuovo del tarball usato per la migrazione, al primo avvio si aggiorna da solo.
- **WebDAV:** la migrazione **non** imposta le credenziali WebDAV: finché non si
  compilano in `arfea.yml` il backup resta solo locale.

**In più per la sorgente DOCKER:** la versione di OpenHAB **resta quella che girava**
(immagine del container, del vecchio compose o, se il tag è mobile come `latest`,
quella scritta nell'userdata). La migrazione cambia la struttura, non la versione:
l'upgrade si fa dopo, dalla card *"Aggiornamento software"*, che prima fa il backup.

**In più per la sorgente NATIVA:**
1. **Installa Docker** se assente (repo apt Ubuntu/Debian). Se il daemon non parte
   senza riavvio, lo script esce chiedendo un **reboot + ri-esecuzione** (le cartelle
   native non vengono toccate, quindi riprende da capo senza danni). Se Docker c'è ma
   è il `docker.io` di Ubuntu, che non ha né `docker compose` né buildx, installa
   `docker-compose-v2` e `docker-buildx` (e aggiorna `docker.io`: i container già
   presenti ripartono) **prima** di fermare qualunque servizio.
2. Copia `conf`/`userdata`/`addons` in `/opt/docker_store/openhab/` con owner
   **9001:9001** (escludendo `cache`/`tmp`/`logs` e il pacchetto
   `openhab-addons-X.Y.Z.kar` della versione nativa: quello della versione giusta lo
   scarica il controller). **Le cartelle native NON vengono cancellate: restano come
   backup.**
3. **Servizi companion** (verificati sull'OS con `systemctl`/`pgrep`):
   - `habapp`, `mosquitto`, `samba` → **abilitati sul controller** + `stop`+`disable` nativo;
   - `frontail` → **solo `stop`+`disable`** (non più necessario, nessun servizio controller);
   - la config HABApp viene individuata (da `ExecStart --config` o path comuni) e copiata
     in `openhab/conf/habapp`.
4. **Porte seriali USB** (zwave/modbus): rilevate da `EXTRA_JAVA_OPTS` (solo le righe
   attive: il file del pacchetto ha un esempio commentato con `/dev/ttyS0`, che sulla
   C4 è la console seriale), dalle `things`/jsondb e dai nodi presenti. Una seriale già
   usata da un container che resta fuori dal controller (es. un vecchio
   `zwavejs2mqtt`) non viene mappata, e il container viene elencato. Mappate **1:1** nel container openhab (non
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

**Salto di versione (OpenHAB 3.x nativo → 5.x):** OpenHAB parte con l'immagine del
template e aggiorna da solo l'userdata al primo avvio (lo strumento di upgrade passa
per tutte le versioni intermedie). Prima di avviarlo lo script prepara la copia:

| Cosa | Perché |
|---|---|
| script Jython da `automation/jsr223/python` a `automation/jython`, librerie in `automation/jython/lib` | dal 4.2/5.x il Jython legge solo lì, e gli script rimasti in `jsr223` **non si caricano, senza un errore nel log** |
| JS Scripting aggiunto agli addon | lo vogliono `arfea_controller.js` e `arfea_system.js` e le trasformazioni `JS(...)`; il 3.x non ce l'ha |
| item doppioni di `arfea.items` tolti dal JSONDB | il vecchio HABApp ARFEA li creava via REST (`users_list`, `send_message`, `timeSlot`, ...) |
| `default = ...` tolto da Strategies nei `.persist` | dal 5.1 rende il file illeggibile (solo se ogni voce ha già le sue strategie, altrimenti lo segnala) |
| log di HABApp relativi, vecchie regole `system/arfea.py`, `system/time.py`, `tools/tools.py` messe da parte | percorsi dell'host inesistenti nel container (HABApp in loop); le regole le sostituiscono `arfea_system.js` e `aasystem/tools.py` |

Alla fine stampa cosa resta **da guardare a mano**:
- **regole UI in JavaScript**: dal 4.0 `application/javascript` è GraalJS, non più
  Nashorn. Tipico: `getStatus() == 'OFFLINE'` su un enum Java non è più vero, serve
  `getStatus().toString()`;
- **thing MQTT Home Assistant** creati prima del 4.3 (`mqtt:homeassistant_...`): dal 5.0
  cambiano gli ID dei canali e dal 5.1 il binding è a parte (lo installa l'upgrade).
  Si eliminano, si riapprovano dall'inbox (`homeassistant:device:...`) e si
  ricollegano gli item. Eliminare un thing **non** cancella i suoi collegamenti:
  restano nel JSONDB e puntano a canali che non esistono più, ed è da lì che si
  ricostruisce la mappa. Il canale nuovo si chiama `<objectid>#sensor` (o solo
  `<objectid>` per gli interruttori, es. `switch_1`), dove `objectid` è nella
  configurazione del canale vecchio; il `state_topic` va confrontato con quello della
  discovery retained. Su un impianto migrato: 29 item ricollegati così, tutti verificati;
- se il log dice *Graal JavaScript language not initialized*, riavviare il container
  openhab (JS Scripting installato a caldo).

**Un vecchio zwavejs2mqtt → zwave-js-ui del controller** (fatto su un impianto migrato):
fermare il vecchio container e togliergli il riavvio automatico
(`docker update --restart=no zwavejs2mqtt && docker stop zwavejs2mqtt`), copiare il suo
store (`/home/<utente>/store`) in `/opt/docker_store/zwave-js-ui` e nel `settings.json`
cambiare solo `zwave.port` → `/dev/zwave` e `mqtt.host` → `mosquitto` (il container sta
sulla rete Docker del controller). Restano le **chiavi di sicurezza S0/S2**, i nomi dei
nodi e il nome del client MQTT, quindi i topic e la discovery Home Assistant non
cambiano. Mai avviare zwave-js-ui con la config di default su una chiavetta già in
uso: senza chiavi re-intervista i nodi sicuri senza sicurezza.

Trappole viste sullo stesso impianto, prima di approvare i thing Home Assistant:
- **`gateway.ignoreLoc` di zwave-js-ui deve restare `false`** se i nomi dei nodi si
  ripetono (tre «allagamento», quattro «movimento» in stanze diverse). Salvando la
  pagina impostazioni della UI 11.x può diventare `true`: i topic perdono la stanza
  e nodi diversi scrivono sugli stessi topic e sulle stesse discovery.
- **Discovery vecchie nel database di mosquitto**: il broker migrato (o un periodo
  con `ignoreLoc`) si porta dietro discovery retained di nomi che il gateway non usa
  più. Il binding le unisce a quelle giuste nello stesso thing e i canali nascono
  doppi o con suffissi (`water_binary_sensor#sensor`). Prima di approvare:
  `mosquitto_sub --retained-only -t 'homeassistant/+/+/+/config'`, e ogni nodeid che
  il gateway non pubblica più si cancella con `mosquitto_pub -r -n -t <topic>`
  (salvandolo prima su file).
- Nel binding Home Assistant 5.2.1 modificare la configurazione di un thing, o
  disattivare il broker, può bloccare lo smontaggio degli handler (*Disposing handler
  … takes more than 5000ms* ogni 10 s): si sblocca solo riavviando il container
  openhab.

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

**Addon a bordo (installazione senza internet).** Di suo OpenHAB scarica il
binding dalla rete nel momento in cui lo si installa: su una centralina senza
linea l'installazione non riesce e l'impianto resta senza quel pezzo. Il
controller tiene percio' in `openhab/addons` il *kar* ufficiale con **tutti** gli
addon della versione in uso — l'equivalente del pacchetto `openhab-addons` delle
installazioni native. Con quello a bordo i binding si installano offline, dalla
UI di OpenHAB come sempre.

- **Quando lo scarica**: all'avvio del controller, alla creazione/recreate del
  container openhab e quando un aggiornamento cambia la versione di OpenHAB (il
  kar deve combaciare col runtime, altrimenti le feature non si risolvono): il
  pacchetto **segue da solo la versione di OpenHAB**. Scarica il nuovo e solo dopo
  toglie il vecchio. Il download e' in background: OpenHAB parte subito e Karaf
  carica il pacchetto a caldo appena compare nella cartella. Dal controller 1.8.4
  lo ricontrolla anche **ogni ora**: se il download era fallito (linea giù proprio
  durante l'aggiornamento), riprova senza aspettare il riavvio del controller.
- **Quanto pesa**: ~600 MB scaricati, ~1,2 GB a bordo (Karaf lo estrae in
  `openhab/userdata/tmp/kar`). Se il disco libero non basta il download non parte
  e il motivo finisce nei log: riempire l'eMMC fermerebbe tutto, OpenHAB compreso.
- **Effetto sull'avvio**: `cont-init.d/20-arfea-custom` ripulisce `userdata/tmp`
  ad ogni partenza, quindi Karaf riestrae il pacchetto ogni volta che il
  container parte. Il file `.kar` in `addons/` deve percio' restare dov'e' — e
  l'avvio di OpenHAB si allunga di qualche minuto.
- **Dove si vede**: card «Addon OpenHAB (offline)» nella Web UI (stato,
  avanzamento, pulsante per scaricarlo a mano se la centralina e' stata
  installata senza linea) e `GET /api/openhab/addons`.
- **Come si disattiva**: `controller.addons_kar_url: ""` in `arfea.yml`. Con un
  tag immagine senza numero di versione (`latest`, `snapshot`) il controller non
  scarica nulla e lo dice: non saprebbe quale kar prendere.
- Il kar **non entra nel backup**: sono MB ri-scaricabili, non dati
  dell'impianto. Dopo un ripristino lo riporta a bordo il controller.

### 6.2 Samba (opzionale, porte 139/445)
Condivisione file per accesso ai `conf/` di OpenHAB da rete. Non è più core: nel
template è spento (`enabled: false`), resta acceso sugli impianti migrati che lo
usavano. Probabilmente si toglie; per questo è l'unico servizio a `:latest`, fuori da
`releases.json`.

### 6.3 Mosquitto (auto-dipendenza, porta 1883)
Broker MQTT. Avviato **automaticamente** quando abiliti zwave-js-ui o zigbee2mqtt;
fermato quando nessuno dei due lo richiede più. Host del broker per i client: `mosquitto`.

### 6.4 HABApp (opzionale)
Engine di automazione Python. Tre funzioni attivabili **dalla Web UI** (card
*HABApp*), indipendenti fra loro:

| Funzione | Chiave in `arfea.yml` | Regole installate | Configurazione impianto |
|---|---|---|---|
| Termoregolazione | `thermo` | `rules/thermostats/` + `lib/thermostats/` | `params/thermo.yml` |
| Irrigazione | `irrigation` | `rules/irrigation/` + `lib/irrigation/` | `params/irrigation.yml` |
| Controllo carichi | `loads` | `rules/loads/` + `lib/loads/` | `params/loads.yml` |

**Come funziona.** I sorgenti arrivano con l'OTA del controller (in
`arfea-controller/habapp/<versione>/`, come lo skeleton OpenHAB): abilitare
HABApp **non richiede rete** e le regole sono sempre quelle della versione del
controller in esecuzione. Alla creazione del container il controller deploya in
`openhab/conf/habapp` **solo** le funzioni scelte, più la base comune
(`lib/system/`). Togliere una funzione ne rimuove le regole; le regole
specifiche cliente eventualmente presenti (`accessControl/`, `infraRed/`, ...)
non vengono toccate.

**Autenticazione (non è opzionale).** HABApp deve autenticarsi: openHAB concede
alle richieste anonime il solo ruolo `USER` (`implicitUserRole`, attivo di
default), mentre queste regole creano item e scrivono metadata ad ogni avvio —
endpoint riservati ad ADMIN, che risponderebbero `403`. Il controller conia da
solo un API token admin (console Karaf) e lo scrive in `config.yml` nel campo
`user` (con `password` vuota, come vuole HABApp). Avviene **una volta sola**: se
in `config.yml` c'è già un token valido, il file non viene più toccato — quindi
un `config.yml` personalizzato (location, mqtt) sopravvive agli aggiornamenti.

**Configurazione degli impianti.** Quali termostati, quali zone, quali carichi
si definisce nei `params/*.yml`, editabili dalla Web UI (validazione YAML +
riavvio automatico di HABApp). Non serve più mettere le mani nei file via Samba.
Il controller non li sovrascrive mai: se manca, ne crea uno vuoto (`{}`).

**Log.** `logging.yml` è deployato solo se manca, con livello **WARN**. Se lo
alzi a DEBUG per diagnosticare un impianto, un riavvio del container non te lo
riporta a WARN.

> Le **fasce giornaliere** (`timeSlots`/`timeSlot`/`isHoliday`/`holidayName`)
> **non** sono in HABApp: vivono in `conf/automation/js/arfea_system.js` dello
> skeleton, quindi funzionano su ogni impianto anche senza HABApp. Il vecchio
> `rules/aasystem/time.py` che le gestiva, rimasto sugli impianti installati prima
> della 1.6.0, girava insieme al JS (su un impianto andava in errore due volte al
> minuto). Dal controller **1.8.8** lo si toglie da solo, all'avvio e nel
> provisioning, se `arfea_system.js` è a bordo: diventa `time.py.obsoleto` nella
> stessa cartella, che HABApp non carica.

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
| `html/semantic/<tag>.svg` | `conf/html/semantic/` (URL `/static/semantic/<tag>.svg`) | Sfondi delle card della Home, uno per tag semantico |

**Sfondi delle card della Home (dal controller 1.8.7).** La Home della Main UI
genera da sola una card per ogni Location e per ogni tipo di Equipment e di
Property. Di suo le colora con un colore fisso; il controller assegna a ognuna
l'immagine del suo tag semantico (sorgenti in `openhab-semantic-icons/`, 267
immagini da 1200×400, una per ogni tag di OpenHAB). Lo fa scrivendo
`backgroundImage` nella pagina `ui:page/home`, all'avvio e poi ogni 10 minuti,
così le Location nuove prendono lo sfondo senza fare niente.
- Un tag senza immagine (un tag personalizzato) prende quella del tag padre:
  una `Location_Indoor_Room_Taverna` avrebbe lo sfondo di `Room`.
- Una card con un'immagine o un colore scelti a mano (Impostazioni → Pagine →
  Home) **non viene toccata**: vale la scelta dell'utente. Per togliere lo
  sfondo a una card basta darle un colore.
- Se la pagina Home non esiste ancora il controller la crea con le impostazioni
  di default della UI. Dove si vede: Impostazioni → Pagine → Home, oppure
  `GET /rest/ui/components/ui:page/home` (i `backgroundImage` sotto `/static/semantic/`).
  Provato su un impianto: 26 card (7 Location, 7 Equipment, 12 Property).

> Ownership: ogni file sotto `/opt/docker_store/openhab/` DEVE restare
> `9001:9001` (UID/GID del container OpenHAB). Le cartelle che un aggiornamento
> porta per la prima volta nascevano di root (con la 1.8.7 `conf/html/semantic`):
> dal controller **1.8.8** nascono 9001, e all'avvio il controller riporta a
> `9001:9001` tutto ciò che in `openhab/conf` è di root.

### 6.11 Rete: LAN, wifi e access point

Dalla Web UI (schede **Rete**, **Wifi**, **Access point di emergenza**) si
gestisce la rete della centralina. Il controller non la configura da sé: la fa
configurare a **NetworkManager** sull'host (via `nsenter` + `nmcli`), quindi la
configurazione resta valida anche a controller spento.

**Prerequisito: rete gestita da NetworkManager.** Armbian e Ubuntu Server
nascono con netplan + systemd-networkd, che non sanno cercare reti wifi né fare
da access point. Il passaggio si fa una volta per centralina.

**Dalla Web UI** (controller 1.8.4): se NetworkManager manca, la scheda **Rete**
lo dice e propone *"Installa e attiva ora"* o *"Installa, attiva al prossimo
riavvio"*. Il controller lancia sull'host lo stesso script di sotto, in un'unità
systemd transitoria (`arfea-network-install`), e la pagina mostra i passi fino
all'esito. Con *"al prossimo riavvio"* alla fine propone *"Riavvia ora"*. Serve
internet, per scaricare i pacchetti. Durante il passaggio la pagina può non
rispondere per qualche secondo: l'IP resta lo stesso.

**Da riga di comando**, se la Web UI non è raggiungibile:

```bash
# sulla board (le centraline preparate con lo script di setup ARFEA lo fanno già in fase 2)
sudo /opt/docker_store/arfea-controller/script/arfea-network-nm.sh            # subito, con rollback
sudo /opt/docker_store/arfea-controller/script/arfea-network-nm.sh --boot     # al prossimo riavvio
sudo /opt/docker_store/arfea-controller/script/arfea-network-nm.sh --rollback # torna a netplan/networkd
```

Lo script riprende la configurazione in uso (stesso MAC e stesso client-id DHCP,
quindi **stesso IP**, oppure lo stesso IP statico), salva i file netplan in
`/var/backups/arfea-network/<data>/` e spegne systemd-networkd. Il passaggio
gira in un'unità systemd: se la sessione SSH cade, arriva comunque in fondo; se
entro 90 s il gateway non risponde, rimette tutto com'era da solo. Log in
`/var/log/arfea-network.log`. Senza questo passaggio le schede della Web UI
mostrano "NetworkManager non installato" e il resto del controller funziona
come prima.

**LAN (DHCP / IP statico).** Dopo *Applica* hai **2 minuti per confermare**. Se
non confermi (per esempio perché con i nuovi valori la centralina non è più
raggiungibile) NetworkManager rimette **da solo** la configurazione di prima
(checkpoint con rollback automatico). Se cambi indirizzo, apri la Web UI sul
nuovo IP e conferma da lì: la pagina te lo propone.

**Wifi.** *Cerca reti*, scegli, password, *Collega*. Se la connessione fallisce
(password errata, rete non trovata) la rete di prima resta com'era. *Dimentica*
scollega e cancella la rete configurata. Con **LAN e wifi attivi insieme vince
la LAN** (metrica 100 contro 600): il traffico passa dal cavo e il wifi resta di
riserva. Se il cavo si stacca, il traffico passa sul wifi da solo.

**Access point di emergenza.** Serve a raggiungere la centralina quando non la
si trova in rete (cliente che cambia router, password wifi cambiata, centralina
nuova senza cavo). Il watchdog del controller:

| Situazione | Cosa fa |
|---|---|
| Rete wifi configurata ma assente da `grace_seconds` (default 2 min) | Accende l'AP |
| Nessuna rete wifi configurata **e** LAN scollegata da `grace_seconds` | Accende l'AP |
| AP acceso, **nessuno collegato**, ogni `retry_seconds` (default 5 min) | Spegne l'AP ~10-20 s, cerca la rete: se c'è si collega e l'AP resta spento, altrimenti lo riaccende |
| AP acceso con **qualcuno collegato** | Non tocca nulla (sta configurando) |
| AP acceso, nessuna rete wifi configurata, LAN tornata | Spegne l'AP |

Per configurare una rete nuova tramite l'AP:
1. dal telefono/PC collegati alla rete `ARFEA-XXXX` (nome e password nella Web
   UI, sezione *Access point*, e in `/root/arfea-credentials.txt`);
2. apri `http://192.168.200.1:8888` e inserisci la API key;
3. *Wifi* → *Cerca reti* (con l'AP acceso l'elenco è quello dell'ultima
   scansione; *Aggiorna* spegne l'AP per ~15 s e poi ti ricolleghi), scegli la
   rete, password, *Collega*;
4. l'AP si spegne e il telefono si scollega: se la connessione riesce la
   centralina è sulla nuova rete (IP dal router); se fallisce l'AP si riaccende
   in pochi secondi e nella scheda *Wifi* trovi il motivo.

L'AP serve **solo** a raggiungere la centralina: non instrada verso la LAN né
verso internet. Si può anche accendere a mano (*Accendi ora*: resta su almeno
10 minuti) o disattivare del tutto (*Abilitato* off).

**Hardware.** La scheda wifi deve supportare la modalità AP (`iw list` →
*Supported interface modes* contiene `AP`). Provata la chiavetta USB Realtek
RTL8821CU (driver `rtw88_8821cu`) su ODROID-C4/Armbian trixie. Una sola radio
non fa client e AP insieme: per questo l'AP si spegne per qualche secondo quando
cerca la rete.

**Un AP fatto a mano sull'host (create_ap / linux-wifi-hotspot).** Alcuni
impianti vecchi hanno un AP permanente per i propri dispositivi (per esempio su
`wlan0`, servizio `create_ap` con `/etc/create_ap.conf`).
Per non litigare con NetworkManager la scheda è esclusa (`unmanaged-devices=
interface-name:wlan0` in `NetworkManager.conf`), quindi il controller non la può
usare: niente wifi client né AP di emergenza, e conviene spegnerlo (*Abilitato*
off) per non avere tentativi falliti a ogni caduta della LAN. Trappola: se in
NetworkManager la radio wifi è spenta (`nmcli radio wifi` → `disabled`,
`WirelessEnabled=false` in `/var/lib/NetworkManager/NetworkManager.state`), a ogni
avvio NM rimette il blocco rfkill anche sulla scheda esclusa e create_ap esce con
*Operation not possible due to RF-kill*. Si risolve con `sudo nmcli radio wifi on`
(resta salvato) e `sudo systemctl enable --now create_ap`.

---

## 7. Interfaccia web e API REST

### Web UI
`http://<IP_CENTRALINA>:8888`. Richiede la API key, che resta salvata nel browser
(«Esci» la dimentica). Raggiungibile solo da LAN o VPN.

Dal controller 1.8.6 è divisa in **cinque sezioni**. Sul telefono si scelgono dalla
barra in basso, su uno schermo grande dalla barra a sinistra, con le card su due
colonne. L'indirizzo ricorda la sezione (`…:8888/#rete`), quindi si può aprire
direttamente.

| Sezione | Cosa c'è |
|---|---|
| **Stato** | *Da guardare*: gli avvisi che chiedono attenzione (servizio fermo, aggiornamento disponibile o in corso, scelta in sospeso, backup non riuscito, rete da sistemare, HABApp senza token, porta seriale mappata con un nome che può cambiare al riavvio); un tocco porta alla sezione giusta. Poi servizi con riavvio, dati della centralina e IP, riavvio del sistema operativo. |
| **Impianto** | HABApp (funzioni attive), configurazione dell'impianto (`params/*.yml`), porte seriali dei dispositivi, telefono di emergenza. |
| **Rete** | LAN, wifi, access point di emergenza; installazione di NetworkManager se manca. |
| **Aggiornamenti** | Versioni dei software (release certificate, con avanzamento e scelta *continua senza backup / ferma*), controller, pacchetto addon offline. |
| **Backup** | Backup manuale e ripristino (elenco aggiornato all'apertura della sezione). |

Un pallino sulla sezione (arancione = da guardare, rosso = problema) segnala dove
c'è qualcosa in sospeso anche senza aprirla.

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
| GET | `/api/openhab/addons` | Stato del pacchetto addon offline (+ avanzamento download) |
| POST | `/api/openhab/addons/download?force=` | Scarica il pacchetto addon (background) |
| POST | `/api/backup/run` · GET `/status` · `/list` | Backup |
| POST | `/api/backup/restore?backup_name=...` | Ripristino |
| GET/PUT | `/api/linphone/config` · GET `/status` · POST `/call?number=&message=` | Emergenza |
| GET | `/api/habapp/status` | Funzioni HABApp, versione sorgenti, stato token |
| PUT | `/api/habapp/functions` | Sceglie le funzioni attive (ricrea HABApp) |
| GET/PUT | `/api/habapp/params/{thermo\|irrigation\|loads}` | Configurazione impianto (YAML) |
| GET | `/api/network/status` | LAN, wifi, access point, watchdog, modifica LAN in attesa |
| PUT | `/api/network/lan` · POST `/lan/confirm` · `/lan/rollback` | DHCP/IP statico (da confermare entro 2 min) |
| GET | `/api/network/wifi/scan?force=` | Reti visibili (con AP acceso: ultima scansione) |
| POST/DELETE | `/api/network/wifi/connect` · `/api/network/wifi` | Collega / dimentica la rete wifi |
| PUT | `/api/network/ap` · POST `/ap/start` · `/ap/stop` | Configura / accendi / spegni l'access point |

---

## 8. Backup e ripristino

**Backup** (UI "Esegui Backup" o `POST /api/backup/run`):
0. **Controlla lo spazio su disco** prima di fermare qualunque cosa (dal controller
   1.8.4): stima l'archivio per eccesso e, se non c'è posto, **toglie i backup
   locali vecchi**, dal più vecchio. Se non basta nemmeno così si ferma lì, con
   l'impianto acceso, e lo dice.
1. Ferma tutti i container (tranne il controller)
2. Crea un `tar.gz` di `/opt/docker_store`
3. Riavvia i container che erano attivi
4. Carica su WebDAV (se configurato)

Sulla centralina resta **solo l'ultimo backup completo**: lo storico sta su WebDAV,
e su una eMMC da 16 GB ogni archivio in più toglie spazio a immagini e
aggiornamenti. Se il caricamento su WebDAV non riesce, il backup lo segnala ma
**l'archivio locale resta valido**. Un archivio rimasto a metà, per esempio col
disco pieno, viene cancellato.

**Prima di un aggiornamento di versione** il backup è il punto di ripristino:
- caricamento su WebDAV fallito, archivio locale integro → l'aggiornamento
  **continua** e alla fine lo dice;
- spazio insufficiente anche togliendo i backup vecchi → l'aggiornamento si
  **ferma e chiede** (widget di OpenHAB o Web UI): *"Continua senza backup"*, a
  proprio rischio, oppure *"Ferma l'aggiornamento"*. Senza risposta entro 30
  minuti si ferma da solo.

**WebDAV di Nextcloud:** `webdav_url` è l'indirizzo WebDAV della condivisione,
`https://<host>/public.php/dav/files/<token>`, con utente `<token>` e la password
della condivisione. Il link `https://<host>/s/<token>` è la pagina web: un
caricamento lì risponde **401**. Fino al controller 1.8.3 era il default degli
installer, e con quello nessun backup arrivava su WebDAV. Dalla 1.8.4 il
controller converte da solo il link nell'indirizzo WebDAV all'avvio e lo salva.

L'impianto resta fermo solo per l'archiviazione (passi 1-2): l'upload avviene a
container riavviati, perche' l'archivio su disco e' gia' completo e coerente e
mezzo giga su una linea domestica sono decine di minuti. L'upload ha un tetto di
30 minuti oltre il quale viene interrotto e il backup segnalato fallito: senza,
un trasferimento che avanza a singhiozzo non fa scattare nessun timeout e resta
appeso, bloccando anche l'aggiornamento di versione che lo aspetta.

**Ripristino** (UI o `POST /api/backup/restore?backup_name=<FILE>`):
1. Ferma tutti i container
2. Estrae l'archivio in `/opt/docker_store`
3. Ricarica `arfea.yml`
4. Riavvia i servizi abilitati

Se il file non è locale ma il WebDAV è configurato, viene scaricato prima del
ripristino. La cartella `backups` resta sempre fuori dall'archivio, e così ogni
percorso in `exclude_paths`, a qualsiasi profondità. Fino al controller 1.8.4
valeva solo per le cartelle di primo livello di `/opt/docker_store`, quindi ogni
backup conteneva quello precedente.

Fuori dall'archivio stanno il pacchetto addon di OpenHAB
(`openhab/addons/*.kar`, vedi [6.1](#61-openhab-core-porta-8080-network_mode-host)), le
sue copie estratte da Karaf (`openhab/userdata/kar`, `openhab/userdata/tmp/kar`) e
la cache di OpenHAB (`userdata/cache`, `userdata/tmp`, che il container svuota
comunque a ogni avvio). Con le copie dentro, un backup era passato da ~460 MB a
2,15 GB e aveva riempito il disco. Il pacchetto addon è fatto di
~600 MB ri-scaricabili in qualsiasi momento, non dati dell'impianto. Tenerli
dentro raddoppierebbe l'archivio e il tempo di trasmissione, mandando l'upload
oltre il tetto dei 30 minuti — cioe' facendo fallire i backup su linea lenta. Al
ripristino lo riporta a bordo il controller al primo avvio con la linea attiva:
fino a quel momento la centralina e' come una senza pacchetto, gli addon si
installano solo online.

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
- [ ] `update_url` punta a un server **fidato** (il tarball viene estratto ed
      eseguito) e non è vuoto: senza, la centralina non riceve più l'OTA
- [ ] Access point di emergenza: password annotata (o cambiata) e rete in
      `192.168.0.0/16`, così dall'AP la API key resta obbligatoria (10.x e
      172.16.x sono reti fidate, senza key: il validatore le rifiuta)

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

0. **È il dispositivo giusto?** Se il mapping usa `/dev/ttyUSB0` sull'host, dopo un
   riavvio può essere diventato un altro adattatore: timeout su tutte le letture
   Modbus e, se c'è una chiavetta Z-Wave, zwave-js-ui con `Timeout while waiting
   for an ACK from the controller`. Confronta:
   ```bash
   ls -l /dev/serial/by-id/                                  # quale tty è ogni adattatore, oggi
   docker inspect openhab zwave-js-ui --format '{{.Name}} {{json .HostConfig.Devices}}'
   ```
   Rimedio: mappare per by-id ([§3](#device-seriali-importante)); dal controller 1.8.8
   la Web UI lo segnala da sola.
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
