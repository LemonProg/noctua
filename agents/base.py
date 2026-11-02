"""Classe commune à tous les agents.

Un agent = une tâche. Avant chaque tâche, la charte et les leçons sont relues :
c'est imposé ici, pas laissé à chaque agent.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from noyau.claude import MoteurClaude
from noyau.config import RACINE, Config
from noyau.contexte import Contexte, lire
from noyau.couts import JournalCouts
from noyau.file import File, Tache
from noyau.stockage import Bibliotheque

# Statuts qu'un agent peut donner à sa tâche une fois traitée.
STATUTS_FIN = ("terminé", "à_valider")


@dataclass(frozen=True)
class Outils:
    """Ce dont un agent dispose pour traiter une tâche."""

    file: File
    biblio: Bibliotheque
    couts: JournalCouts
    dossier_travail: Path
    claude: MoteurClaude
    config: Config | None = None


class Agent:
    id: ClassVar[str]
    voie: ClassVar[str] = "normale"

    def __init__(self, racine: Path = RACINE):
        self.racine = racine

    def executer(self, tache: Tache, outils: Outils) -> str:
        contexte = lire(self.racine)
        statut = self.traiter(tache, contexte, outils)
        if statut not in STATUTS_FIN:
            raise ValueError(f"{self.id} a renvoyé un statut de fin invalide : {statut!r}")
        return statut

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        """Traite la tâche et renvoie « terminé » ou « à_valider ».

        Les messages suivants sont créés avec `outils.file.creer(..., parent_id=tache.id)`.
        """
        raise NotImplementedError
