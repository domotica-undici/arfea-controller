"""Link a tempo per far scaricare un file al browser (Redmine #302).

Dalla LAN l'API vuole la chiave nell'header X-API-Key, e un link aperto dal
browser non puo' portare header. La chiave in un URL no: resterebbe nei log e
nella cronologia. La pagina chiede quindi, con la chiave, un token che vale
pochi minuti e per un solo file, e il browser scarica con quello.
"""
from __future__ import annotations

import secrets
import threading
import time

# Serve solo ad avviare il download: la richiesta parte subito dopo il rilascio.
# Non e' monouso, perche' il browser puo' ripetere la richiesta (riprova, «salva
# con nome»).
DEFAULT_TTL = 300


class DownloadLinks:
    def __init__(self, ttl: int = DEFAULT_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}   # token -> (nome file, scadenza)

    def issue(self, name: str) -> str:
        """Rilascia un token per scaricare ``name``."""
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with self._lock:
            for t in [t for t, (_, exp) in self._tokens.items() if exp <= now]:
                del self._tokens[t]
            self._tokens[token] = (name, now + self.ttl)
        return token

    def valid(self, token: str, name: str) -> bool:
        """Vero se ``token`` e' stato rilasciato per ``name`` e non e' scaduto."""
        if not token:
            return False
        with self._lock:
            entry = self._tokens.get(token)
        return bool(entry) and entry[0] == name and entry[1] > time.monotonic()
