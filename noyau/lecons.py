"""Leçons tirées des contenus rejetés.

agent_erreurs propose une leçon ; elle arrive dans la file de validation (tâche « lecon » pour l'humain)
et dans la table `lecons` (statut « proposée »). Seules les leçons validées sont recopiées dans
lecons.md, lu par tous les agents. La table est la source de vérité ; lecons.md est régénéré.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from noyau.base import transaction
from noyau.config import RACINE
from noyau.contexte import ENTETE_LECONS
from noyau.file import File
from noyau.horloge import Horloge, maintenant_utc
from noyau.messages import Lecon


class LeconErreur(Exception):
    pass


@dataclass(frozen=True)
class LeconInfo:
    id: int
    tache_id: int
    origine_rejet: str
    erreur: str
    cause: str
    regle: str
    agents_concernes: list[str]
    statut: str
    commentaire: str | None
    cree_le: str
    decide_le: str | None


class Lecons:
    def __init__(self, conn: sqlite3.Connection, file: File, racine: Path = RACINE,
                 horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.file = file
        self.racine = racine
        self.horloge = horloge

    def _date(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()

    def lire(self, lecon_id: int) -> LeconInfo:
        ligne = self.conn.execute("SELECT * FROM lecons WHERE id = ?", (lecon_id,)).fetchone()
        if ligne is None:
            raise LeconErreur(f"Leçon {lecon_id} introuvable")
        valeurs = dict(ligne)
        valeurs["agents_concernes"] = json.loads(valeurs["agents_concernes"])
        return LeconInfo(**valeurs)

    def lister(self, statut: str) -> list[LeconInfo]:
        lignes = self.conn.execute("SELECT id FROM lecons WHERE statut = ? ORDER BY id", (statut,))
        return [self.lire(l["id"]) for l in lignes]

    def proposer(self, lecon: Lecon, origine_rejet: str, parent_id: int | None = None) -> int:
        """Crée la tâche de validation pour l'humain et la ligne « proposée »."""
        tache_id = self.file.creer(lecon, parent_id=parent_id)
        return self.conn.execute(
            "INSERT INTO lecons (tache_id, origine_rejet, erreur, cause, regle, agents_concernes, cree_le) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tache_id, origine_rejet, lecon.erreur, lecon.cause, lecon.regle,
             json.dumps(lecon.agents_concernes), self._date()),
        ).lastrowid

    def valider(self, lecon_id: int, regle: str | None = None) -> LeconInfo:
        """Valide la leçon, éventuellement avec une règle reformulée par l'humain."""
        lecon = self._proposee(lecon_id)
        regle = (regle or "").strip() or lecon.regle
        self.file.valider(lecon.tache_id)
        self.conn.execute("UPDATE lecons SET statut = 'validée', regle = ?, decide_le = ? WHERE id = ?",
                          (regle, self._date(), lecon_id))
        self.regenerer()
        return self.lire(lecon_id)

    def refuser(self, lecon_id: int, commentaire: str | None = None) -> LeconInfo:
        lecon = self._proposee(lecon_id)
        commentaire = (commentaire or "").strip() or "Leçon refusée."
        self.file.refuser(lecon.tache_id, commentaire)
        self.conn.execute("UPDATE lecons SET statut = 'refusée', commentaire = ?, decide_le = ? WHERE id = ?",
                          (commentaire, self._date(), lecon_id))
        return self.lire(lecon_id)

    def retirer(self, lecon_id: int) -> LeconInfo:
        """Retire une leçon validée qui ne convient plus : elle sort de lecons.md."""
        with transaction(self.conn):
            if self.lire(lecon_id).statut != "validée":
                raise LeconErreur(f"Leçon {lecon_id} : seule une leçon validée peut être retirée")
            self.conn.execute("UPDATE lecons SET statut = 'refusée', commentaire = 'Retirée.', decide_le = ? "
                              "WHERE id = ?", (self._date(), lecon_id))
        self.regenerer()
        return self.lire(lecon_id)

    def _proposee(self, lecon_id: int) -> LeconInfo:
        lecon = self.lire(lecon_id)
        if lecon.statut != "proposée":
            raise LeconErreur(f"Leçon {lecon_id} déjà traitée (statut : {lecon.statut})")
        return lecon

    def regenerer(self) -> Path:
        """Réécrit lecons.md avec les leçons validées, de la plus ancienne à la plus récente."""
        lignes = [ENTETE_LECONS]
        for lecon in self.lister("validée"):
            agents = ", ".join(lecon.agents_concernes)
            lignes.append(f"- Leçon {lecon.id} ({agents}) : {lecon.regle}")
        chemin = self.racine / "lecons.md"
        temporaire = chemin.with_name("lecons.md.partiel")
        temporaire.write_text("\n".join(lignes).rstrip() + "\n", encoding="utf-8")
        temporaire.replace(chemin)
        return chemin
