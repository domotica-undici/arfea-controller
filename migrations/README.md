# Migrazioni di versione (release certificate)

Ogni sottocartella prende il nome di una `version` del manifest `releases.json`
e contiene gli script eseguiti dal controller durante l'apply di quella release:

- `pre.sh`  — eseguito PRIMA del pull/recreate (es. patch a items/regole con
  sintassi cambiata). Se esce con codice ≠ 0, lo step viene annullato (rollback).
- `post.sh` — eseguito DOPO che i nuovi container sono operativi (es. cleanup,
  fix che richiedono il nuovo container già avviato).

## Ambiente disponibile agli script

Il controller li lancia con `bash` e queste variabili:

| Variabile      | Significato                                    |
|----------------|------------------------------------------------|
| `DATA_PATH`    | radice dati persistenti (es. `/opt/docker_store`) |
| `OH_CONF`      | cartella conf di OpenHAB (`$DATA_PATH/openhab/conf`) |
| `FROM_VERSION` | versione di partenza dello step                |
| `TO_VERSION`   | versione di destinazione dello step            |

## Regole importanti

- **Ownership**: ogni file creato/modificato sotto `$DATA_PATH/openhab` DEVE
  restare di proprietà `9001:9001` (UID/GID del container OpenHAB). Lo script è
  responsabile del `chown`.
- **Idempotenza**: gli script devono poter girare più volte senza danni.
- **Niente segreti**: non scrivere credenziali; non toccare `arfea.yml`
  (i tag immagine li gestisce il controller).
- Queste cartelle vengono impacchettate nel tarball OTA e finiscono in
  `$DATA_PATH/arfea-controller/migrations/`.
