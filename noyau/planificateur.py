"""Planificateur : lance les tâches périodiques de l'agence (analyse, veille, puis planning de la semaine suivante).

Le dimanche (heure de Paris) : à 19 h, une demande d'analyse des statistiques ; à 20 h, une demande de veille ;
à 21 h, une demande de planning. La veille et le planning, gourmands, sont confiés à la nuit (noyau/creneaux.py) :
ils tournent après minuit, et les posts de la semaine sont produits dans la foulée.
Si le worker était arrêté à l'heure prévue, la tâche est lancée à son redémarrage (une seule fois).
Au tout premier démarrage, rien n'est lancé : l'heure est notée, pour ne pas consommer l'abonnement par surprise.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from noyau.file import File
from noyau.horloge import Horloge, maintenant_utc
from noyau.messages import DemandeAnalyse, DemandePlanning, DemandeVeille
from noyau.modeles import OPUS, SONNET

FUSEAU = ZoneInfo("Europe/Paris")
DIMANCHE = 6


@dataclass(frozen=True)
class Evenement:
    nom: str
    jour: int  # 0 = lundi … 6 = dimanche
    heure: time


EVENEMENTS = (Evenement("demande_analyse", DIMANCHE, time(19, 0)), Evenement("demande_veille", DIMANCHE, time(20, 0)),
              Evenement("demande_planning", DIMANCHE, time(21, 0)))
# Créneau des tâches périodiques : la veille et le planning attendent la nuit.
CRENEAUX_EVENEMENTS = {"demande_analyse": "jour", "demande_veille": "nuit", "demande_planning": "nuit"}
AGENTS_EVENEMENTS = {"demande_analyse": "agent_analyste", "demande_veille": "agent_veille",
                     "demande_planning": "orchestrateur_marketing"}


def lundi_suivant(jour: date) -> date:
    return jour + timedelta(days=7 - jour.weekday())


def message(nom: str, semaine: date):
    """Messages du planificateur ; modèle et effort précisés comme pour toute tâche confiée."""
    if nom == "demande_analyse":
        return DemandeAnalyse(semaine=semaine, modele=SONNET, effort="medium")
    if nom == "demande_veille":
        return DemandeVeille(semaine=semaine, modele=SONNET, effort="high")
    return DemandePlanning(semaine=semaine, modele=OPUS, effort="high")


class Planificateur:
    def __init__(self, conn: sqlite3.Connection, file: File, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.file = file
        self.horloge = horloge

    def _derniere_occurrence(self, evenement: Evenement, maintenant: datetime) -> datetime:
        local = maintenant.astimezone(FUSEAU)
        jour = local.date() - timedelta(days=(local.weekday() - evenement.jour) % 7)
        occurrence = datetime.combine(jour, evenement.heure, FUSEAU)
        if occurrence > local:
            occurrence -= timedelta(days=7)
        return occurrence

    def verifier(self) -> list[int]:
        """Lance les tâches dont l'heure est passée depuis le dernier lancement. Renvoie les tâches créées."""
        maintenant = self.horloge().astimezone(UTC)
        creees = []
        for evenement in EVENEMENTS:
            ligne = self.conn.execute("SELECT dernier_lancement FROM planification WHERE evenement = ?",
                                      (evenement.nom,)).fetchone()
            if ligne is None:  # premier démarrage : on note l'heure sans lancer
                self._noter(evenement.nom, maintenant)
                continue
            occurrence = self._derniere_occurrence(evenement, maintenant)
            if datetime.fromisoformat(ligne["dernier_lancement"]) < occurrence:
                creees.append(self.lancer(evenement.nom, occurrence.date(), CRENEAUX_EVENEMENTS[evenement.nom]))
        return creees

    def lancer(self, nom: str, jour: date | None = None, creneau: str = "jour") -> int:
        """Lance une tâche périodique ; le bouton de l'interface la lance tout de suite (créneau « jour »)."""
        jour = jour or self.horloge().astimezone(FUSEAU).date()
        tache_id = self.file.creer(message(nom, lundi_suivant(jour)), creneau=creneau)
        self._noter(nom, self.horloge().astimezone(UTC))
        return tache_id

    def _noter(self, nom: str, quand: datetime) -> None:
        self.conn.execute("INSERT INTO planification (evenement, dernier_lancement) VALUES (?, ?) "
                          "ON CONFLICT (evenement) DO UPDATE SET dernier_lancement = excluded.dernier_lancement",
                          (nom, quand.isoformat()))
