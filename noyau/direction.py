"""Direction artistique de l'agence, tirée des références de style de l'humain.

agent_visuel la propose ; l'humain la valide (en la retouchant si besoin) ou la refuse. La dernière
direction validée est écrite dans direction_artistique.md, lu par les agents comme la charte et les leçons.
La table est la source de vérité ; le fichier est régénéré.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from noyau.base import transaction
from noyau.config import RACINE
from noyau.horloge import Horloge, maintenant_utc

ENTETE = """# Direction artistique

Tirée des réalisations de référence de l'humain et validée par lui. Lue par les agents avant chaque visuel.
Ce fichier est régénéré depuis la base : ne pas le modifier à la main.

"""


class DirectionErreur(Exception):
    pass


@dataclass(frozen=True)
class Direction:
    id: int
    tache_id: int | None
    texte: str
    resume: str
    references: list[int]
    statut: str
    commentaire: str | None
    cree_le: str
    decide_le: str | None


class Directions:
    def __init__(self, conn: sqlite3.Connection, racine: Path = RACINE, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.racine = racine
        self.horloge = horloge

    def _date(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()

    def lire(self, direction_id: int) -> Direction:
        ligne = self.conn.execute("SELECT * FROM directions_artistiques WHERE id = ?", (direction_id,)).fetchone()
        if ligne is None:
            raise DirectionErreur(f"Direction artistique {direction_id} introuvable")
        valeurs = dict(ligne)
        valeurs["references"] = json.loads(valeurs.pop("references_json"))
        return Direction(**valeurs)

    def lister(self, statut: str) -> list[Direction]:
        lignes = self.conn.execute("SELECT id FROM directions_artistiques WHERE statut = ? ORDER BY id", (statut,))
        return [self.lire(l["id"]) for l in lignes]

    def active(self) -> Direction | None:
        validees = self.lister("validée")
        return validees[-1] if validees else None

    def proposer(self, texte: str, resume: str, references: list[int], tache_id: int | None = None) -> int:
        return self.conn.execute(
            "INSERT INTO directions_artistiques (tache_id, texte, resume, references_json, cree_le) VALUES (?, ?, ?, ?, ?)",
            (tache_id, texte, resume, json.dumps(references), self._date()),
        ).lastrowid

    def valider(self, direction_id: int, texte: str | None = None) -> Direction:
        """Valide la proposition (texte retouché par l'humain s'il est fourni) ; l'ancienne est remplacée."""
        with transaction(self.conn):
            direction = self._proposee(direction_id)
            texte = (texte or "").strip() or direction.texte
            self.conn.execute("UPDATE directions_artistiques SET statut = 'remplacée' WHERE statut = 'validée'")
            self.conn.execute("UPDATE directions_artistiques SET statut = 'validée', texte = ?, decide_le = ? WHERE id = ?",
                              (texte, self._date(), direction_id))
        self.regenerer()
        return self.lire(direction_id)

    def refuser(self, direction_id: int, commentaire: str | None = None) -> Direction:
        self._proposee(direction_id)
        self.conn.execute("UPDATE directions_artistiques SET statut = 'refusée', commentaire = ?, decide_le = ? WHERE id = ?",
                          ((commentaire or "").strip() or None, self._date(), direction_id))
        return self.lire(direction_id)

    def _proposee(self, direction_id: int) -> Direction:
        direction = self.lire(direction_id)
        if direction.statut != "proposée":
            raise DirectionErreur(f"Direction artistique {direction_id} déjà traitée (statut : {direction.statut})")
        return direction

    def regenerer(self) -> Path:
        chemin = self.racine / "direction_artistique.md"
        active = self.active()
        if active is None:
            chemin.unlink(missing_ok=True)
            return chemin
        temporaire = chemin.with_name("direction_artistique.md.partiel")
        temporaire.write_text(ENTETE + active.texte.strip() + "\n", encoding="utf-8")
        temporaire.replace(chemin)
        return chemin
