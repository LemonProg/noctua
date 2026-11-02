"""Pause de l'agence décidée par l'humain : il veut son abonnement Claude pour lui.

Tant que la pause dure, aucun appel à Claude : le worker ne prend plus de tâche, ne mesure plus le quota, et un
agent en cours de travail s'arrête avant son prochain appel (sa tâche est reportée sans consommer de tentative).
Ce qui n'appelle pas Claude continue : publication des posts validés à leur horaire, relevé des statistiques,
notifications, purge. Pause pour une durée (reprise automatique) ou jusqu'à ce que l'humain reprenne.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from noyau.horloge import Horloge, maintenant_utc

DUREES_HEURES = (1, 3, 5)  # proposées dans l'interface ; 5 h = une fenêtre de l'abonnement


@dataclass(frozen=True)
class EtatPause:
    depuis: datetime
    jusqu_a: datetime | None  # None : jusqu'à ce que l'humain reprenne


class Pause:
    def __init__(self, conn: sqlite3.Connection, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.horloge = horloge

    def etat(self) -> EtatPause | None:
        """La pause en cours, ou None. Une pause arrivée à son terme est levée."""
        ligne = self.conn.execute("SELECT depuis, jusqu_a FROM pause WHERE id = 1").fetchone()
        if ligne is None:
            return None
        jusqu_a = datetime.fromisoformat(ligne["jusqu_a"]) if ligne["jusqu_a"] else None
        if jusqu_a and jusqu_a <= self.horloge():
            self.reprendre()
            return None
        return EtatPause(datetime.fromisoformat(ligne["depuis"]), jusqu_a)

    def activer(self, duree: timedelta | None = None) -> EtatPause:
        """Met l'agence en pause pour `duree`, ou sans limite. Remplace une pause en cours."""
        if duree is not None and duree <= timedelta(0):
            raise ValueError("La durée de la pause doit être positive")
        maintenant = self.horloge().astimezone(UTC)
        jusqu_a = (maintenant + duree).isoformat() if duree else None
        self.conn.execute("INSERT INTO pause (id, depuis, jusqu_a) VALUES (1, ?, ?) "
                          "ON CONFLICT (id) DO UPDATE SET depuis = excluded.depuis, jusqu_a = excluded.jusqu_a",
                          (maintenant.isoformat(), jusqu_a))
        return self.etat()

    def reprendre(self) -> None:
        self.conn.execute("DELETE FROM pause WHERE id = 1")
