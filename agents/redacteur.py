"""agent_redacteur : écrit le texte d'une publication et produit le post_brouillon (texte, médias, réseau, horaire).

Deux entrées :
- brief_texte (+ rushs éventuels) : post de texte, avec les rushs du brief comme médias ;
- asset (visuel d'agent_visuel ou reel d'agent_video) : texte qui accompagne l'image ou la vidéo, d'après le brief
  d'origine. Pour un reel, Claude voit trois images extraites et le script (scènes et textes à l'écran) ;
- modification_texte (demande de l'humain, sans refus) : retouche du texte actuel du post selon sa demande, avec
  les mêmes médias et le même horaire ; le reste du texte est gardé.
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
from noyau.messages import Asset, BriefTexte, BriefVideo, BriefVisuel, ModificationTexte, PostBrouillon
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
             script: str | None = None, retouche: tuple[str, list[str]] | None = None) -> str:
    """`retouche` : (texte actuel, demandes de l'humain) pour une modification du texte seulement."""
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
    if retouche:
        texte, demandes = retouche
        lignes.append(f"\nTexte actuel de la publication :\n{texte}")
        lignes.append("\nL'humain demande de retoucher ce texte (le visuel ne change pas) :")
        lignes += [f"- {d}" for d in demandes]
        lignes.append("\nRécris le texte en appliquant exactement cette demande. Garde tel quel tout ce qu'elle "
                      "ne touche pas.")
    else:
        lignes.append("\nÉcris le texte de la publication.")
    return "\n".join(lignes)


class AgentRedacteur(Agent):
    id = "agent_redacteur"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        retouche, post = None, None
        if tache.type == "modification_texte":
            demande: ModificationTexte = tache.message
            post = outils.file.lire(demande.post_id)
            brief, _, intention, script = self._source(source_du_post(post, outils), outils)
            medias = post.contenu["medias"]  # les médias du post, gardés tels quels
            retouche = (post.contenu["texte"], demande.consignes)
        else:
            brief, medias, intention, script = self._source(tache, outils)

        choix = execution(tache.message, PAR_DEFAUT)  # génération de contenu : Opus 5.5 imposé
        descriptions = [self._joindre(asset_id, outils) for asset_id in medias]
        redige = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte, lecons=contexte.lecons),
            consigne=consigne(brief, descriptions, intention, script, retouche),
            schema=PostRedige,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            lecture=any("(regarde-l" in d for d in descriptions),
            modele=choix.modele,
            effort=choix.effort,
        )
        limite = LIMITES_RESEAUX.get(brief.reseau.lower())
        if limite and len(redige.texte) > limite:
            raise ClaudeErreur(f"Texte trop long pour {brief.reseau} : {len(redige.texte)} caractères (max {limite})")

        if post is not None:
            # Retouche : même horaire (peut-être réglé par l'humain) et même étiquette « créé avec l'IA ».
            brouillon = PostBrouillon(texte=redige.texte, medias=medias, reseau=post.contenu["reseau"],
                                      horaire_prevu=post.contenu["horaire_prevu"],
                                      etiquette_ia=post.contenu.get("etiquette_ia", False))
        else:
            brouillon = PostBrouillon(texte=redige.texte, medias=medias, reseau=brief.reseau,
                                      horaire_prevu=brief.date_prevue)
        outils.file.creer(brouillon, parent_id=tache.id, a_valider=not RELECTURE_AUTOMATIQUE)
        return "terminé"

    @staticmethod
    def _source(tache: Tache, outils: Outils) -> tuple[BriefTexte | BriefVisuel | BriefVideo, list[int],
                                                        str | None, str | None]:
        """Brief, médias, intention et script (reel) d'une tâche brief_texte ou asset."""
        if tache.type == "brief_texte":
            return tache.message, tache.message.rushs, None, None
        if tache.type == "asset":
            visuel: Asset = tache.message
            brief = outils.file.lire(visuel.brief_id).message
            script = None
            if visuel.type_media == "vidéo":
                script = visuel.prompt_ou_script.split("\n\nComposition :", 1)[0].split("\n\n", 1)[-1]
            return (brief, [visuel.asset_id, *visuel.autres_slides], visuel.prompt_ou_script.split("\n", 1)[0],
                    script)
        raise ErreurDefinitive(f"agent_redacteur ne traite pas les messages {tache.type}")

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


def source_du_post(post: Tache, outils: Outils) -> Tache:
    """Tâche (brief_texte ou asset) dont le texte du post est issu, en remontant les retouches successives."""
    parent_id = post.parent_id
    while parent_id:
        parent = outils.file.lire(parent_id)
        if parent.type in ("brief_texte", "asset"):
            return parent
        parent_id = parent.parent_id
    raise ErreurDefinitive(f"Origine du post {post.id} introuvable")
