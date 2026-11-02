"""agent_redacteur : écrit le texte d'une publication et produit le post_brouillon (texte, médias, réseau, horaire).

Deux entrées :
- brief_texte (+ rushs éventuels) : post de texte, avec les rushs du brief comme médias ;
- asset (visuel d'agent_visuel ou reel d'agent_video) : texte qui accompagne l'image ou la vidéo, d'après le brief
  d'origine. Pour un reel, Claude voit trois images extraites et le script (scènes et textes à l'écran).
Claude regarde les photos jointes (lecture limitée au dossier de travail).
Le brouillon passe par agent_relecteur avant la file de validation.
"""

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.claude import ClaudeErreur
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.images import reduire
from noyau.messages import Asset, BriefTexte, BriefVideo, BriefVisuel, PostBrouillon
from noyau.modeles import OPUS, Execution, execution
from noyau.stockage import AssetIntrouvable
from noyau.video import extraire_images

RELECTURE_AUTOMATIQUE = True  # agent_relecteur relit chaque post avant l'humain
PAR_DEFAUT = Execution(OPUS, "medium")

# Longueur maximale d'un texte de publication, par réseau (en caractères).
LIMITES_RESEAUX = {"instagram": 2200, "facebook": 63206, "linkedin": 3000, "tiktok": 2200, "x": 280}

SYSTEME = """Tu es agent_redacteur, le rédacteur de l'agence marketing de la marque décrite ci-dessous.
Ta seule tâche : écrire le texte d'une publication pour un réseau social, à partir d'un brief.

Règles :
- Respecte strictement la charte de marque et les leçons ci-dessous ; une leçon prime sur tes habitudes.
- N'invente rien : ni client, ni chiffre, ni prix, ni délai, ni résultat qui ne figure pas dans la charte ou le brief.
- Si des photos ou un visuel sont joints, regarde-les (outil Read) : le texte les accompagne et ne décrit rien qui n'y figure pas.
- Écris directement le texte final, prêt à publier, adapté au réseau (longueur, ton, hashtags éventuels).
- Pas de commentaire sur ton travail dans le texte.

# Charte de marque

{charte}

# Leçons tirées des contenus refusés

{lecons}
"""


class PostRedige(BaseModel):
    texte: str = Field(min_length=1, description="Texte final de la publication, prêt à publier")


def consigne(brief: BriefTexte | BriefVisuel | BriefVideo, medias: list[str], intention: str | None = None,
             script: str | None = None) -> str:
    lignes = [
        "Brief :",
        f"- Objectif : {brief.objectif}",
        f"- Format : {brief.format}",
        f"- Réseau : {brief.reseau}",
        f"- Publication prévue le : {brief.date_prevue:%d/%m/%Y à %H:%M}",
    ]
    limite = LIMITES_RESEAUX.get(brief.reseau.lower())
    if limite:
        lignes.append(f"- Longueur maximale du texte : {limite} caractères")
    if intention:
        lignes.append(f"- Intention du {'reel' if script else 'visuel'} créé pour ce post : {intention}")
    if script:
        lignes.append(f"- Script du reel (scènes et textes à l'écran) :\n{script}")
    if medias:
        lignes.append("- Médias joints :")
        lignes += [f"  - {m}" for m in medias]
    else:
        lignes.append("- Aucun média joint : texte seul.")
    lignes.append("\nÉcris le texte de la publication.")
    return "\n".join(lignes)


class AgentRedacteur(Agent):
    id = "agent_redacteur"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        script = None
        if tache.type == "brief_texte":
            brief, medias, intention = tache.message, tache.message.rushs, None
        elif tache.type == "asset":
            visuel: Asset = tache.message
            brief = outils.file.lire(visuel.brief_id).message
            medias, intention = [visuel.asset_id, *visuel.autres_slides], visuel.prompt_ou_script.split("\n", 1)[0]
            if visuel.type_media == "vidéo":
                script = visuel.prompt_ou_script.split("\n\nComposition :", 1)[0].split("\n\n", 1)[-1]
        else:
            raise ErreurDefinitive(f"agent_redacteur ne traite pas les messages {tache.type}")

        choix = execution(tache.message, PAR_DEFAUT)  # génération de contenu : Opus 5.5 imposé
        descriptions = [self._joindre(asset_id, outils) for asset_id in medias]
        post = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte, lecons=contexte.lecons),
            consigne=consigne(brief, descriptions, intention, script),
            schema=PostRedige,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            lecture=any("(regarde-l" in d for d in descriptions),
            modele=choix.modele,
            effort=choix.effort,
        )
        limite = LIMITES_RESEAUX.get(brief.reseau.lower())
        if limite and len(post.texte) > limite:
            raise ClaudeErreur(f"Texte trop long pour {brief.reseau} : {len(post.texte)} caractères (max {limite})")

        brouillon = PostBrouillon(texte=post.texte, medias=medias, reseau=brief.reseau, horaire_prevu=brief.date_prevue)
        outils.file.creer(brouillon, parent_id=tache.id, a_valider=not RELECTURE_AUTOMATIQUE)
        return "terminé"

    @staticmethod
    def _joindre(asset_id: int, outils: Outils) -> str:
        """Vérifie le média et, pour une photo, la copie dans le dossier de travail pour que Claude la voie."""
        try:
            asset = outils.biblio.lire(asset_id)
            source = outils.biblio.chemin_local(asset_id)
        except AssetIntrouvable:
            raise ErreurDefinitive(f"Média {asset_id} introuvable") from None
        if asset.etat != "actif":
            raise ErreurDefinitive(f"Média {asset_id} indisponible (état : {asset.etat})")
        nom = asset.nom_original or asset.chemin
        if asset.type == "vidéo":
            images = extraire_images(source, outils.dossier_travail, f"media-{asset_id}")
            return f"reel créé « {nom} » : images extraites {', '.join(images)} (regarde-les)"
        if asset.type != "image":
            return f"{asset.type} « {nom} »"
        fichier = f"media-{asset_id}.jpg"
        reduire(source, outils.dossier_travail / fichier)
        nature = "slide du visuel créé" if asset.origine == "généré" else "image"
        return f"{nature} « {nom} » : fichier {fichier} (regarde-le)"
