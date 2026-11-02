"""agent_visuel : le graphiste de l'agence.

- brief_visuel → une image ou un carrousel. Claude écrit chaque slide en HTML/CSS selon la direction artistique,
  les références de style, la charte et les leçons ; les photos des rushs sont placées telles quelles.
  Chromium rend chaque slide en PNG ; Claude regarde les rendus, les critique comme un directeur artistique et
  corrige (deux fois au plus). Les slides sont enregistrées et un message `asset` part vers agent_redacteur.
- analyse_style → une direction artistique proposée à l'humain, tirée de ses réalisations de référence.
"""

import json
import re

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.direction import Directions
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.images import FORMAT_PAR_DEFAUT, FORMATS_RESEAUX, reduire
from noyau.messages import AnalyseStyle, Asset, BriefVisuel
from noyau.modeles import OPUS, Execution, execution
from noyau.references import References
from noyau.rendu import elements_marque, polices_disponibles, rendre
from noyau.stockage import AssetIntrouvable, StockageErreur

MAX_SLIDES = 10
MAX_CORRECTIONS = 2
MAX_REFERENCES_MONTREES = 6
PAR_DEFAUT = Execution(OPUS, "high")  # conception et analyse de style
EFFORT_RELECTURE = "medium"

SYSTEME = """Tu es agent_visuel, le graphiste senior de l'agence marketing de la marque décrite ci-dessous.
Ta seule tâche : concevoir le visuel d'une publication (une image, ou les slides d'un carrousel) en HTML/CSS.
Le résultat doit être professionnel, au niveau des réalisations de référence de la marque : jamais d'esthétique
« affiche générée par IA ».

Contraintes techniques :
- Chaque slide fait exactement {largeur} × {hauteur} px. Tu fournis, pour chaque slide, le contenu du <body>
  (`html`) et sa feuille de style (`css`). Pas de script, pas de ressource externe (ni police, ni image en ligne).
- Polices disponibles : {polices} (font-family par leur nom). Rien d'autre.
- Photos : uniquement les rushs fournis, cités par leur nom de fichier (ex. <img src="rush-12.jpg">), utilisés
  tels quels (recadrage avec object-fit et object-position ; aucun filtre qui dénature la photo).
- Sans rush : composition graphique (typographie, couleurs, formes simples), jamais de fausse photo.
- Signature : celle de la direction artistique (monogramme typographique ou logo). Les fichiers de la marque
  (logo…) sont fournis à côté de la page et cités par leur nom ; un logo n'est jamais déformé.
- Mot surligné : la ponctuation qui suit un mot surligné colle au mot (« soif. », jamais « soif . ») : surlignage en
  padding asymétrique (environ 0,14 em à gauche, 0,04 em au plus à droite, jamais de padding symétrique ni
  de marge), ponctuation à l'extérieur, juste après l'élément surligné, sans espace ni marge.

Règles :
- Suis la direction artistique et les leçons ; elles priment sur tes habitudes. Regarde les références (outil Read)
  pour retrouver leur niveau de finition : marges, hiérarchie, respiration, alignements.
- Textes dans l'image : courts, exacts, en français, sans faute, conformes à la charte (aucune promesse, aucun prix
  ou chiffre qui n'y figure pas).
- Un carrousel quand le brief le demande ou quand le message s'y prête (2 à {max_slides} slides) ; sinon une seule.

# Direction artistique

{direction}

# Charte de marque

{charte}

# Leçons tirées des contenus refusés

{lecons}
"""

SYSTEME_CRITIQUE = """Tu es directeur artistique senior. Tu relis les visuels qu'un graphiste vient de produire
pour une publication, avant qu'ils soient soumis au client. Regarde chaque rendu (outil Read), compare-le aux
références et à la direction artistique, et sois exigeant : un visuel qui ferait « amateur » ou « IA » n'est pas
conforme.

Vérifie : textes lisibles, exacts et sans faute ; rien de coupé ou qui déborde ; marges et alignements nets ;
hiérarchie claire ; ponctuation collée au mot surligné (aucun écart visible avant « . » ou « ? ») ;
photos bien cadrées et non déformées ; cohérence avec la direction artistique et la charte ;
cohérence entre les slides d'un carrousel.

Réponds `conforme: true` si tout est bon. Sinon, `conforme: false` et la liste des problèmes : chacun précis,
localisé (slide, élément) et actionnable par le graphiste. Ne réécris pas les slides toi-même.

# Direction artistique

{direction}

# Charte de marque

{charte}
"""

SYSTEME_ANALYSE = """Tu es directeur artistique senior. Tu analyses les réalisations de référence d'une marque
(ses meilleurs posts Instagram) pour écrire la direction artistique qu'un graphiste suivra ensuite pour créer
de nouveaux posts en HTML/CSS, au format 1080 px de large. Le but : des visuels professionnels, cohérents
avec ces réalisations, jamais d'esthétique « affiche générée par IA ».

Regarde chaque référence avec l'outil Read, puis rédige la direction artistique en Markdown, concrète,
mesurable et réutilisable, avec ces sections :
1. Impression d'ensemble (ce qui fait l'identité, en quelques phrases)
2. Grille et marges (en pixels pour 1080 de large, alignements)
3. Typographie (polices disponibles : {polices} ; hiérarchie des tailles en px, graisses, interlignage, casse, longueur des textes)
4. Couleurs (codes hexadécimaux observés et rôle de chacun)
5. Traitement des photos (cadrage, place dans la composition, lumière, aucun filtre artificiel)
6. Compositions types (4 à 6 gabarits décrits précisément, réutilisables)
7. Carrousels (structure, rythme, première et dernière slide)
8. À éviter

Appuie-toi sur ce que tu vois réellement dans les références et sur la charte ci-dessous ; n'invente pas un style.

# Charte de marque

{charte}
"""


class DirectionProposee(BaseModel):
    texte: str = Field(min_length=1, description="Direction artistique complète, en Markdown")
    resume: str = Field(min_length=1, description="Résumé en une ou deux phrases, pour l'humain")


# Espace (souvent un retour à la ligne du HTML) entre une balise fermante et « . », « , » ou « … » :
# rendue comme « soif . ». L'espace avant « ? ; : ! » reste, c'est la règle française.
ESPACE_AVANT_POINT = re.compile(r"(</[a-z][a-z0-9]*>)\s+(?=[.,…])")


def coller_ponctuation(html: str) -> str:
    return ESPACE_AVANT_POINT.sub(r"\1", html)


class Slide(BaseModel):
    html: str = Field(min_length=1, description="Contenu du <body> de la slide")
    css: str = Field(description="Feuille de style de la slide")


class Conception(BaseModel):
    intention: str = Field(min_length=1, description="En une phrase, en français : ce que montre le visuel")
    slides: list[Slide] = Field(min_length=1, max_length=MAX_SLIDES)


class Critique(BaseModel):
    conforme: bool
    problemes: list[str] = Field(default_factory=list)


def consigne(brief: BriefVisuel, largeur: int, hauteur: int, rushs: list[str], references: list[str]) -> str:
    lignes = [
        "Brief :",
        f"- Objectif : {brief.objectif}",
        f"- Format : {brief.format}",
        f"- Réseau : {brief.reseau} ({largeur} × {hauteur} px par slide)",
    ]
    if rushs:
        lignes.append("- Photos à utiliser (rushs, dans le dossier courant) : " + ", ".join(rushs))
    else:
        lignes.append("- Aucune photo : composition graphique.")
    if references:
        lignes.append("- Réalisations de référence à regarder pour le niveau attendu : " + ", ".join(references))
    lignes.append("\nConçois le visuel.")
    return "\n".join(lignes)


class AgentVisuel(Agent):
    id = "agent_visuel"
    voie = "rendu"  # rendus Chromium un à la fois, sans bloquer la rédaction des textes

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type == "analyse_style":
            return self._analyser_style(tache, contexte, outils)
        if tache.type != "brief_visuel":
            raise ErreurDefinitive(f"agent_visuel ne traite pas les messages {tache.type}")
        brief: BriefVisuel = tache.message
        choix = execution(brief, PAR_DEFAUT)  # génération de contenu : Opus 5.5 imposé
        largeur, hauteur = FORMATS_RESEAUX.get(brief.reseau.lower(), FORMAT_PAR_DEFAUT)
        rushs = self._preparer_rushs(brief, outils)
        references = self._preparer_references(outils)
        dossier = outils.dossier_travail

        contraintes = {"largeur": largeur, "hauteur": hauteur, "polices": ", ".join(polices_disponibles()),
                       "direction": contexte.direction or "(pas encore de direction validée : inspire-toi des références et de la charte)",
                       "charte": contexte.charte}
        conception = outils.claude.demander(
            systeme=SYSTEME.format(lecons=contexte.lecons, max_slides=MAX_SLIDES, **contraintes),
            consigne=consigne(brief, largeur, hauteur, rushs, references),
            schema=Conception, dossier_travail=dossier, effort=choix.effort, tache_id=tache.id, lecture=True,
            modele=choix.modele,
        )
        slides, problemes = conception.slides, []
        rendus = self._rendre(slides, largeur, hauteur, dossier)
        for _ in range(MAX_CORRECTIONS):
            # Relecture courte (verdict et problèmes seulement) ; une correction n'est demandée que si nécessaire.
            critique = outils.claude.demander(
                systeme=SYSTEME_CRITIQUE.format(**contraintes),
                consigne="Rendus à relire : " + ", ".join(r.name for r in rendus)
                         + ("\nRéférences : " + ", ".join(references) if references else ""),
                schema=Critique, dossier_travail=dossier, effort=EFFORT_RELECTURE, tache_id=tache.id, lecture=True,
                modele=choix.modele,
            )
            if critique.conforme or not critique.problemes:
                break
            problemes += critique.problemes
            slides = outils.claude.demander(
                systeme=SYSTEME.format(lecons=contexte.lecons, max_slides=MAX_SLIDES, **contraintes),
                consigne=consigne(brief, largeur, hauteur, rushs, references)
                         + "\n\nVoici tes slides actuelles (HTML/CSS) :\n"
                         + json.dumps([s.model_dump() for s in slides], ensure_ascii=False)
                         + "\n\nLe directeur artistique demande ces corrections :\n"
                         + "\n".join(f"- {p}" for p in critique.problemes)
                         + "\n\nRenvoie toutes les slides corrigées, dans l'ordre.",
                schema=Conception, dossier_travail=dossier, effort=choix.effort, tache_id=tache.id, lecture=True,
                modele=choix.modele,
            ).slides
            rendus = self._rendre(slides, largeur, hauteur, dossier)

        assets = [outils.biblio.ajouter(rendu.rename(dossier / f"visuel-{tache.id}-{i}.png"), "généré", tache_id=tache.id)
                  for i, rendu in enumerate(rendus, start=1)]
        resume = conception.intention
        if problemes:
            resume += "\n\nCorrections faites après relecture : " + " ; ".join(problemes)
        outils.file.creer(Asset(brief_id=tache.id, type_media="image", asset_id=assets[0], autres_slides=assets[1:],
                                prompt_ou_script=resume + "\n\nHTML/CSS : "
                                + json.dumps([s.model_dump() for s in slides], ensure_ascii=False)),
                          parent_id=tache.id)
        return "terminé"

    @staticmethod
    def _rendre(slides: list[Slide], largeur: int, hauteur: int, dossier) -> list:
        return [rendre(coller_ponctuation(s.html), s.css, dossier / f"rendu-{i}.png", largeur, hauteur, dossier)
                for i, s in enumerate(slides, start=1)]

    @staticmethod
    def _preparer_rushs(brief: BriefVisuel, outils: Outils) -> list[str]:
        """Copies réduites des rushs dans le dossier de travail (seul dossier lisible par Claude et Chromium)."""
        fichiers = []
        for asset_id in brief.rushs:
            try:
                asset = outils.biblio.lire(asset_id)
                source = outils.biblio.chemin_local(asset_id)
            except AssetIntrouvable:
                raise ErreurDefinitive(f"Rush {asset_id} introuvable") from None
            if asset.etat != "actif":
                raise ErreurDefinitive(f"Rush {asset_id} indisponible (état : {asset.etat})")
            if asset.type != "image":
                raise ErreurDefinitive(f"Rush {asset_id} : seules les photos servent à un visuel (les vidéos vont aux reels)")
            fichier = f"rush-{asset_id}.jpg"
            reduire(source, outils.dossier_travail / fichier)
            fichiers.append(fichier)
        return fichiers

    @staticmethod
    def _preparer_references(outils: Outils) -> list[str]:
        references = References(outils.file.conn, outils.biblio.stockage, outils.biblio.dossier_temp)
        fichiers = []
        for reference in references.lister()[-MAX_REFERENCES_MONTREES:]:
            fichier = f"reference-{reference.id}.jpg"
            reduire(references.chemin_local(reference.id), outils.dossier_travail / fichier, cote_max=1080)
            fichiers.append(fichier)
        return fichiers

    def _analyser_style(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        """Analyse les références de style et propose une direction artistique, à valider par l'humain."""
        analyse: AnalyseStyle = tache.message
        choix = execution(analyse, PAR_DEFAUT)
        references = References(outils.file.conn, outils.biblio.stockage, outils.biblio.dossier_temp)
        fichiers = []
        for reference_id in analyse.references:
            try:
                source = references.chemin_local(reference_id)
            except StockageErreur:
                continue  # référence retirée entre-temps
            fichier = f"reference-{reference_id}.jpg"
            reduire(source, outils.dossier_travail / fichier)
            fichiers.append(fichier)
        if not fichiers:
            raise ErreurDefinitive("Aucune des références demandées n'existe encore")

        proposee = outils.claude.demander(
            systeme=SYSTEME_ANALYSE.format(charte=contexte.charte, polices=", ".join(polices_disponibles())),
            consigne="Références à analyser (fichiers du dossier courant) :\n" + "\n".join(f"- {f}" for f in fichiers),
            schema=DirectionProposee,
            dossier_travail=outils.dossier_travail,
            effort=choix.effort,
            modele=choix.modele,
            tache_id=tache.id,
            lecture=True,
        )
        Directions(outils.file.conn, self.racine).proposer(proposee.texte, proposee.resume, list(analyse.references),
                                                           tache_id=tache.id)
        return "terminé"
