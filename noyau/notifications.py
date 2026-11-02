"""Notifications push sur le téléphone de l'humain, par ntfy (app libre et gratuite, iPhone et Android).

L'agence publie sur un sujet ntfy secret (NTFY_SUJET, choisi au hasard) ; l'app abonnée à ce sujet affiche la
notification. Serveur public ntfy.sh par défaut, remplaçable par un serveur ntfy installé sur le VPS
(NTFY_SERVEUR). Messages sobres (nombres et types, jamais le texte des posts) ; un clic ouvre la bonne page
de l'interface (URL_PUBLIQUE).

Événements, vérifiés chaque minute par le worker et regroupés :
- contenus et leçons à valider ;
- tâches en échec (priorité haute) ;
- posts publiés sur Instagram ;
- alerte disque (une fois par jour).
Pendant la plage de silence (NOTIFICATIONS_SILENCE, 22 h – 8 h par défaut), rien n'est envoyé : tout part
en un message au réveil. Un événement n'est notifié qu'une fois ; un envoi raté est retenté la minute suivante.
"""

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from noyau import bilan
from noyau.config import Config
from noyau.horloge import Horloge, maintenant_utc

journal = logging.getLogger("agence.notifications")
FUSEAU = ZoneInfo("Europe/Paris")
DELAI_RESEAU_SECONDES = 15
NOMS = {"post_brouillon": "post", "lecon": "leçon"}

# (URL, corps JSON) → code HTTP
Transport = Callable[[str, dict], int]


def transport_urllib(url: str, corps: dict) -> int:
    requete = urllib.request.Request(url, data=json.dumps(corps).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(requete, timeout=DELAI_RESEAU_SECONDES) as reponse:
        return reponse.status


def plage(texte: str) -> tuple[time, time]:
    debut, fin = texte.split("-")
    return time.fromisoformat(debut.strip()), time.fromisoformat(fin.strip())


class Notifications:
    def __init__(self, config: Config, conn: sqlite3.Connection, horloge: Horloge = maintenant_utc,
                 transport: Transport = transport_urllib):
        self.config = config
        self.conn = conn
        self.horloge = horloge
        self.transport = transport

    @property
    def actives(self) -> bool:
        return bool(self.config.ntfy_sujet)

    def en_silence(self, maintenant: datetime) -> bool:
        debut, fin = plage(self.config.notifications_silence)
        heure = maintenant.astimezone(FUSEAU).time()
        return (debut <= heure or heure < fin) if debut > fin else debut <= heure < fin

    # --- Envoi ---

    def envoyer(self, titre: str, message: str, *, priorite: int = 3, tags: tuple[str, ...] = (),
                chemin: str = "/a-valider") -> bool:
        """Publie une notification ; renvoie False (sans lever) si ntfy est injoignable."""
        corps = {"topic": self.config.ntfy_sujet, "title": titre, "message": message, "priority": priorite,
                 "tags": list(tags)}
        if self.config.url_publique:
            corps["click"] = f"{self.config.url_publique}{chemin}"
        try:
            return 200 <= self.transport(self.config.ntfy_serveur, corps) < 300
        except (urllib.error.URLError, TimeoutError, OSError) as erreur:
            journal.warning("Notification non envoyée : %s", erreur)
            return False

    # --- Événements ---

    def _deja(self, type_evenement: str) -> set[int]:
        return {l["objet_id"] for l in self.conn.execute(
            "SELECT objet_id FROM notifications WHERE type = ?", (type_evenement,))}

    def _noter(self, type_evenement: str, identifiants) -> None:
        date = self.horloge().astimezone(UTC).isoformat()
        self.conn.executemany("INSERT OR IGNORE INTO notifications (type, objet_id, envoye_le) VALUES (?, ?, ?)",
                              [(type_evenement, i, date) for i in identifiants])

    def _premiere_fois(self) -> None:
        """À l'activation : les échecs et publications passés ne sont pas notifiés (seulement ce qui suit)."""
        if self.conn.execute("SELECT 1 FROM notifications LIMIT 1").fetchone():
            return
        self._noter("echec", [l["id"] for l in self.conn.execute("SELECT id FROM taches WHERE statut = 'échec'")])
        self._noter("publication", [l["tache_id"] for l in self.conn.execute("SELECT tache_id FROM publications")])
        self._noter("init", [0])

    def verifier(self, alerte_disque: str | None = None) -> list[str]:
        """Envoie les notifications en attente. Renvoie les titres envoyés."""
        maintenant = self.horloge()
        if not self.actives or self.en_silence(maintenant):
            return []
        self._premiere_fois()
        envoyes = []

        deja = self._deja("a_valider")
        a_valider = [l for l in self.conn.execute(
            "SELECT id, type FROM taches WHERE statut = 'à_valider' ORDER BY id") if l["id"] not in deja]
        if a_valider:
            types = Counter(NOMS.get(l["type"], "contenu") for l in a_valider)
            detail = ", ".join(f"{n} {nom}{'s' if n > 1 else ''}" for nom, n in types.items())
            titre = f"{len(a_valider)} à valider"
            # Un appui ouvre directement le premier contenu concerné.
            posts = [l["id"] for l in a_valider if l["type"] != "lecon"]
            chemin = f"/a-valider#post-{posts[0]}" if posts else "/lecons"
            if self.envoyer(titre, f"Nouveau : {detail}.", tags=("inbox_tray",), chemin=chemin):
                self._noter("a_valider", [l["id"] for l in a_valider])
                envoyes.append(titre)

        deja = self._deja("echec")
        echecs = [l for l in self.conn.execute("SELECT id, type, agent FROM taches WHERE statut = 'échec' ORDER BY id")
                  if l["id"] not in deja]
        if echecs:
            titre = f"{len(echecs)} tâche{'s' if len(echecs) > 1 else ''} en échec"
            detail = ", ".join(f"{l['type']} ({l['agent']})" for l in echecs[:5])
            # La fenêtre de l'agent responsable, tâche dépliée (page Agents).
            premier = echecs[0]
            chemin = f"/agents/{premier['agent']}?tache={premier['id']}#tache-{premier['id']}"
            if self.envoyer(titre, detail, priorite=4, tags=("warning",), chemin=chemin):
                self._noter("echec", [l["id"] for l in echecs])
                envoyes.append(titre)

        deja = self._deja("publication")
        publies = [l["tache_id"] for l in self.conn.execute(
            "SELECT tache_id FROM publications WHERE mode = 'instagram' AND identifiant_externe IS NOT NULL")
            if l["tache_id"] not in deja]
        if publies:
            titre = f"{len(publies)} post{'s' if len(publies) > 1 else ''} publié{'s' if len(publies) > 1 else ''} sur Instagram"
            chemin = f"/calendrier/tache/{publies[0]}" if len(publies) == 1 else "/calendrier"
            if self.envoyer(titre, "Publication automatique réussie.", tags=("white_check_mark",), chemin=chemin):
                self._noter("publication", publies)
                envoyes.append(titre)

        # Le lundi matin (après la plage de silence) : bilan de la semaine écoulée, une fois.
        lundi = bilan.semaine_ecoulee(maintenant)
        if maintenant.astimezone(FUSEAU).weekday() == 0 and lundi.toordinal() not in self._deja("bilan"):
            if self.envoyer("Bilan de la semaine", bilan.resume(bilan.bilan(self.conn, lundi)), tags=("bar_chart",), chemin="/"):
                self._noter("bilan", [lundi.toordinal()])
                envoyes.append("Bilan de la semaine")

        jour = maintenant.astimezone(FUSEAU).date().toordinal()
        if alerte_disque and jour not in self._deja("disque"):
            if self.envoyer("Espace disque faible", alerte_disque, priorite=4, tags=("floppy_disk",), chemin="/corbeille"):
                self._noter("disque", [jour])
                envoyes.append("Espace disque faible")
        return envoyes
