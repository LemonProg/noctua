"""Agent factice pour vérifier le circuit de la file, sans appel d'API.

Activé uniquement par `python -m noyau.worker --agent-factice`. Il se fait passer pour
agent_redacteur : il écrit un fichier de travail, puis met le brief dans la file de validation.
"""

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.file import Tache


class AgentFactice(Agent):
    id = "agent_redacteur"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        (outils.dossier_travail / "brouillon.txt").write_text(f"Brouillon factice pour la tâche {tache.id}\n")
        return "à_valider"
