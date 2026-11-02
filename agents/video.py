"""agent_video : le motion designer de l'agence. brief_video → un reel en motion design, créé de A à Z.

1. Storyboard (Claude, Opus 5.5) : intention et scènes, chacune avec son texte à l'écran et sa durée de lecture.
2. Composition HTML/CSS/GSAP (Claude) : typographie animée, formes, couleurs de la marque, photos réelles
   éventuelles, et bruitages : de vrais enregistrements CC0 de la banque (noyau/banque_sons.py), frappe calée sur
   l'image, ou en secours des sons écrits en Web Audio, calculés hors ligne par Chrome (noyau/sons.py). Contrôles
   gratuits avant tout rendu (ressources externes, durée, bruitages, `hyperframes lint`) ; une correction au plus.
3. Une image par seconde en brouillon, dont quelques-unes relues par Claude en directeur artistique ;
   une correction au plus.
4. Un seul rendu MP4 complet (1080×1920, 30 i/s ; ni voix ni musique : une musique peut être ajoutée depuis
   Instagram à la publication), niveau sonore réglé, enregistré ; message `asset` vers agent_redacteur.

Chaque étape faite est gardée dans le dossier `video-<tâche>` des fichiers de travail (storyboard, composition
contrôlée, composition relue) : une reprise (pause de quota, plantage, redémarrage du worker, relance d'une tâche
en échec) ne refait aucun appel déjà payé. Supprimé après réussite. Voie `rendu` : un seul rendu à la fois.
"""

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from agents.visuel import AgentVisuel, coller_ponctuation
from noyau import sons
from noyau.banque_sons import Banque, BanqueErreur
from noyau import video as moteur_video
from noyau.claude import ClaudeErreur
from noyau.config import RACINE
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.messages import Asset, BriefVideo
from noyau.modeles import OPUS, Execution, execution
from noyau.rendu import elements_marque, polices_disponibles
from noyau.stockage import StockageErreur

PAR_DEFAUT = Execution(OPUS, "high")
# La composition est l'appel le plus long (des dizaines de milliers de jetons) : effort moyen, la relecture des
# images clés rattrape les défauts.
EFFORT_COMPOSITION = "medium"
EFFORT_RELECTURE = "medium"
DUREE_MAX = 30  # secondes, jusqu'au banc de test du VPS
IMAGES_RELUES = 6
MAX_BRUITAGES = 8
MAX_WEB_AUDIO = 2  # sons synthétisés en secours quand la banque est remplie

SYSTEME_STORYBOARD = """Tu es agent_video, le motion designer senior de la marque décrite ci-dessous. Ta tâche ici :
écrire le storyboard d'un reel Instagram vertical en motion design (aucune vidéo filmée, aucune voix ni musique :
typographie animée, formes, couleurs de la marque, éventuellement des photos réelles fournies, bruitages
discrets sur les transitions et apparitions).

Règles :
- Durée totale : {duree_max} s au plus ; vise 10 à 20 s (les reels courts marchent le mieux).
- Le message passe par les textes à l'écran : une accroche forte dans les 2 premières secondes, une idée par
  scène, un appel à l'action à la fin. Textes courts, en français, vouvoiement, ton de la charte ; chaque scène
  dure assez pour être lue (environ 0,4 s par mot, 1,5 s au minimum).
- 3 à 7 scènes, chacune avec son intention visuelle, son texte à l'écran et sa durée.
- N'invente aucun chiffre, client, prix ou promesse absent de la charte.

# Direction artistique

{direction}

# Charte de marque

{charte}

# Leçons tirées des contenus refusés

{lecons}
"""

SYSTEME_COMPOSITION = """Tu es agent_video, motion designer senior. Tu écris la composition HyperFrames d'un reel :
du HTML/CSS animé avec GSAP, rendu image par image en MP4 de {largeur} × {hauteur} px à {fps} i/s, avec des
bruitages (ni voix ni musique).
Le niveau attendu est celui d'un studio professionnel : jamais d'esthétique « générée par IA ».

Ce que tu fournis (le gabarit fournit le reste : racine aux bonnes dimensions, GSAP, polices) :
- `html` : le contenu de la racine. Chaque scène est un <section id="…" class="clip" data-start="…"
  data-duration="…"> (secondes, positions absolues dans la vidéo, `.clip` est en position absolue plein cadre).
  Identifiants uniques. Pas de <video>, pas d'<audio>, pas de <script>.
- `css` : la feuille de style. Polices disponibles : {polices} (font-family par leur nom). Fichiers utilisables :
  {fichiers}. Aucune ressource externe (ni police, ni image, ni @import, ni adresse http).
- `animation` : du JavaScript qui ajoute des tweens à la timeline GSAP `tl` déjà créée et en pause, avec des
  positions absolues en secondes (ex. tl.from("#titre", {{y: 80, opacity: 0, duration: 0.6, ease: "power3.out"}}, 1.2)).
  Interdits : Math.random, Date, requestAnimationFrame, setTimeout, répétitions infinies, play(). Tout doit
  être fini et déterministe.
- `duree` : durée totale en secondes, {duree_max} s au plus, conforme au storyboard.
{bruitages}

Mise en scène :
- Textes lisibles et en place assez longtemps pour être lus ; dans la zone sûre : Instagram couvre environ
  les 220 px du haut et les 420 px du bas ; garde les textes entre ces deux bandes et à 60 px au moins des bords.
- Mouvement soigné : easings doux (power2/power3/expo), durées de 0,3 à 0,8 s, décalages entre éléments,
  transitions entre scènes ; rien ne reste figé plus de 2 s.
- Direction artistique et charte avant tout (couleurs, typographie, monogramme ou logo de la marque en fin de vidéo).
- Mot surligné : la ponctuation qui suit un mot surligné colle au mot (« soif. », jamais « soif . ») : surlignage en
  padding asymétrique (environ 0,14 em à gauche, 0,04 em au plus à droite, jamais de padding symétrique ni
  de marge), ponctuation à l'extérieur, juste après l'élément surligné, sans espace ni marge.

# Direction artistique

{direction}

# Charte de marque

{charte}
"""

SYSTEME_RELECTURE = """Tu es directeur artistique senior. Tu relis les images clés d'un reel en motion design
(une image par seconde, en qualité brouillon) avant le rendu final. Regarde chaque image (outil Read).

Vérifie : textes lisibles, exacts et sans faute, dans la zone sûre (ni les 220 px du haut ni les 420 px du bas) ;
rien de coupé, superposé par erreur ou qui déborde ; ponctuation collée au mot surligné (aucun écart visible
avant « . » ou « ? ») ; hiérarchie et rythme visuel ; cohérence avec la direction
artistique et la charte ; aucune esthétique « IA » ou amateur.

Réponds `conforme: true` si tout est bon. Sinon `conforme: false` et la liste des problèmes : chacun précis,
localisé (seconde, élément) et actionnable par le motion designer.

# Direction artistique

{direction}

# Charte de marque

{charte}
"""


CODE_WEB_AUDIO = """  `code` : le corps d'une fonction `son(ctx, aleatoire)` en Web Audio.
  `ctx` est un OfflineAudioContext stéréo à 48 kHz, de la durée du son : crée des nœuds (oscillateurs, tampons de
  bruit, filtres, gains avec enveloppes), relie-les à `ctx.destination` et démarre-les entre 0 et la durée.
  Pour le hasard (bruit), uniquement `aleatoire()` (entre 0 et 1) ; ni Math.random, ni requête. Sons courts,
  doux et élégants (souffles, pops, clics, montées), jamais agressifs. Exemples :
{exemples}"""

# Banque vide (import pas encore fait) : uniquement des sons synthétisés, comme avant la banque.
BRUITAGES_WEB_AUDIO = """- `bruitages` : 2 à {max_bruitages} sons discrets calés sur les animations (transitions, apparitions, mise en
  valeur), chacun avec son début et sa durée en secondes (0,05 à 5 s, fini avant la fin de la vidéo), son volume
  (0,05 à 1 ; 0,2 à 0,5 en général) et son
""" + CODE_WEB_AUDIO

BRUITAGES_BANQUE = """- `bruitages` : 2 à {max_bruitages} sons discrets calés sur les animations (transitions, apparitions, mise en
  valeur). **De vrais enregistrements d'abord**, choisis dans la banque ci-dessous par leur `son_id` : `debut` est
  alors l'instant exact où le son doit frapper (impact d'un mot, apparition d'un élément, passage d'une
  transition) ; le son est décalé automatiquement pour que sa frappe tombe sur cet instant. Ni `duree` ni `code`
  pour eux. Le son entier doit tenir avant la fin de la vidéo. Volume de 0,05 à 1 (0,2 à 0,5 en général). Varie
  les sons d'une scène à l'autre, reste sobre et élégant.
  Banque (identifiant · catégorie (usage) · durée, instant de la frappe dans le son · titre (mots-clés)) :
{catalogue}
- En secours seulement, si aucun son de la banque ne convient : {max_web_audio} sons au plus synthétisés en Web Audio,
  avec `duree` (0,05 à 5 s), `debut` = début du son, et
""" + CODE_WEB_AUDIO


class Scene(BaseModel):
    intention: str = Field(min_length=1, description="Ce que montre la scène et comment elle bouge")
    texte_ecran: str = Field(default="", description="Texte affiché (court)")
    duree: float = Field(gt=0, description="Durée de la scène en secondes")


class Storyboard(BaseModel):
    intention: str = Field(min_length=1, description="En une phrase, en français : le message du reel")
    scenes: list[Scene] = Field(min_length=2, max_length=8)


class Bruitage(BaseModel):
    usage: str = Field(min_length=1, description="À quoi sert le son, en français (ex. transition vers la scène 2)")
    son_id: int | None = Field(default=None, description="Son de la banque (vrai enregistrement), de préférence")
    debut: float = Field(ge=0, description="Banque : instant de la frappe ; Web Audio : début du son (secondes)")
    duree: float | None = Field(default=None, gt=0, le=sons.DUREE_MAX, description="Web Audio seulement")
    volume: float = Field(default=0.35, ge=0.05, le=1)
    code: str = Field(default="", description="Web Audio seulement : corps de la fonction son(ctx, aleatoire)")


class Composition(BaseModel):
    css: str
    html: str = Field(min_length=1)
    animation: str = Field(min_length=1)
    duree: float = Field(gt=0)
    bruitages: list[Bruitage] = Field(default_factory=list, max_length=MAX_BRUITAGES)


class Relecture(BaseModel):
    conforme: bool
    problemes: list[str] = Field(default_factory=list)


class AgentVideo(Agent):
    id = "agent_video"
    voie = "rendu"

    def __init__(self, racine: Path = RACINE, moteur=moteur_video, duree_max: float = DUREE_MAX,
                 synthetiseur=sons.synthetiser):
        super().__init__(racine)
        self._banque: Banque | None = None
        self.moteur = moteur
        self.duree_max = duree_max
        self.synthetiseur = synthetiseur

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type != "brief_video":
            raise ErreurDefinitive(f"agent_video ne traite pas les messages {tache.type}")
        brief: BriefVideo = tache.message
        choix = execution(brief, PAR_DEFAUT)  # génération de contenu : Opus 5.5 imposé
        dossier = outils.dossier_travail
        photos = AgentVisuel._preparer_rushs(brief, outils)
        direction = contexte.direction or "(pas encore de direction validée : suis la charte)"
        cache = outils.biblio.dossier_temp / f"video-{tache.id}"
        cache.mkdir(parents=True, exist_ok=True)
        self._banque = Banque(outils.biblio.conn, outils.biblio.stockage, outils.biblio.dossier_temp)
        catalogue = self._banque.catalogue()

        # 1. Storyboard
        storyboard = self._repris(cache / "storyboard.json", Storyboard) or self._garder(
            cache / "storyboard.json", outils.claude.demander(
                systeme=SYSTEME_STORYBOARD.format(duree_max=f"{self.duree_max:g}", direction=direction,
                                                  charte=contexte.charte, lecons=contexte.lecons),
                consigne=self._consigne_brief(brief, photos),
                schema=Storyboard, dossier_travail=dossier, effort=choix.effort, modele=choix.modele,
                tache_id=tache.id, lecture=bool(photos)))

        # 2. Composition, contrôlée avant tout rendu
        systeme = SYSTEME_COMPOSITION.format(
            largeur=self.moteur.LARGEUR, hauteur=self.moteur.HAUTEUR, fps=self.moteur.FPS,
            polices=", ".join(polices_disponibles()), fichiers=", ".join([*elements_marque(), *photos]) or "aucun",
            duree_max=f"{self.duree_max:g}", bruitages=self._consigne_bruitages(catalogue),
            direction=direction, charte=contexte.charte)
        base = "Storyboard :\n" + storyboard.model_dump_json() + "\n\nÉcris la composition."

        def demander(consigne: str) -> Composition:
            return outils.claude.demander(systeme=systeme, consigne=consigne, schema=Composition,
                                          dossier_travail=dossier, effort=EFFORT_COMPOSITION, modele=choix.modele,
                                          tache_id=tache.id, lecture=bool(photos))

        projet = dossier / "projet"
        relue = self._repris(cache / "relue.json", Composition)
        if relue:  # reprise après la relecture : seul le rendu reste à faire
            composition = self._verifier(relue, projet, demander, base)
            return self._terminer(tache, storyboard, composition, None, projet, cache, outils)
        composition = self._repris(cache / "composition.json", Composition)
        composition = self._garder(cache / "composition.json",
                                   self._verifier(composition or demander(base), projet, demander, base))

        # 3. Relecture d'images clés en brouillon, une correction au plus
        images = self.moteur.images_cles(projet, dossier / "images")
        nombre = min(IMAGES_RELUES, len(images))  # réparties du début à la fin, sans doublon
        choisies = [images[round(i * (len(images) - 1) / max(nombre - 1, 1))] for i in range(nombre)]
        relecture = outils.claude.demander(
            systeme=SYSTEME_RELECTURE.format(direction=direction, charte=contexte.charte),
            consigne="Images clés à relire (seconde = numéro - 1) : "
                     + ", ".join(str(i.relative_to(dossier)) for i in choisies),
            schema=Relecture, dossier_travail=dossier, effort=EFFORT_RELECTURE, modele=choix.modele,
            tache_id=tache.id, lecture=True)
        if not relecture.conforme and relecture.problemes:
            corrigee = demander(base + "\n\nTa composition actuelle :\n" + composition.model_dump_json()
                                + "\n\nLe directeur artistique demande ces corrections :\n"
                                + "\n".join(f"- {p}" for p in relecture.problemes)
                                + "\n\nRenvoie la composition corrigée complète.")
            composition = self._verifier(corrigee, projet, demander, base)
        self._garder(cache / "relue.json", composition)
        return self._terminer(tache, storyboard, composition, relecture, projet, cache, outils)

    def _terminer(self, tache: Tache, storyboard: Storyboard, composition: Composition, relecture: Relecture | None,
                  projet: Path, cache: Path, outils: Outils) -> str:
        """4. Rendu final unique, enregistrement et message vers le rédacteur."""
        niveau = ({"cible_lufs": outils.config.son_cible_lufs, "crete_dbtp": outils.config.son_crete_dbtp}
                  if outils.config else {})
        mp4 = self.moteur.rendre_mp4(projet, outils.dossier_travail / f"video-{tache.id}.mp4", **niveau)
        asset = outils.biblio.ajouter(mp4, "généré", tache_id=tache.id, genere_par_ia=True)
        script = (f"{storyboard.intention}\n\nScènes :\n"
                  + "\n".join(f"{i}. ({s.duree:g} s) {s.intention}" + (f" — « {s.texte_ecran} »" if s.texte_ecran else "")
                              for i, s in enumerate(storyboard.scenes, start=1)))
        if relecture and not relecture.conforme:
            script += "\n\nCorrections faites après relecture : " + " ; ".join(relecture.problemes)
        outils.file.creer(Asset(brief_id=tache.id, type_media="vidéo", asset_id=asset,
                                prompt_ou_script=script + "\n\nComposition : " + composition.model_dump_json()),
                          parent_id=tache.id)
        shutil.rmtree(cache, ignore_errors=True)
        return "terminé"

    # --- Reprise ---

    @staticmethod
    def _repris(fichier: Path, schema):
        return schema.model_validate_json(fichier.read_text(encoding="utf-8")) if fichier.exists() else None

    @staticmethod
    def _garder(fichier: Path, valeur):
        fichier.write_text(valeur.model_dump_json(), encoding="utf-8")
        return valeur

    # --- Contrôles ---

    def _verifier(self, composition: Composition, projet: Path, demander, base: str) -> Composition:
        """Assemble le projet et le contrôle (gratuit) ; une correction au plus, sinon nouvel essai de la tâche."""
        for essai in range(2):
            erreurs = self._erreurs(composition, projet)
            if not erreurs:
                return composition
            if essai == 1:
                raise ClaudeErreur("Composition vidéo invalide après correction : " + " ; ".join(erreurs))
            composition = demander(base + "\n\nTa composition actuelle :\n" + composition.model_dump_json()
                                   + "\n\nElle est refusée par les contrôles techniques :\n"
                                   + "\n".join(f"- {e}" for e in erreurs)
                                   + "\n\nRenvoie la composition corrigée complète.")
        return composition

    def _erreurs(self, composition: Composition, projet: Path) -> list[str]:
        if composition.duree > self.duree_max:
            return [f"durée {composition.duree:g} s : {self.duree_max:g} s au plus"]
        shutil.rmtree(projet, ignore_errors=True)
        projet.mkdir(parents=True)
        erreurs, placements = [], []
        banque_remplie = bool(self._banque and self._banque.lister())
        web_audio = 0
        for numero, bruitage in enumerate(composition.bruitages, start=1):
            nom = f"bruitage {numero} ({bruitage.usage})"
            if (bruitage.son_id is None) == (not bruitage.code.strip()):
                erreurs.append(f"{nom} : donner soit `son_id` (banque), soit `code` (Web Audio), un seul des deux")
                continue
            if bruitage.son_id is not None:  # vrai enregistrement, frappe calée sur `debut`
                try:
                    son = self._banque.lire(bruitage.son_id)
                    source = self._banque.chemin_local(bruitage.son_id)
                except (BanqueErreur, StockageErreur):
                    erreurs.append(f"{nom} : le son {bruitage.son_id} n'est pas dans la banque")
                    continue
                debut, duree = round(max(0.0, bruitage.debut - son.attaque), 3), son.duree
                if debut + duree > composition.duree:
                    erreurs.append(f"{nom} : le son ({duree:g} s, frappe à {son.attaque:g} s) finit après la fin "
                                   "de la vidéo ; frappe plus tôt ou son plus court")
                    continue
                fichier = f"banque-{numero}.wav"
                shutil.copyfile(source, projet / fichier)
            else:  # secours : son synthétisé en Web Audio
                web_audio += 1
                if banque_remplie and web_audio > MAX_WEB_AUDIO:
                    erreurs.append(f"{nom} : {MAX_WEB_AUDIO} sons Web Audio au plus, prends un son de la banque")
                    continue
                if bruitage.duree is None:
                    erreurs.append(f"{nom} : `duree` obligatoire pour un son Web Audio")
                    continue
                debut, duree = bruitage.debut, bruitage.duree
                if debut + duree > composition.duree:
                    erreurs.append(f"{nom} : finit après la fin de la vidéo")
                    continue
                fichier = f"bruitage-{numero}.wav"
                try:
                    self.synthetiseur(bruitage.code, duree, projet / fichier, projet.parent)
                except sons.SonErreur as erreur:
                    erreurs.append(f"{nom} : {erreur}")
                    continue
            placements.append(self.moteur.Son(fichier, debut, bruitage.volume))
        if erreurs:
            return erreurs
        try:
            self.moteur.assembler(projet, css=composition.css, html=coller_ponctuation(composition.html),
                                  animation=composition.animation, duree=composition.duree, sons=placements)
        except self.moteur.VideoErreur as erreur:
            return [str(erreur)]
        for photo in projet.parent.glob("rush-*.jpg"):
            shutil.copyfile(photo, projet / photo.name)
        return self.moteur.lint(projet)

    @staticmethod
    def _consigne_bruitages(catalogue: str) -> str:
        if not catalogue:
            return BRUITAGES_WEB_AUDIO.format(max_bruitages=MAX_BRUITAGES, exemples=sons.EXEMPLES)
        return BRUITAGES_BANQUE.format(max_bruitages=MAX_BRUITAGES, catalogue=catalogue,
                                       max_web_audio=MAX_WEB_AUDIO, exemples=sons.EXEMPLES)

    @staticmethod
    def _consigne_brief(brief: BriefVideo, photos: list[str]) -> str:
        lignes = ["Brief :", f"- Objectif : {brief.objectif}", f"- Format : {brief.format}", f"- Réseau : {brief.reseau}"]
        lignes.append("- Photos réelles utilisables (dossier courant) : " + ", ".join(photos) if photos
                      else "- Aucune photo : motion design pur.")
        lignes.append("\nÉcris le storyboard.")
        return "\n".join(lignes)
