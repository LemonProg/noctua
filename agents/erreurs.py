"""agent_erreurs : analyse chaque contenu rejeté et propose une leçon pour ne pas reproduire l'erreur.

Entrée : contenu_rejete (refus humain, « ne pas utiliser », corrections du relecteur) + charte + leçons.
Sortie : une leçon proposée, à valider par l'humain. Les médias produits du contenu sont ensuite
marqués « analysés », ce qui permet de les purger de la corbeille.
"""

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.lecons import Lecons
from noyau.messages import Agent as IdAgent
from noyau.messages import ContenuRejete, Lecon
from noyau.modeles import SONNET, Execution, execution
from noyau.stockage import StockageErreur

# Analyse d'un rejet : pas de génération de contenu, Sonnet suffit (l'émetteur peut choisir un autre modèle).
PAR_DEFAUT = Execution(SONNET, "medium")

ORIGINES = {
    "refus_humain": "refusé par l'humain, qui demande une nouvelle version",
    "ne_pas_utiliser": "écarté par l'humain (« ne pas utiliser ») : il ne sera pas réécrit",
    "relecteur": "renvoyé par agent_relecteur avec des corrections, avant d'arriver à l'humain",
}

SYSTEME = """Tu es agent_erreurs. Ta seule tâche : comprendre pourquoi un contenu produit par l'agence
a été rejeté, et proposer UNE leçon pour que l'erreur ne se reproduise pas.

Règles :
- Pars du commentaire ou des corrections : c'est la parole de l'humain ou du relecteur, elle fait foi.
- Distingue l'erreur constatée (ce qui ne va pas dans ce contenu) de sa cause probable (brief ambigu,
  charte incomplète, mauvaise habitude de l'agent…).
- La règle doit être générale, réutilisable pour les prochains contenus, courte, à l'impératif,
  et vérifiable. Pas de règle qui ne vaut que pour ce post.
- Choisis les agents concernés parmi : orchestrateur_marketing, agent_visuel, agent_video, agent_redacteur,
  agent_analyste, orchestrateur_prospection, agent_recherche, agent_message, agent_suivi, agent_relecteur.
- Si une leçon existante couvre déjà cette erreur, indique son numéro dans `rappel_de`.
- Si le rejet ne donne aucune raison, propose l'hypothèse la plus probable et dis-le dans la cause.

# Charte de marque

{charte}

# Leçons déjà validées

{lecons}
"""


class AnalyseRejet(BaseModel):
    erreur: str = Field(min_length=1, description="Ce qui ne va pas dans ce contenu, en une ou deux phrases")
    cause: str = Field(min_length=1, description="Cause probable de l'erreur")
    regle: str = Field(min_length=1, max_length=300, description="Règle générale à appliquer désormais")
    agents_concernes: list[IdAgent] = Field(min_length=1)
    rappel_de: int | None = Field(None, description="Numéro d'une leçon validée qui couvre déjà cette erreur")


def consigne(rejet: ContenuRejete) -> str:
    lignes = [f"Contenu {ORIGINES[rejet.origine]}.", "", "Contenu rejeté :"]
    for cle, valeur in rejet.contenu.items():
        lignes.append(f"- {cle} : {valeur}")
    if rejet.brief:
        lignes += ["", "Brief d'origine :"] + [f"- {cle} : {valeur}" for cle, valeur in rejet.brief.items()]
    if rejet.commentaire:
        lignes += ["", f"Commentaire de l'humain : {rejet.commentaire}"]
    if rejet.corrections:
        lignes += ["", "Corrections du relecteur :"] + [f"- {c}" for c in rejet.corrections]
    lignes += ["", "Analyse ce rejet et propose une leçon."]
    return "\n".join(lignes)


class AgentErreurs(Agent):
    id = "agent_erreurs"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type != "contenu_rejete":
            raise ErreurDefinitive(f"agent_erreurs ne traite pas les messages {tache.type}")
        rejet = tache.message
        choix = execution(rejet, PAR_DEFAUT)
        lecons = Lecons(outils.file.conn, outils.file, self.racine, outils.file.horloge)
        validees = lecons.lister("validée")

        analyse = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte, lecons=contexte.lecons),
            consigne=consigne(rejet),
            schema=AnalyseRejet,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            modele=choix.modele,
            effort=choix.effort,
        )
        rappel = analyse.rappel_de if analyse.rappel_de in {l.id for l in validees} else None
        lecons.proposer(
            Lecon(contenu_id=rejet.contenu_id, erreur=analyse.erreur, cause=analyse.cause, regle=analyse.regle,
                  agents_concernes=analyse.agents_concernes, rappel_de=rappel),
            rejet.origine, parent_id=tache.id,
        )

        for asset_id in rejet.contenu.get("medias", []):
            try:
                outils.biblio.marquer_analyse(asset_id)
            except StockageErreur:
                pass
        return "terminé"
