"""agent_relecteur : relit chaque post_brouillon avant l'humain.

Vérifie la charte, les fautes, les faits (rien d'inventé), la cohérence entre le texte et le visuel, et les leçons.
- Conforme : le post passe dans la file de validation de l'humain.
- Non conforme : corrections vers l'orchestrateur (qui relance le brief), contenu_rejete vers agent_erreurs
  (leçon), médias produits en corbeille. Au-delà de MAX_ALLERS_RETOURS, le post est soumis à l'humain tel quel,
  pour ménager le quota.
"""

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.images import reduire
from noyau.video import extraire_images
from noyau.messages import ContenuRejete, Corrections, PostBrouillon
from noyau.modeles import SONNET, Execution, execution
from noyau.stockage import StockageErreur

PAR_DEFAUT = Execution(SONNET, "medium")
MAX_ALLERS_RETOURS = 2

SYSTEME = """Tu es agent_relecteur, le relecteur de l'agence marketing de la marque décrite ci-dessous.
Ta seule tâche : relire un post (texte et visuels) avant qu'il soit soumis au client, et dire s'il est conforme.

Vérifie :
- la charte (ton, vouvoiement, interdits, zone, offres et prix exacts) et les leçons ;
- l'orthographe, la grammaire, la typographie française ;
- les faits : aucun chiffre, client, délai, résultat ou promesse qui ne figure pas dans la charte ou le brief ;
- la cohérence entre le texte et les visuels (regarde-les avec l'outil Read) ;
- la longueur adaptée au réseau.

Sois exigeant mais juste : ne signale que de vrais problèmes, chacun précis et actionnable.

# Charte de marque

{charte}

# Leçons tirées des contenus refusés

{lecons}
"""


class Relecture(BaseModel):
    conforme: bool
    problemes: list[str] = Field(default_factory=list)


class AgentRelecteur(Agent):
    id = "agent_relecteur"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type != "post_brouillon":
            raise ErreurDefinitive(f"agent_relecteur ne traite pas encore les messages {tache.type}")
        post: PostBrouillon = tache.message
        choix = execution(post, PAR_DEFAUT)

        fichiers, reel = [], None
        for asset_id in post.medias:
            try:
                asset = outils.biblio.lire(asset_id)
                if asset.type == "image":
                    fichier = f"media-{asset_id}.jpg"
                    reduire(outils.biblio.chemin_local(asset_id), outils.dossier_travail / fichier, cote_max=1080)
                    fichiers.append(fichier)
                elif asset.type == "vidéo":
                    reel = extraire_images(outils.biblio.chemin_local(asset_id), outils.dossier_travail,
                                           f"media-{asset_id}")
            except StockageErreur:
                continue
        consigne = (f"Réseau : {post.reseau}. Publication prévue le {post.horaire_prevu:%d/%m/%Y à %H:%M}.\n\n"
                    f"Texte du post :\n{post.texte}\n\n")
        if reel:
            # Un reel n'est pas un carrousel : trois images tirées d'une seule vidéo, et son script.
            consigne += ("Média : un reel (vidéo verticale en motion design, muette). Ce n'est pas un carrousel : "
                         "voici trois images extraites de la même vidéo, au début, au milieu et à la fin : "
                         + ", ".join(reel) + ". Le texte à l'écran change d'une scène à l'autre ; ne reproche pas "
                         "l'absence d'une scène entre ces images. Script du reel :\n" + script_du_reel(tache, outils))
        else:
            consigne += "Visuels (dans l'ordre) : " + ", ".join(fichiers) if fichiers else "Aucun visuel."
        relecture = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte, lecons=contexte.lecons),
            consigne=consigne,
            schema=Relecture,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            modele=choix.modele,
            effort=choix.effort,
            lecture=bool(fichiers),
        )
        if relecture.conforme or not relecture.problemes:
            return "à_valider"
        if allers_retours(outils, tache) >= MAX_ALLERS_RETOURS:
            return "à_valider"  # le quota est ménagé : l'humain tranche

        for asset_id in post.medias:
            try:
                if outils.biblio.lire(asset_id).origine == "généré":
                    outils.biblio.mettre_en_corbeille(asset_id, "relecteur")
            except StockageErreur:
                pass
        outils.file.creer(Corrections(contenu_id=tache.id, problemes=relecture.problemes, origine="relecteur"),
                          agent="orchestrateur_marketing", parent_id=tache.id)
        outils.file.creer(ContenuRejete(contenu_id=tache.id, origine="relecteur", contenu=tache.contenu,
                                        corrections=relecture.problemes), parent_id=tache.id)
        return "terminé"


def script_du_reel(tache: Tache, outils: Outils) -> str:
    """Script (intention et scènes) porté par le message `asset` dont le post est issu, en remontant les
    retouches de texte demandées par l'humain (le reel, lui, n'a pas changé)."""
    parent = outils.file.lire(tache.parent_id) if tache.parent_id else None
    while parent is not None and parent.type in ("modification_texte", "post_brouillon"):
        parent = outils.file.lire(parent.parent_id) if parent.parent_id else None
    if parent is None or parent.type != "asset":
        return "(non disponible)"
    return parent.contenu["prompt_ou_script"].split("\n\nComposition :", 1)[0]


def allers_retours(outils: Outils, tache: Tache) -> int:
    """Nombre de corrections du relecteur déjà demandées dans la lignée de ce post."""
    nombre, parent_id = 0, tache.parent_id
    while parent_id:
        parent = outils.file.lire(parent_id)
        if parent.type == "corrections" and parent.contenu.get("origine") == "relecteur":
            nombre += 1
        parent_id = parent.parent_id
    return nombre
