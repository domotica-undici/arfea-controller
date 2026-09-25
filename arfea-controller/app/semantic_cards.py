"""Sfondo delle card del modello semantico nella Main UI (Redmine #230).

La Home della Main UI genera da sola una card per ogni Location, per ogni tipo
di Equipment e per ogni Property. Lo sfondo di una card non ha un default per
tag: la UI (openhab-webui 5.2, components/cards/model-card.vue) usa
``config.backgroundImage`` della card se c'e', altrimenti un colore fisso. La
config delle card sta nella pagina ``ui:page`` con uid ``home``:

    slots.locations[0]  = {component: oh-locations-tab,  slots: {<nome item>: [card]}}
    slots.equipment[0]  = {component: oh-equipment-tab,  slots: {<tag>: [card]}}
    slots.properties[0] = {component: oh-properties-tab, slots: {<tag>: [card]}}

Le immagini (una per tag, 1200x400) le porta lo skeleton in
``conf/html/semantic/<tag>.svg``, che OpenHAB serve come
``/static/semantic/<tag>.svg``. Qui si calcola la pagina con lo sfondo di ogni
card; il giro REST lo fa main.py.

Le card personalizzate non si toccano: se l'utente ha messo un'immagine sua o un
colore (``backgroundColor``), resta la sua scelta. Si aggiornano solo gli
sfondi che hanno il nostro prefisso.
"""

from __future__ import annotations

import copy
from pathlib import Path

URL_PREFIX = "/static/semantic/"

# Chiave del tab nella pagina -> (componente del tab, componente della card)
_TABS = {
    "locations": ("oh-locations-tab", "oh-location-card"),
    "equipment": ("oh-equipment-tab", "oh-equipment-card"),
    "properties": ("oh-properties-tab", "oh-property-card"),
}


def available_tags(images_dir: Path) -> set[str]:
    """Tag (minuscoli) per cui c'e' un'immagine a bordo."""
    if not images_dir.is_dir():
        return set()
    return {p.stem.lower() for p in images_dir.glob("*.svg")}


def _image_for(tags: list[str], available: set[str]) -> str:
    """URL dell'immagine del tag piu' specifico che ne ha una.

    ``tags`` va dal piu' specifico al piu' generico (Bathroom, Room, Indoor):
    un tag personalizzato senza immagine prende quella del padre."""
    for tag in tags:
        if tag.lower() in available:
            return f"{URL_PREFIX}{tag.lower()}.svg"
    return ""


def _semantic_value(item: dict) -> str:
    return ((item.get("metadata") or {}).get("semantics") or {}).get("value") or ""


def _relates_to(item: dict) -> str:
    sem = (item.get("metadata") or {}).get("semantics") or {}
    return (sem.get("config") or {}).get("relatesTo") or ""


def wanted_backgrounds(items: list[dict], available: set[str]) -> dict[str, dict[str, str]]:
    """Per ogni tab, chiave della card -> URL dello sfondo.

    Le chiavi seguono quelle della UI (useModelStore.ts): nome dell'item per le
    Location, ultimo segmento del tag per gli Equipment, primo segmento della
    Property di ``relatesTo`` per le Property."""
    wanted: dict[str, dict[str, str]] = {tab: {} for tab in _TABS}
    for item in items:
        value = _semantic_value(item)
        parts = value.split("_")
        if parts[0] == "Location" and len(parts) > 1:
            url = _image_for(parts[:0:-1], available)
            if url:
                wanted["locations"][item["name"]] = url
        elif parts[0] == "Equipment" and len(parts) > 1:
            url = _image_for(parts[:0:-1], available)
            if url:
                wanted["equipment"].setdefault(parts[-1], url)
        relates = _relates_to(item).split("_")
        if len(relates) > 1 and relates[1]:
            url = _image_for([relates[1]], available)
            if url:
                wanted["properties"].setdefault(relates[1], url)
    return wanted


def new_home_page() -> dict:
    """La pagina Home che la UI userebbe se non esistesse (home-edit.vue)."""
    return {
        "uid": "home",
        "component": "oh-home-page",
        "config": {"label": "Home Page"},
        "slots": {tab: [{"component": comp, "config": {}, "slots": {}}]
                  for tab, (comp, _card) in _TABS.items()},
    }


def apply_backgrounds(page: dict, wanted: dict[str, dict[str, str]]) -> tuple[dict, int]:
    """Ritorna (pagina aggiornata, numero di card cambiate). La pagina in
    ingresso non viene modificata."""
    page = copy.deepcopy(page)
    slots = page.setdefault("slots", {})
    changes = 0

    for tab, (tab_component, card_component) in _TABS.items():
        tab_list = slots.get(tab)
        if not isinstance(tab_list, list) or not tab_list or not isinstance(tab_list[0], dict):
            tab_list = [{}]
            slots[tab] = tab_list
        tab_comp = tab_list[0]
        tab_comp.setdefault("component", tab_component)
        tab_comp.setdefault("config", {})
        cards = tab_comp.get("slots")
        if not isinstance(cards, dict):
            cards = {}
            tab_comp["slots"] = cards

        for key, url in wanted[tab].items():
            card_list = cards.get(key)
            if not isinstance(card_list, list) or not card_list:
                cards[key] = [{"component": card_component, "config": {"backgroundImage": url}}]
                changes += 1
                continue
            card = card_list[0]
            config = card.get("config")
            if not isinstance(config, dict):
                config = {}
                card["config"] = config
            if config.get("backgroundColor"):
                continue  # l'utente ha scelto un colore
            current = config.get("backgroundImage") or ""
            if current and not current.startswith(URL_PREFIX):
                continue  # immagine scelta dall'utente
            if current != url:
                config["backgroundImage"] = url
                changes += 1

        # Card nostre di elementi che non esistono piu': solo quelle che non
        # hanno altro che il nostro sfondo, le altre sono dell'utente.
        for key in list(cards):
            if key in wanted[tab]:
                continue
            card_list = cards[key]
            if (isinstance(card_list, list) and len(card_list) == 1
                    and isinstance(card_list[0], dict)
                    and card_list[0].get("component") == card_component
                    and not card_list[0].get("slots")
                    and set((card_list[0].get("config") or {})) == {"backgroundImage"}
                    and str(card_list[0]["config"]["backgroundImage"]).startswith(URL_PREFIX)):
                del cards[key]
                changes += 1

    return page, changes
