"""orchestrateur_marketing : construit chaque semaine le calendrier éditorial et lance les briefs.

Entrée : demande_planning (semaine, nombre de posts). Il s'appuie sur la charte, les leçons, la direction
artistique, le dernier rapport de veille, le dernier rapport de l'analyste, la bibliothèque de rushs (avec leur utilisation, pour les faire
tourner) et l'historique récent des posts (validés, refusés avec commentaire) pour varier les contenus.
Sortie : un brief par post, brief_visuel (image ou carrousel) ou brief_video (reel en motion design, un par semaine
au plus) : jamais de post texte seul, qui n'a pas sa place sur Instagram.
Chaque brief précise le modèle et l'effort ; la génération de contenu se fait toujours avec Opus 5.5.
Pour chaque brief, il décide aussi du créneau (noyau/creneaux.py) : la production attend la nuit si le post
peut être produit et validé à temps, sinon elle part tout de suite.
"""

import json
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.claude import ClaudeErreur
from noyau.contexte import Contexte
from noyau.creneaux import Creneaux
from noyau.direction import Directions
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.images import reduire
from noyau.messages import MESSAGES, BriefVideo, BriefVisuel, Corrections, DemandePlanning
from noyau.modeles import EFFORTS, GUIDE_DU_CHOIX, MODELES, OPUS, Execution, execution
from noyau.stockage import StockageErreur

FUSEAU = ZoneInfo("Europe/Paris")
PAR_DEFAUT = Execution(OPUS, "high")
MAX_RUSHS_MONTRES = 12
MAX_HISTORIQUE = 20
MAX_VIDEOS_PAR_SEMAINE = 1  # le rendu vidéo est la tâche la plus lourde (à revoir après le banc du VPS)
JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

SYSTEME = """Tu es orchestrateur_marketing, le responsable éditorial de l'agence marketing de la marque décrite
ci-dessous. Ta seule tâche : construire le calendrier de la semaine (nombre de posts imposé) et rédiger le
brief de chaque post pour les agents qui les produiront. L'humain ne fait que valider : tes briefs doivent
suffire.

Règles :
- Chaque post a un visuel : jamais de post texte seul. Varie les formats : visuels avec photo réelle,
  carrousels pédagogiques, créations graphiques, et au plus {max_videos} reel par semaine en motion design
  (typographie animée, textes rythmés, bruitages discrets, 10 à 20 s), quand le message s'y prête ; jamais deux fois le même sujet ou le même
  angle que dans l'historique récent.
- Fais tourner les rushs : privilégie les moins utilisés (regarde-les avec l'outil Read), et ne prévois pas de
  rush pour un post qui n'en a pas besoin (création graphique sur fond uni, carrousel typographique).
- Appuie-toi sur le rapport de veille (formats et angles qui marchent) sans copier ; respecte la charte, les
  leçons et la direction artistique ; n'invente ni chiffre, ni client, ni promesse.
- Tiens compte du rapport de l'analyste (résultats réels des posts publiés) : reconduis ce qui marche, corrige
  ce qui ne marche pas, et cite-le dans la justification des posts concernés.
- Instagram en priorité (compte professionnel de la marque) ; choisis les jours et heures de publication
  dans la semaine indiquée, aux moments les plus pertinents pour la cible.
- Les contenus sont produits la nuit, pour ménager l'abonnement, puis validés le matin : privilégie des
  publications à partir de l'heure indiquée dans la consigne. Un post plus tôt reste possible s'il le faut,
  mais sa production entamera la journée.
- Chaque brief précise obligatoirement le modèle et l'effort que l'agent devra utiliser. La génération de
  contenu (tous les posts) se fait avec claude-opus-5-5 ; choisis l'effort selon la difficulté du post.
  Repères :
{guide}

# Charte de marque

{charte}

# Leçons tirées des contenus refusés

{lecons}

# Direction artistique (résumé)

{direction}
"""


class PostPrevu(BaseModel):
    date_prevue: datetime = Field(description="Date et heure de publication, avec fuseau (heure de Paris)")
    reseau: Literal["instagram", "linkedin"]
    type_contenu: Literal["visuel", "video"] = Field("visuel", description="visuel (image, carrousel) ou video (reel)")
    format: str = Field(min_length=1, description="Ex. carrousel de 5 slides, post photo, visuel explicatif")
    objectif: str = Field(min_length=1, description="Brief détaillé : sujet, angle, accroche, message clé")
    rushs: list[int] = Field(default_factory=list, description="Numéros des rushs à utiliser (aucun pour une création graphique)")
    modele: Literal[MODELES]
    effort: Literal[EFFORTS]
    justification: str = Field(min_length=1, description="Pourquoi ce post, en lien avec la veille ou l'historique")


class Planning(BaseModel):
    strategie: str = Field(min_length=1, description="Stratégie de la semaine, en quelques phrases, pour l'humain")
    posts: list[PostPrevu] = Field(min_length=1)


class AgentOrchestrateur(Agent):
    id = "orchestrateur_marketing"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type == "corrections":
            return self._refaire(tache, outils)
        if tache.type != "demande_planning":
            raise ErreurDefinitive(f"orchestrateur_marketing ne traite pas encore les messages {tache.type}")
        demande: DemandePlanning = tache.message
        choix = execution(demande, PAR_DEFAUT)
        conn = outils.file.conn

        rushs = self._rushs(outils)
        veille = conn.execute("SELECT contenu FROM rapports_veille ORDER BY id DESC LIMIT 1").fetchone()
        analyse = conn.execute("SELECT contenu FROM rapports_analyste ORDER BY id DESC LIMIT 1").fetchone()
        active = Directions(conn, self.racine).active()
        lundi = datetime.combine(demande.semaine, datetime.min.time(), FUSEAU)
        creneaux = self._creneaux(outils)
        consigne = [
            f"Semaine à planifier : du lundi {demande.semaine:%d/%m/%Y} au dimanche "
            f"{demande.semaine + timedelta(days=6):%d/%m/%Y}. Nombre de posts : {demande.nombre_posts}.",
            "Première publication possible pour un contenu produit la nuit prochaine : "
            + f"{JOURS[(plus_tot := creneaux.plus_tot_publiable().astimezone(FUSEAU)).weekday()]} "
            + f"{plus_tot:%d/%m/%Y à %H:%M}.",
            "Rapport de veille : " + (json.dumps(json.loads(veille["contenu"]), ensure_ascii=False, indent=1)
                                      if veille else "aucun pour l'instant."),
            "Rapport de l'analyste (résultats des posts publiés) : "
            + (json.dumps(json.loads(analyse["contenu"]), ensure_ascii=False, indent=1)
               if analyse else "aucun pour l'instant."),
            "Rushs disponibles (fichier à regarder, utilisations passées) :\n"
            + ("\n".join(f"- n° {r['id']} : {r['fichier']} ({r['nom']}), utilisé {r['utilisations']} fois" for r in rushs)
               or "aucun"),
            "Historique récent des posts :\n" + json.dumps(self._historique(outils), ensure_ascii=False, indent=1),
        ]
        planning = outils.claude.demander(
            systeme=SYSTEME.format(guide=GUIDE_DU_CHOIX, charte=contexte.charte, lecons=contexte.lecons,
                                   max_videos=MAX_VIDEOS_PAR_SEMAINE,
                                   direction=active.resume if active else "aucune direction validée"),
            consigne="\n\n".join(consigne),
            schema=Planning,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            modele=choix.modele,
            effort=choix.effort,
            lecture=bool(rushs),
        )
        self._verifier(planning, demande, lundi, {r["id"] for r in rushs})

        for post in planning.posts:
            classe = BriefVideo if post.type_contenu == "video" else BriefVisuel
            outils.file.creer(classe(objectif=f"{post.objectif}\n\nPourquoi : {post.justification}",
                                     format=post.format, reseau=post.reseau, date_prevue=post.date_prevue,
                                     rushs=post.rushs, modele=OPUS, effort=post.effort), parent_id=tache.id,
                              creneau=creneaux.decider(post.date_prevue))
        conn.execute("INSERT INTO plannings (tache_id, semaine, strategie, cree_le) VALUES (?, ?, ?, ?)",
                     (tache.id, demande.semaine.isoformat(), planning.strategie, outils.file.horloge().isoformat()))
        return "terminé"

    @staticmethod
    def _creneaux(outils: Outils) -> Creneaux:
        if outils.config is None:
            return Creneaux(outils.file.conn, horloge=outils.file.horloge)
        return Creneaux.depuis_config(outils.config, outils.file.conn, outils.file.horloge)

    def _refaire(self, tache: Tache, outils: Outils) -> str:
        """Relance le brief d'origine du post, avec les corrections demandées (sans appel à Claude)."""
        corrections: Corrections = tache.message
        parent_id = outils.file.lire(corrections.contenu_id).parent_id
        while parent_id:
            parent = outils.file.lire(parent_id)
            if parent.type in ("brief_texte", "brief_visuel", "brief_video"):
                break
            parent_id = parent.parent_id
        else:
            raise ErreurDefinitive(f"Brief d'origine du contenu {corrections.contenu_id} introuvable")
        auteur = "le relecteur" if corrections.origine == "relecteur" else "l'humain"
        champs = dict(parent.contenu)
        champs["objectif"] += f"\n\nNouvelle version demandée par {auteur}. Corrections à appliquer :\n" \
                              + "\n".join(f"- {p}" for p in corrections.problemes)
        brief = MESSAGES[parent.type].model_validate(champs)
        # Correction du relecteur : suite de la même chaîne (de nuit si elle l'était). Refus humain : nouvelle
        # production, de nuit si la date de publication le permet.
        post = outils.file.lire(corrections.contenu_id)
        if corrections.origine == "relecteur":
            creneau = "jour" if post.creneau == "jour" else "nuit_suite"
        elif corrections.immediat:
            creneau = "jour"  # demande de modification de l'humain, « tout de suite »
        else:
            creneau = self._creneaux(outils).decider(brief.date_prevue)
        outils.file.creer(brief, parent_id=tache.id, creneau=creneau)
        return "terminé"

    @staticmethod
    def _verifier(planning: Planning, demande: DemandePlanning, lundi: datetime, rushs: set[int]) -> None:
        """Un planning hors cadre est redemandé (erreur ordinaire, la tâche est retentée)."""
        if len(planning.posts) != demande.nombre_posts:
            raise ClaudeErreur(f"Planning de {len(planning.posts)} posts au lieu de {demande.nombre_posts}")
        for post in planning.posts:
            if not lundi <= post.date_prevue < lundi + timedelta(days=7):
                raise ClaudeErreur(f"Post prévu le {post.date_prevue:%d/%m/%Y}, hors de la semaine demandée")
            inconnus = set(post.rushs) - rushs
            if inconnus:
                raise ClaudeErreur(f"Rushs inconnus dans le planning : {sorted(inconnus)}")
        videos = sum(post.type_contenu == "video" for post in planning.posts)
        if videos > MAX_VIDEOS_PAR_SEMAINE:
            raise ClaudeErreur(f"{videos} reels dans la semaine : {MAX_VIDEOS_PAR_SEMAINE} au plus")

    @staticmethod
    def _rushs(outils: Outils) -> list[dict]:
        """Photos disponibles, les moins utilisées d'abord, copiées en petit pour que Claude les voie."""
        conn = outils.file.conn
        utilisations = {l["rush"]: l["nombre"] for l in conn.execute(
            "SELECT CAST(j.value AS INTEGER) AS rush, count(*) AS nombre FROM taches, json_each(taches.contenu, '$.rushs') AS j "
            "WHERE taches.type IN ('brief_texte', 'brief_visuel') GROUP BY rush")}
        photos = [a for a in outils.biblio.lister("actif", origine="rush") if a.type == "image"]
        photos.sort(key=lambda a: (utilisations.get(a.id, 0), -a.id))
        rushs = []
        for asset in photos[:MAX_RUSHS_MONTRES]:
            fichier = f"rush-{asset.id}.jpg"
            try:
                reduire(outils.biblio.chemin_local(asset.id), outils.dossier_travail / fichier, cote_max=512)
            except StockageErreur:
                continue
            rushs.append({"id": asset.id, "fichier": fichier, "nom": asset.nom_original,
                          "utilisations": utilisations.get(asset.id, 0)})
        return rushs

    @staticmethod
    def _historique(outils: Outils) -> list[dict]:
        lignes = outils.file.conn.execute(
            "SELECT contenu, statut, commentaire, maj_le FROM taches WHERE type = 'post_brouillon' "
            "ORDER BY id DESC LIMIT ?", (MAX_HISTORIQUE,))
        return [{"texte": json.loads(l["contenu"])["texte"][:240], "statut": l["statut"],
                 "commentaire": l["commentaire"], "date": l["maj_le"][:10]} for l in lignes]
