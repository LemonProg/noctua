"""File de tâches partagée : point de passage unique de tous les échanges entre agents."""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from noyau.base import transaction
from noyau.horloge import Horloge, maintenant_utc
from noyau.messages import HUMAIN, Message, valider

# Tâches gourmandes (voir noyau/creneaux.py) : la suite d'une chaîne de nuit reste de nuit.
LOURDES = frozenset({"brief_texte", "brief_visuel", "brief_video", "asset", "post_brouillon",
                     "demande_veille", "demande_planning", "analyse_style"})

TENTATIVES_MAX = 3

# Transitions autorisées : à_faire → en_cours → à_valider → validé / refusé,
# plus terminé (tâche intermédiaire), échec (après TENTATIVES_MAX) et les reprises.
TRANSITIONS = {
    "à_faire": {"en_cours"},
    "en_cours": {"à_valider", "terminé", "à_faire", "échec"},
    "à_valider": {"validé", "refusé"},
    "échec": {"à_faire"},
    "validé": set(),
    "refusé": set(),
    "terminé": set(),
}


class TransitionInterdite(Exception):
    pass


class TacheIntrouvable(Exception):
    pass


@dataclass(frozen=True)
class Tache:
    id: int
    type: str
    agent: str
    voie: str
    statut: str
    contenu: dict[str, Any]
    parent_id: int | None
    tentatives: int
    erreur: str | None
    commentaire: str | None
    cree_le: str
    maj_le: str
    pris_le: str | None
    creneau: str = "jour"

    @property
    def message(self) -> Message:
        return valider(self.type, self.contenu)

    @classmethod
    def depuis_ligne(cls, ligne: sqlite3.Row) -> "Tache":
        valeurs = dict(ligne)
        valeurs["contenu"] = json.loads(valeurs["contenu"])
        return cls(**valeurs)


class File:
    def __init__(self, conn: sqlite3.Connection, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.horloge = horloge

    def _date(self, decalage: timedelta = timedelta(0)) -> str:
        return (self.horloge().astimezone(UTC) - decalage).isoformat()

    # --- Création et lecture ---

    def creer(self, message: Message, agent: str | None = None, parent_id: int | None = None,
              a_valider: bool = False, creneau: str | None = None) -> int:
        """Ajoute un message validé à la file.

        Les leçons arrivent directement « à_valider ». `a_valider=True` fait de même pour un autre
        message, quand l'étape de l'agent destinataire est sautée (ex. relecteur absent en phase 2).
        `creneau` (noyau/creneaux.py) : sans précision, une tâche lourde issue d'une tâche de nuit en est
        la suite (« nuit_suite ») ; sinon « jour ».
        """
        if agent is None:
            if len(message.destinataires) != 1:
                raise ValueError(
                    f"Le message {message.type} a plusieurs destinataires possibles : préciser l'agent "
                    f"({', '.join(message.destinataires)})"
                )
            agent = message.destinataires[0]
        elif agent not in message.destinataires:
            raise ValueError(f"{agent} ne peut pas recevoir un message {message.type}")

        if creneau is None:
            creneau = "jour"
            if parent_id and message.type in LOURDES and self.lire(parent_id).creneau != "jour":
                creneau = "nuit_suite"
        statut = "à_valider" if agent == HUMAIN or a_valider else "à_faire"
        date = self._date()
        return self.conn.execute(
            "INSERT INTO taches (type, agent, voie, statut, contenu, parent_id, cree_le, maj_le, creneau) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.type, agent, message.voie, statut, json.dumps(message.model_dump(mode="json"), ensure_ascii=False),
             parent_id, date, date, creneau),
        ).lastrowid

    def lire(self, tache_id: int) -> Tache:
        ligne = self.conn.execute("SELECT * FROM taches WHERE id = ?", (tache_id,)).fetchone()
        if ligne is None:
            raise TacheIntrouvable(f"Tâche {tache_id} introuvable")
        return Tache.depuis_ligne(ligne)

    def lister(self, statut: str | None = None, agent: str | None = None) -> list[Tache]:
        conditions, parametres = [], []
        if statut:
            conditions.append("statut = ?")
            parametres.append(statut)
        if agent:
            conditions.append("agent = ?")
            parametres.append(agent)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        lignes = self.conn.execute(f"SELECT * FROM taches {where} ORDER BY id", parametres)
        return [Tache.depuis_ligne(l) for l in lignes]

    def modifier(self, tache_id: int, **champs: Any) -> Tache:
        """Retouche par l'humain d'un contenu en attente de validation (ex. horaire, étiquette IA).
        Le message modifié est revalidé avant d'être enregistré."""
        with transaction(self.conn):
            tache = self.lire(tache_id)
            if tache.statut != "à_valider":
                raise TransitionInterdite(f"Tâche {tache_id} : seul un contenu « à_valider » peut être modifié")
            message = valider(tache.type, {**tache.contenu, **champs})
            self.conn.execute("UPDATE taches SET contenu = ?, maj_le = ? WHERE id = ?",
                              (json.dumps(message.model_dump(mode="json"), ensure_ascii=False), self._date(), tache_id))
        return self.lire(tache_id)

    # --- Prise d'une tâche ---

    def prendre(self, agents: Iterable[str], voie: str = "normale",
                creneaux: Iterable[str] = ("jour", "nuit", "nuit_suite")) -> Tache | None:
        """Prend la plus ancienne tâche « à_faire » d'un des agents, sur une voie donnée, parmi les créneaux
        permis maintenant (noyau/creneaux.py).

        Atomique : deux workers ne peuvent pas prendre la même tâche.
        Sur la voie `rendu`, rien n'est pris si un rendu est déjà en cours.
        """
        agents, creneaux = list(agents), list(creneaux)
        if not agents or not creneaux:
            return None
        marques = ", ".join("?" for _ in agents)
        marques_creneaux = ", ".join("?" for _ in creneaux)
        try:
            with transaction(self.conn):
                if voie == "rendu" and self.conn.execute(
                    "SELECT 1 FROM taches WHERE voie = 'rendu' AND statut = 'en_cours'"
                ).fetchone():
                    return None
                ligne = self.conn.execute(
                    f"SELECT id FROM taches WHERE statut = 'à_faire' AND voie = ? AND agent IN ({marques}) "
                    f"AND creneau IN ({marques_creneaux}) ORDER BY cree_le, id LIMIT 1",
                    (voie, *agents, *creneaux),
                ).fetchone()
                if ligne is None:
                    return None
                date = self._date()
                self.conn.execute(
                    "UPDATE taches SET statut = 'en_cours', tentatives = tentatives + 1, pris_le = ?, maj_le = ? "
                    "WHERE id = ?",
                    (date, date, ligne["id"]),
                )
        except sqlite3.IntegrityError:
            # Index un_seul_rendu : un autre processus a lancé un rendu entre-temps.
            return None
        return self.lire(ligne["id"])

    # --- Transitions ---

    def changer_statut(self, tache_id: int, nouveau: str, *, erreur: str | None = None,
                       commentaire: str | None = None) -> Tache:
        with transaction(self.conn):
            tache = self.lire(tache_id)
            if nouveau not in TRANSITIONS[tache.statut]:
                raise TransitionInterdite(f"Tâche {tache_id} : passage de « {tache.statut} » à « {nouveau} » interdit")
            if nouveau == "refusé" and not (commentaire and commentaire.strip()):
                raise TransitionInterdite(f"Tâche {tache_id} : un refus doit avoir un commentaire")
            self.conn.execute(
                "UPDATE taches SET statut = ?, maj_le = ?, "
                "erreur = COALESCE(?, erreur), commentaire = COALESCE(?, commentaire), "
                "tentatives = CASE WHEN ? = 'échec' AND ? = 'à_faire' THEN 0 ELSE tentatives END "
                "WHERE id = ?",
                (nouveau, self._date(), erreur, commentaire, tache.statut, nouveau, tache_id),
            )
        return self.lire(tache_id)

    def terminer(self, tache_id: int) -> Tache:
        return self.changer_statut(tache_id, "terminé")

    def soumettre(self, tache_id: int) -> Tache:
        """Met un contenu dans la file de validation humaine."""
        return self.changer_statut(tache_id, "à_valider")

    def valider(self, tache_id: int, commentaire: str | None = None) -> Tache:
        return self.changer_statut(tache_id, "validé", commentaire=commentaire)

    def refuser(self, tache_id: int, commentaire: str) -> Tache:
        return self.changer_statut(tache_id, "refusé", commentaire=commentaire)

    def echouer(self, tache_id: int, erreur: str) -> Tache:
        """Remet la tâche à faire, ou la passe en « échec » après TENTATIVES_MAX tentatives."""
        tache = self.lire(tache_id)
        nouveau = "échec" if tache.tentatives >= TENTATIVES_MAX else "à_faire"
        return self.changer_statut(tache_id, nouveau, erreur=erreur)

    def reporter(self, tache_id: int, raison: str) -> Tache:
        """Remet une tâche « en_cours » dans la file sans consommer de tentative (ex. quota atteint)."""
        with transaction(self.conn):
            tache = self.lire(tache_id)
            if tache.statut != "en_cours":
                raise TransitionInterdite(f"Tâche {tache_id} : seule une tâche « en_cours » peut être reportée")
            self.conn.execute(
                "UPDATE taches SET statut = 'à_faire', tentatives = MAX(tentatives - 1, 0), erreur = ?, "
                "pris_le = NULL, maj_le = ? WHERE id = ?",
                (raison, self._date(), tache_id),
            )
        return self.lire(tache_id)

    def relancer(self, tache_id: int) -> Tache:
        """Relance manuelle d'une tâche en échec, avec un compteur de tentatives remis à zéro."""
        return self.changer_statut(tache_id, "à_faire")

    def reprendre_bloquees(self, plus_vieilles_que: timedelta = timedelta(0)) -> list[int]:
        """Après un plantage : les tâches restées « en_cours » sont remises à faire (ou en échec)."""
        limite = self._date(plus_vieilles_que)
        bloquees = self.conn.execute(
            "SELECT id FROM taches WHERE statut = 'en_cours' AND pris_le <= ? ORDER BY id", (limite,)
        ).fetchall()
        for ligne in bloquees:
            self.echouer(ligne["id"], "interrompue (arrêt du worker pendant le traitement)")
        return [l["id"] for l in bloquees]
