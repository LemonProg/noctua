"""Modèles Claude utilisables et choix du modèle et de l'effort pour chaque tâche.

Tout passe par l'abonnement : le quota est la ressource à ménager. L'orchestrateur choisit, pour chaque
tâche qu'il confie, le modèle le plus adapté (de Haiku 5.5 pour le simple à Opus 5.5 pour le complexe)
et l'effort. Seule exception : la génération de contenu (textes, visuels, vidéos) se fait toujours avec Opus 5.5.
"""

from dataclasses import dataclass

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
MODELES = (HAIKU, SONNET, OPUS)
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Messages qui commandent une génération de contenu : Opus 5.5 imposé.
TYPES_CONTENU = frozenset({"brief_texte", "brief_visuel", "brief_video", "asset", "modification_texte"})

# Repères pour l'orchestrateur (phase 4), repris dans docs/architecture.md.
GUIDE_DU_CHOIX = """- claude-haiku-5-5 : tâches simples et courtes (tri, classement, extraction, vérification mécanique).
- claude-sonnet-5-5 : analyse et rédaction internes de difficulté moyenne (analyse d'un rejet, relecture,
  synthèse de statistiques, veille courante).
- claude-opus-5-5 : tâches complexes ou à fort enjeu (stratégie, direction artistique) et, obligatoirement,
  toute génération de contenu (textes, visuels, vidéos).
- Effort : low pour le mécanique, medium par défaut, high pour la création et le raisonnement difficile ;
  xhigh et max seulement si la qualité l'exige vraiment."""


@dataclass(frozen=True)
class Execution:
    modele: str
    effort: str


def execution(message, defaut: Execution) -> Execution:
    """Modèle et effort d'une tâche : ceux choisis par l'émetteur du message, sinon ceux de l'agent.
    La génération de contenu reste sur Opus 5.5 quoi qu'il arrive."""
    modele = getattr(message, "modele", None) or defaut.modele
    if message.type in TYPES_CONTENU:
        modele = OPUS
    return Execution(modele, getattr(message, "effort", None) or defaut.effort)
