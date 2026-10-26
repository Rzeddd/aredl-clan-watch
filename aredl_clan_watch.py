"""
Surveille les records d'un clan AREDL (liste classique + platformer) et envoie un
message avec ping dans un salon Discord quand un nouveau record apparait.

Pas besoin de creer un bot Discord : le script utilise un webhook de salon.

Important : l'API ne liste qu'UNE entree par niveau pour le clan, avec un compteur
(completion_count). Quand un deuxieme membre complete un niveau deja fait par le clan,
seul le compteur augmente. Le script detecte cette hausse, puis demande la liste
complete des victoires du clan sur ce niveau pour annoncer la nouvelle.

Installation :
    pip install requests

Configuration (variables d'environnement, ou modifie les valeurs ci-dessous) :
    WEBHOOK_URL   URL du webhook du salon
    PING          Ce qui est ping : "<@&ID_DU_ROLE>" pour un role, "" pour personne
    CLAN_ID       Identifiant du clan (deja rempli pour TAF)
    CHECK_EVERY   Delai entre deux verifications, en secondes (600 par defaut)

Lancement :
    python aredl_clan_watch.py            # tourne en continu
    python aredl_clan_watch.py --once     # une seule verification puis stop
    DRY_RUN=1 python aredl_clan_watch.py --once   # affiche au lieu d'envoyer

Envoyer un message libre dans le salon (sans verifier les records) :
    python aredl_clan_watch.py --say "Salut tout le monde !"
    python aredl_clan_watch.py --say "Ligne 1\\nLigne 2"     # \\n = retour a la ligne
    python aredl_clan_watch.py --say "Reunion ce soir" --ping   # ajoute le ping PING
Depuis GitHub : onglet Actions > "Envoyer un message" > Run workflow.
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
SOURCES = {"Classic": "aredl", "Platformer": "arepl"}

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY", "600"))
STATE_FILE = Path(os.getenv("STATE_FILE", "seen_records.json"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
HEADERS = {"User-Agent": "aredl-clan-watch/1.2"}

# ------------------------------------------------------------------ CODE ----


def api_get(path):
    response = requests.get(f"{API_BASE}/{path}", headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.json()


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
    """Etat = pour chaque liste : ids de records vus + compteur de victoires par niveau."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state = {}
        for label, value in raw.items():
            if isinstance(value, list):  # ancien format : simple liste d'identifiants
                state[label] = {"ids": set(value), "counts": None}
            else:
                state[label] = {
                    "ids": set(value.get("ids", [])),
                    "counts": dict(value.get("counts", {})),
                }
        return state
    except (json.JSONDecodeError, OSError, AttributeError):
        print("Fichier d'etat illisible, il sera recree.", file=sys.stderr)
        return {}


def save_state(state):
    data = {
        label: {"ids": sorted(s["ids"]), "counts": s["counts"] or {}}
        for label, s in state.items()
    }
    STATE_FILE.write_text(json.dumps(data), encoding="utf-8")


def post_to_discord(content, mentions=("roles", "users", "everyone")):
    if DRY_RUN:
        print("[SIMULATION] Message qui serait envoye :\n" + content + "\n")
        return
    payload = {
        "content": content,
        "allowed_mentions": {"parse": list(mentions)},
    }
    for _ in range(2):
        response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if response.status_code == 429:  # limite de debit : on attend puis on reessaie
            time.sleep(float(response.json().get("retry_after", 2)) + 0.5)
            continue
        response.raise_for_status()
        return
    raise RuntimeError("Discord a refuse l'envoi (limite de debit).")


def check_source(label, kind, state):
    data = api_get(f"{kind}/clan/{CLAN_ID}")
    entries = [
        e for e in data.get("records", [])
        if e.get("id") and (e.get("level") or {}).get("id")
    ]

    if label not in state:
        # Premier passage : on memorise l'existant sans rien envoyer.
        state[label] = {
            "ids": {e["id"] for e in entries},
            "counts": {e["level"]["id"]: e.get("completion_count") or 1 for e in entries},
        }
        save_state(state)
        print(f"[{label}] Premier passage : {len(entries)} niveaux memorises, rien envoye.")
        return

    st = state[label]
    if st["counts"] is None:
        # Migration depuis l'ancien format : on memorise le compteur de tous les niveaux
        # existants, sans rien annoncer. Seule exception : une entree inconnue avec un
        # seul record est un vrai nouveau niveau, elle sera annoncee normalement.
        st["counts"] = {
            e["level"]["id"]: e.get("completion_count") or 1
            for e in entries
            if e["id"] in st["ids"] or (e.get("completion_count") or 1) > 1
        }
        save_state(state)

    pending = []         # records a annoncer : (date, record)
    count_updates = {}   # niveau -> nouveau compteur, applique une fois les envois faits

    for entry in entries:
        level = entry["level"]
        level_id = level["id"]
        count = entry.get("completion_count") or 1
        known = st["counts"].get(level_id, 0)

        if count == known:
            continue
        if count < known:  # un record a ete retire : on se met simplement a jour
            st["counts"][level_id] = count
            continue

        diff = count - known
        if count == 1:
            candidates = [entry]
        else:
            try:
                victors = api_get(f"{kind}/clan/{CLAN_ID}/levels/{level_id}/records")
            except Exception as error:  # on reessaiera au prochain passage
                print(f"[{label}] Detail indisponible pour {level.get('name')} : {error}",
                      file=sys.stderr)
                continue
            candidates = [dict(r, level=level) for r in victors]

        unseen = [r for r in candidates if r.get("id") and r["id"] not in st["ids"]]
        unseen.sort(key=lambda r: r.get("created_at") or r.get("achieved_at") or "")
        cut = max(0, len(unseen) - diff)
        st["ids"].update(r["id"] for r in unseen[:cut])  # anciens jamais vus : en silence
        for record in unseen[cut:]:
            pending.append((record.get("achieved_at") or "", record))
        count_updates[level_id] = count

    pending.sort(key=lambda item: item[0])
    for _, record in pending:
        content = f"Nouveau record dans le clan !\n{describe(label, record)}"
        post_to_discord(content.strip())
        st["ids"].add(record["id"])
        save_state(state)  # sauvegarde apres chaque envoi pour ne jamais doubler
        time.sleep(1)

    st["counts"].update(count_updates)
    save_state(state)
    if pending:
        print(f"[{label}] {len(pending)} nouveau(x) record(s) annonce(s).")


def check_all(state):
    for label, kind in SOURCES.items():
        try:
            check_source(label, kind, state)
        except Exception as error:  # une erreur sur une liste ne bloque pas l'autre
            print(f"[{label}] Erreur pendant la verification : {error}", file=sys.stderr)


def say():
    """Envoie un message libre dans le salon (mode --say)."""
    args = sys.argv[sys.argv.index("--say") + 1:]
    with_ping = "--ping" in args or os.getenv("SAY_PING", "").lower() == "true"
    text = " ".join(a for a in args if a != "--ping") or os.getenv("SAY_MESSAGE", "")
    text = text.replace("\\n", "\n").strip()  # "\n" tape a la main = retour a la ligne
    if not text:
        sys.exit('Message vide. Exemple : python aredl_clan_watch.py --say "Salut !"')

    content = f"{PING} {text}" if with_ping and PING else text
    if len(content) > 2000:
        sys.exit(f"Message trop long ({len(content)} caracteres, Discord accepte 2000 maximum).")

    # Les mentions de roles et de membres fonctionnent, pas @everyone/@here (securite).
    post_to_discord(content, mentions=("roles", "users"))
    if not DRY_RUN:
        print("Message envoye.")


def main():
    if not DRY_RUN and "COLLE_" in WEBHOOK_URL:
        sys.exit("Renseigne WEBHOOK_URL (ou lance avec DRY_RUN=1 pour tester).")

    if "--say" in sys.argv:
        say()
        return

    state = load_state()
    if "--once" in sys.argv:
        check_all(state)
        return
    while True:
        check_all(state)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
