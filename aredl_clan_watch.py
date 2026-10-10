"""
Surveille les records d'un clan AREDL (liste classique + platformer) et envoie un
message avec ping dans un salon Discord quand un nouveau record apparait.

Pas besoin de creer un bot Discord : le script utilise un webhook de salon.

Installation :
    pip install requests

Configuration (variables d'environnement, ou modifie les valeurs ci-dessous) :
    WEBHOOK_URL   URL du webhook du salon
                  (Parametres du salon > Integrations > Webhooks > Nouveau webhook)
    PING          Ce qui est ping : "<@&ID_DU_ROLE>" pour un role, "" pour personne
    CLAN_ID       Identifiant du clan (deja rempli pour TAF)
    CHECK_EVERY   Delai entre deux verifications, en secondes (600 par defaut)

Lancement :
    python aredl_clan_watch.py            # tourne en continu
    python aredl_clan_watch.py --once     # une seule verification puis stop
    DRY_RUN=1 python aredl_clan_watch.py --once   # affiche au lieu d'envoyer
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------- CONFIG ----

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "COLLE_TON_WEBHOOK_ICI")
PING = os.getenv("PING", "")  # ex. "<@&123456789012345678>"
CLAN_ID = os.getenv("CLAN_ID", "7860e081-b5bf-4546-8a13-85b7f3121c50")

API_BASE = "https://api.aredl.net/v2/api"
SOURCES = {
    "Classic": f"{API_BASE}/aredl/clan/{CLAN_ID}",
    "Platformer": f"{API_BASE}/arepl/clan/{CLAN_ID}",
}

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY", "600"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_records.json"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
HEADERS = {"User-Agent": "aredl-clan-watch/1.1"}

# ------------------------------------------------------------------ CODE ----


def fetch_records(url):
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    records = response.json().get("records")
    if not isinstance(records, list):
        raise ValueError("Reponse inattendue : pas de liste 'records'.")
    return records


def describe(label, record):
    """Construit le texte du message pour un record."""
    user = record.get("submitted_by") or {}
    level = record.get("level") or {}
    player = user.get("global_name") or user.get("username") or "Un membre"
    name = level.get("name") or "un niveau"

    details = []
    if level.get("position"):
        details.append(f"#{level['position']}")
    if isinstance(level.get("points"), (int, float)):
        details.append(f"{level['points'] / 10:.1f} pts")  # l'API donne les points x10
    details.append(label)
    if record.get("mobile"):
        details.append("mobile")
    if record.get("is_verification"):
        details.append("verification")

    discord_id = str(user.get("discord_id") or "")
    who = f"<@{discord_id}>" if discord_id.isdigit() else f"**{player}**"
    text = f"{who} s'est fait accept son **{name}** ({' | '.join(details)})"
    video = record.get("video_url")
    if video and not record.get("hide_video"):
        text += f"\n{video}"
    return text


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {label: set(ids) for label, ids in data.items()}
        except (json.JSONDecodeError, OSError, AttributeError):
            print("Fichier d'etat illisible, il sera recree.", file=sys.stderr)
    return {}


def save_state(state):
    data = {label: sorted(ids) for label, ids in state.items()}
    STATE_FILE.write_text(json.dumps(data), encoding="utf-8")


def post_to_discord(content):
    if DRY_RUN:
        print("[SIMULATION] Message qui serait envoye :\n" + content + "\n")
        return
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles", "users", "everyone"]},
    }
    for _ in range(2):
        response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if response.status_code == 429:  # limite de debit : on attend puis on reessaie
            time.sleep(float(response.json().get("retry_after", 2)) + 0.5)
            continue
        response.raise_for_status()
        return
    raise RuntimeError("Discord a refuse l'envoi (limite de debit).")


def check_source(label, url, state):
    records = fetch_records(url)
    current = {r["id"]: r for r in records if r.get("id")}

    if label not in state:
        # Premier passage : on memorise l'existant sans rien envoyer.
        state[label] = set(current)
        save_state(state)
        print(f"[{label}] Premier passage : {len(current)} records memorises, rien envoye.")
        return

    new_ids = [rid for rid in current if rid not in state[label]]
    # Les plus anciens d'abord, pour que le salon reste dans l'ordre chronologique.
    new_ids.sort(key=lambda rid: current[rid].get("achieved_at") or "")
    for rid in new_ids:
        content = f"{PING} Nouveau record dans le clan !\n{describe(label, current[rid])}"
        post_to_discord(content.strip())
        state[label].add(rid)
        save_state(state)  # sauvegarde apres chaque envoi pour ne jamais doubler
        time.sleep(1)
    if new_ids:
        print(f"[{label}] {len(new_ids)} nouveau(x) record(s) annonce(s).")


def check_all(state):
    for label, url in SOURCES.items():
        try:
            check_source(label, url, state)
        except Exception as error:  # une erreur sur une liste ne bloque pas l'autre
            print(f"[{label}] Erreur pendant la verification : {error}", file=sys.stderr)


def main():
    if not DRY_RUN and "COLLE_" in WEBHOOK_URL:
        sys.exit("Renseigne WEBHOOK_URL (ou lance avec DRY_RUN=1 pour tester).")

    state = load_state()
    if "--once" in sys.argv:
        check_all(state)
        return
    while True:
        check_all(state)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
