"""Journal des appels de modèles et plafond mensuel des API payantes (en dollars).

Avant un appel payant (aucune API payante pour l'instant), l'agent appelle `verifier()`, puis `enregistrer()` après.
Les appels Claude passent par l'abonnement : enregistrés avec un coût de 0, pour suivre l'usage.
Un plafond à 0 bloque tous les appels payants. Les mois sont comptés en UTC.
"""

import sqlite3
from datetime import UTC, datetime

from noyau.config import Config
from noyau.horloge import Horloge, maintenant_utc


class PlafondAtteint(Exception):
    pass


def _debut_du_mois(date: datetime) -> datetime:
    return date.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _mois_suivant(debut: datetime) -> datetime:
    return debut.replace(year=debut.year + 1, month=1) if debut.month == 12 else debut.replace(month=debut.month + 1)


class JournalCouts:
    def __init__(self, conn: sqlite3.Connection, plafond_mensuel_usd: float, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.plafond = plafond_mensuel_usd
        self.horloge = horloge

    @classmethod
    def depuis_config(cls, conn: sqlite3.Connection, config: Config, horloge: Horloge = maintenant_utc):
        return cls(conn, config.plafond_couts_mensuel_usd, horloge)

    def total_du_mois(self, date: datetime | None = None) -> float:
        debut = _debut_du_mois(date or self.horloge())
        ligne = self.conn.execute(
            "SELECT COALESCE(SUM(cout_usd), 0) FROM couts WHERE date >= ? AND date < ?",
            (debut.isoformat(), _mois_suivant(debut).isoformat()),
        ).fetchone()
        return float(ligne[0])

    def resume_du_mois(self, date: datetime | None = None) -> dict[str, dict[str, float]]:
        """Par fournisseur : nombre d'appels, jetons, images et coût du mois."""
        debut = _debut_du_mois(date or self.horloge())
        lignes = self.conn.execute(
            "SELECT fournisseur, count(*) AS appels, SUM(jetons_entree) AS jetons_entree, "
            "SUM(jetons_sortie) AS jetons_sortie, SUM(images) AS images, SUM(cout_usd) AS cout_usd "
            "FROM couts WHERE date >= ? AND date < ? GROUP BY fournisseur ORDER BY fournisseur",
            (debut.isoformat(), _mois_suivant(debut).isoformat()),
        )
        return {l["fournisseur"]: {k: l[k] for k in ("appels", "jetons_entree", "jetons_sortie", "images", "cout_usd")}
                for l in lignes}

    def restant(self) -> float:
        return max(self.plafond - self.total_du_mois(), 0)

    def verifier(self, cout_estime_usd: float = 0) -> None:
        """Lève PlafondAtteint si l'appel prévu ferait dépasser le plafond du mois."""
        if self.plafond <= 0:
            raise PlafondAtteint(
                "Aucun appel payant autorisé : le plafond mensuel est à 0 $. "
                "Fixez PLAFOND_COUTS_MENSUEL_USD dans .env."
            )
        total = self.total_du_mois()
        if total + cout_estime_usd > self.plafond:
            raise PlafondAtteint(
                f"Plafond mensuel atteint : {total:.2f} $ dépensés + {cout_estime_usd:.2f} $ prévus "
                f"> {self.plafond:.2f} $. Appel annulé."
            )

    def enregistrer(self, fournisseur: str, modele: str, cout_usd: float, *, jetons_entree: int = 0,
                    jetons_sortie: int = 0, images: int = 0, tache_id: int | None = None) -> int:
        return self.conn.execute(
            "INSERT INTO couts (date, fournisseur, modele, jetons_entree, jetons_sortie, images, cout_usd, tache_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.horloge().astimezone(UTC).isoformat(), fournisseur, modele, jetons_entree, jetons_sortie,
             images, cout_usd, tache_id),
        ).lastrowid
