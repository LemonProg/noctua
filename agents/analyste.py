"""agent_analyste : rapport hebdomadaire sur les résultats des posts publiés.

Entrée : demande_analyse (chaque dimanche à 19 h, avant la veille et le planning, ou bouton de l'interface).
Il reçoit les 30 derniers posts du compte avec leurs chiffres, croisés avec ce que l'agence sait de
chacun (format, visuel créé ou photos, objectif du brief, jour et heure), les moyennes du compte et
son rapport précédent. Sortie : un rapport enregistré (contenu du message `rapport`), lu par
l'orchestrateur au moment du planning, comme le rapport de veille.

Pas d'appel à Claude sans nouvelle statistique depuis le dernier rapport (sauf demande forcée).
"""

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import File, Tache
from noyau.messages import DemandeAnalyse, Periode, Rapport
from noyau.modeles import SONNET, Execution, execution
from noyau.publication import Publications
from noyau.statistiques import CHIFFRES, Statistiques, engagement

FUSEAU = ZoneInfo("Europe/Paris")
PAR_DEFAUT = Execution(SONNET, "medium")
MAX_POSTS = 30
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

SYSTEME = """Tu es agent_analyste, l'analyste des résultats Instagram de la marque décrite ci-dessous.
Ta seule tâche : lire les chiffres des derniers posts et en tirer un rapport court et actionnable pour
orchestrateur_marketing, qui prépare le calendrier de la semaine suivante.

Méthode :
- Compare chaque post aux moyennes du compte. Le taux d'engagement (j'aime, commentaires, partages et
  enregistrements rapportés à la couverture) compte plus que les vues brutes ; les enregistrements et
  partages signalent un contenu utile, les abonnés gagnés un contenu qui convertit.
- Croise les chiffres avec ce qui varie d'un post à l'autre : format (image, carrousel, reel), visuel créé
  ou photo réelle, sujet et angle, jour et heure de publication.
- Peu de posts : reste prudent, parle de pistes et non de certitudes, et dis-le.
- Lisibilité : chaque élément de liste commence par une phrase courte (12 mots au plus) qui le résume,
  suivie d'un point, puis le détail ; la synthèse tient en 3 à 5 phrases courtes.
- Chiffres clés : 4 à 6 valeurs courtes et parlantes (moyennes, meilleur format, meilleur créneau…) ;
  les explications vont dans la synthèse.
- Recommandations concrètes et vérifiables pour la semaine suivante (formats, sujets, horaires, rushs),
  cohérentes avec la charte. N'invente aucun chiffre : n'utilise que ceux fournis.

# Charte de marque

{charte}
"""


class ChiffreCle(BaseModel):
    nom: str = Field(min_length=1, max_length=40, description="Ex. couverture moyenne, meilleur format")
    valeur: str = Field(min_length=1, max_length=30,
                        description="Valeur courte : un chiffre ou quelques mots (ex. 7,5 %, carrousel). "
                                    "Le détail va dans la synthèse.")


class Analyse(BaseModel):
    synthese: str = Field(min_length=1, description="Synthèse en quelques phrases, pour l'humain")
    chiffres_cles: list[ChiffreCle] = Field(min_length=1, max_length=8)
    ce_qui_marche: list[str] = Field(default_factory=list)
    ce_qui_ne_marche_pas: list[str] = Field(default_factory=list)
    recommandations: list[str] = Field(min_length=1, max_length=6)


def brief_du_post(file: File, post_id: int) -> Tache | None:
    """Brief d'origine d'un post (en remontant : post ← visuel ← brief)."""
    tache = file.lire(post_id)
    while tache.parent_id:
        tache = file.lire(tache.parent_id)
        if tache.type.startswith("brief_"):
            return tache
    return None


def moyenne(valeurs: list) -> float | None:
    valeurs = [v for v in valeurs if v is not None]
    return round(sum(valeurs) / len(valeurs), 1) if valeurs else None


class AgentAnalyste(Agent):
    id = "agent_analyste"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type != "demande_analyse":
            raise ErreurDefinitive(f"agent_analyste ne traite pas les messages {tache.type}")
        demande: DemandeAnalyse = tache.message
        conn = outils.file.conn
        statistiques = Statistiques(outils.file, Publications(outils.file, outils.biblio), outils.file.horloge)
        releve = statistiques.dernier_releve()
        precedent = conn.execute("SELECT contenu, cree_le FROM rapports_analyste ORDER BY id DESC LIMIT 1").fetchone()
        if releve is None or (precedent and releve <= precedent["cree_le"] and not demande.forcer):
            return "terminé"  # rien de nouveau : l'abonnement n'est pas sollicité

        lignes = statistiques.lister(MAX_POSTS)
        tous = statistiques.lister()
        moyennes = {c: moyenne([l[c] for l in tous]) for c in CHIFFRES}
        moyennes["engagement_pourcent"] = moyenne([engagement(l) for l in tous])
        consigne = [
            f"Rapport pour préparer la semaine du {demande.semaine:%d/%m/%Y}.",
            f"Moyennes du compte sur {len(tous)} posts : " + json.dumps(moyennes, ensure_ascii=False),
            "Derniers posts, du plus récent au plus ancien :\n"
            + json.dumps([self._decrire(l, outils.file) for l in lignes], ensure_ascii=False, indent=1),
        ]
        if precedent:
            ancien = json.loads(precedent["contenu"])
            consigne.append("Ton rapport précédent : " + (ancien.get("synthese") or "")
                            + "\nRecommandations : " + " ; ".join(ancien["recommandations"]))

        choix = execution(demande, PAR_DEFAUT)
        analyse = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte),
            consigne="\n\n".join(consigne),
            schema=Analyse,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            modele=choix.modele,
            effort=choix.effort,
        )
        dates = [datetime.fromisoformat(l["publie_le"]).astimezone(FUSEAU).date() for l in lignes if l["publie_le"]]
        rapport = Rapport(periode=Periode(debut=min(dates), fin=max(dates)) if dates else
                          Periode(debut=demande.semaine, fin=demande.semaine),
                          chiffres_cles={c.nom: c.valeur for c in analyse.chiffres_cles},
                          recommandations=analyse.recommandations, synthese=analyse.synthese,
                          ce_qui_marche=analyse.ce_qui_marche, ce_qui_ne_marche_pas=analyse.ce_qui_ne_marche_pas)
        conn.execute("INSERT INTO rapports_analyste (tache_id, contenu, cree_le) VALUES (?, ?, ?)",
                     (tache.id, rapport.model_dump_json(), outils.file.horloge().astimezone(UTC).isoformat()))
        return "terminé"

    @staticmethod
    def _decrire(ligne, file: File) -> dict:
        publie = datetime.fromisoformat(ligne["publie_le"]).astimezone(FUSEAU) if ligne["publie_le"] else None
        post = {
            "publie": f"{JOURS[publie.weekday()]} {publie:%d/%m/%Y %H:%M}" if publie else "inconnu",
            "type": ligne["type"],
            "legende": (ligne["description"] or "")[:300],
            **{c: ligne[c] for c in CHIFFRES if ligne[c] is not None},
            "engagement_pourcent": engagement(ligne),
        }
        if ligne["duree_s"]:
            post["duree_s"] = ligne["duree_s"]
        if ligne["post_id"]:
            brief = brief_du_post(file, ligne["post_id"])
            post["agence"] = {
                "visuel": "créé par le graphiste" if brief and brief.type == "brief_visuel" else "photos réelles",
                "format": brief.contenu["format"] if brief else None,
                "objectif": brief.contenu["objectif"][:400] if brief else None,
                "rushs_utilises": len(brief.contenu.get("rushs", [])) if brief else 0,
            }
        else:
            post["agence"] = "post publié hors de l'agence"
        return post
