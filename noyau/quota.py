"""Suivi du quota de l'abonnement Claude, tel que Claude Code le rapporte à chaque appel.

Deux fenêtres : les 5 dernières heures et les 7 derniers jours (taux d'utilisation de 0 à 1, heure de
remise à zéro). Au-delà du seuil fixé dans .env, l'agence se met en pause jusqu'à la remise à zéro :
l'abonnement sert aussi à l'humain.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from noyau.horloge import Horloge, maintenant_utc

FENETRES = {"five_hour": "5 heures", "seven_day": "semaine"}


@dataclass(frozen=True)
class Fenetre:
    nom: str
    utilisation: float  # de 0 à 1
    reprise: datetime | None
    statut: str
    date: datetime

    @property
    def pourcentage(self) -> int:
        return round(self.utilisation * 100)


class Quota:
    def __init__(self, conn: sqlite3.Connection, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.horloge = horloge

    def enregistrer(self, info: dict) -> None:
        """Enregistre les fenêtres d'un événement `rate_limit_event` de Claude Code."""
        date = self.horloge().astimezone(UTC).isoformat()
        for nom, fenetre in (info.get("unifiedWindows") or {}).items():
            if nom not in FENETRES or "utilization" not in fenetre:
                continue
            reprise = fenetre.get("resetsAt")
            self.conn.execute(
                "INSERT INTO quota_abonnement (date, fenetre, utilisation, reprise, statut) VALUES (?, ?, ?, ?, ?)",
                (date, nom, float(fenetre["utilization"]),
                 datetime.fromtimestamp(reprise, UTC).isoformat() if reprise else None, fenetre.get("status") or ""),
            )

    def actuel(self) -> dict[str, Fenetre]:
        """Dernière mesure de chaque fenêtre ; une fenêtre dont l'heure de reprise est passée repart à 0."""
        maintenant = self.horloge().astimezone(UTC)
        resultat = {}
        for nom in FENETRES:
            ligne = self.conn.execute("SELECT * FROM quota_abonnement WHERE fenetre = ? ORDER BY id DESC LIMIT 1",
                                      (nom,)).fetchone()
            if ligne is None:
                continue
            reprise = datetime.fromisoformat(ligne["reprise"]) if ligne["reprise"] else None
            utilisation = 0.0 if reprise and reprise <= maintenant else ligne["utilisation"]
            resultat[nom] = Fenetre(FENETRES[nom], utilisation, reprise, ligne["statut"],
                                    datetime.fromisoformat(ligne["date"]))
        return resultat

    def derniere_mesure(self) -> datetime | None:
        ligne = self.conn.execute("SELECT max(date) FROM quota_abonnement").fetchone()[0]
        return datetime.fromisoformat(ligne) if ligne else None

    def a_actualiser(self, age_max: timedelta) -> bool:
        derniere = self.derniere_mesure()
        return derniere is None or self.horloge().astimezone(UTC) - derniere >= age_max

    def attente_avant_appel(self, seuil_pourcent: float, seuil_cinq_heures: float | None = None) -> timedelta | None:
        """Délai à attendre si une fenêtre a atteint son seuil, sinon None.

        `seuil_cinq_heures` : seuil propre à la fenêtre de 5 heures (la nuit, elle peut être utilisée
        entièrement : elle se termine avant le réveil de l'humain)."""
        maintenant = self.horloge().astimezone(UTC)
        seuils = {nom: seuil_pourcent for nom in FENETRES}
        if seuil_cinq_heures is not None:
            seuils["five_hour"] = seuil_cinq_heures
        attentes = [f.reprise - maintenant + timedelta(minutes=1) for nom, f in self.actuel().items()
                    if f.utilisation * 100 >= seuils[nom] and f.reprise and f.reprise > maintenant]
        return max(attentes) if attentes else None
