"""L'équipe d'agents vue par l'humain : rôle, état, activité récente et chaînes de production (page Agents).

Lecture seule de la file des tâches et du journal des coûts. Un agent est « en erreur » tant qu'une de ses
tâches est en échec (elle se relance depuis Réglages → Tâches en échec) : c'est lui qui est responsable.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from noyau.file import File, Tache


@dataclass(frozen=True)
class Agent:
    id: str
    nom: str
    icone: str
    role: str        # une ligne, sur la carte
    recoit: str
    produit: str
    niveau: int      # étage de la pyramide (0 = sommet)


# Étages : 0 décide, 1 informe, 2 produit, 3 contrôle et publie. L'ordre suit la chaîne de gauche à droite.
AGENTS = (
    Agent("orchestrateur_marketing", "Orchestrateur", "sparkle", "Planifie la semaine et confie les briefs",
          "Rapports de veille et de l'analyste, leçons, corrections, vos briefs",
          "Calendrier de la semaine, briefs visuels, vidéo et photos", 0),
    Agent("agent_veille", "Veille", "info", "Observe les concurrents et les tendances",
          "Demande de veille (chaque semaine)", "Rapport de veille pour l'orchestrateur", 1),
    Agent("agent_analyste", "Analyste", "chart-bar", "Tire les leçons des statistiques",
          "Statistiques des posts (relevées toutes les 12 h)", "Rapport hebdomadaire pour l'orchestrateur", 1),
    Agent("agent_visuel", "Visuel", "image-square", "Crée images et carrousels",
          "Brief visuel, direction artistique, références, rushs", "Image ou carrousel", 2),
    Agent("agent_video", "Vidéo", "film-strip", "Crée les reels en motion design",
          "Brief vidéo, charte", "Reel MP4, script et bruitages", 2),
    Agent("agent_redacteur", "Rédacteur", "pencil-simple-line", "Écrit la légende et fixe l'horaire",
          "Visuel ou vidéo créé, brief photos", "Post complet (texte, média, horaire)", 2),
    Agent("agent_relecteur", "Relecteur", "check-circle", "Vérifie charte, fautes et faits",
          "Post rédigé", "Post à valider, ou corrections pour l'orchestrateur", 3),
    Agent("agent_publication", "Publication", "upload-simple", "Publie à l'heure prévue sur Instagram",
          "Post validé dont l'horaire arrive", "Post publié, lien Instagram", 3),
    Agent("agent_erreurs", "Erreurs", "warning-circle", "Analyse chaque rejet",
          "Contenu refusé ou corrigé", "Leçon proposée, à valider par vous", 3),
)
PAR_ID = {a.id: a for a in AGENTS}
NIVEAUX = ("Décide", "Informe", "Produit", "Contrôle et publie")
NOMS_MODELES = {"claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5"}
TYPES_RACINES = ("brief_visuel", "brief_video", "brief_texte")
FENETRE = timedelta(days=7)
TRAITEES = ("terminé", "validé", "refusé")


@dataclass
class EtatAgent:
    agent: Agent
    etat: str = "repos"            # erreur, actif, attente, nuit, repos
    en_cours: list[Tache] = field(default_factory=list)
    a_faire: list[Tache] = field(default_factory=list)
    echecs: list[Tache] = field(default_factory=list)
    traitees_7j: int = 0
    echecs_7j: int = 0
    duree_moyenne: timedelta | None = None
    dernier_passage: datetime | None = None
    modele: str | None = None
    part_jetons: float = 0.0       # part des jetons de l'abonnement consommés sur 7 jours, en %

    @property
    def reussite(self) -> int | None:
        total = self.traitees_7j + self.echecs_7j
        return round(100 * self.traitees_7j / total) if total else None


def _date(valeur: str | None) -> datetime | None:
    return datetime.fromisoformat(valeur) if valeur else None


def etats(file: File, maintenant: datetime) -> dict[str, EtatAgent]:
    """État de chaque agent, par ordre de priorité : erreur, actif, nuit (tout attend la nuit), attente, repos."""
    taches = file.lister()
    debut = maintenant - FENETRE
    resultat = {a.id: EtatAgent(a) for a in AGENTS}
    durees: dict[str, list[timedelta]] = {}
    for t in taches:
        etat = resultat.get(t.agent)
        if etat is None:
            continue
        if t.statut == "en_cours":
            etat.en_cours.append(t)
        elif t.statut == "à_faire":
            etat.a_faire.append(t)
        elif t.statut == "échec":
            etat.echecs.append(t)
        maj = _date(t.maj_le)
        if t.statut in TRAITEES or t.statut == "échec":
            etat.dernier_passage = max(filter(None, (etat.dernier_passage, maj)))
            if maj >= debut:
                if t.statut == "échec":
                    etat.echecs_7j += 1
                else:
                    etat.traitees_7j += 1
        if t.statut == "terminé" and t.pris_le:
            durees.setdefault(t.agent, []).append(maj - _date(t.pris_le))
    for identifiant, liste in durees.items():
        resultat[identifiant].duree_moyenne = sum(liste, timedelta()) / len(liste)
    _consommation(file.conn, resultat, debut)
    for etat in resultat.values():
        if etat.echecs:
            etat.etat = "erreur"
        elif etat.en_cours:
            etat.etat = "actif"
        elif etat.a_faire and all(t.creneau != "jour" for t in etat.a_faire):
            etat.etat = "nuit"
        elif etat.a_faire:
            etat.etat = "attente"
    return resultat


def _consommation(conn: sqlite3.Connection, resultat: dict[str, EtatAgent], debut: datetime) -> None:
    """Dernier modèle Claude utilisé par agent et sa part des jetons de l'abonnement sur la période."""
    lignes = conn.execute(
        "SELECT t.agent, c.modele, c.jetons_entree + c.jetons_sortie AS jetons, c.date FROM couts c "
        "JOIN taches t ON t.id = c.tache_id WHERE c.fournisseur = 'claude-abonnement' ORDER BY c.date").fetchall()
    jetons: dict[str, int] = {}
    for ligne in lignes:
        etat = resultat.get(ligne["agent"])
        if etat is None:
            continue
        etat.modele = NOMS_MODELES.get(ligne["modele"], ligne["modele"])
        if _date(ligne["date"]) >= debut:
            jetons[ligne["agent"]] = jetons.get(ligne["agent"], 0) + ligne["jetons"]
    total = sum(jetons.values())
    for identifiant, nombre in jetons.items():
        resultat[identifiant].part_jetons = 100 * nombre / total if total else 0.0


def historique(file: File, agent_id: str, limite: int = 30) -> list[dict]:
    """Tâches d'un agent : celles en cours ou à faire d'abord, puis les plus récentes. Chacune avec sa durée,
    ce qu'elle a produit (tâches filles) et si elle appartient à une publication (ouvrable dans le calendrier)."""
    taches = file.lister()
    par_id = {t.id: t for t in taches}
    enfants: dict[int, list[Tache]] = {}
    for t in taches:
        if t.parent_id:
            enfants.setdefault(t.parent_id, []).append(t)

    def dans_publication(tache: Tache) -> bool:
        while tache:
            if tache.type in TYPES_RACINES:
                return True
            tache = par_id.get(tache.parent_id)
        return False

    siennes = sorted((t for t in taches if t.agent == agent_id), key=lambda t: t.maj_le, reverse=True)
    siennes.sort(key=lambda t: t.statut not in ("en_cours", "à_faire"))  # tri stable : les actives en tête
    lignes = []
    for t in siennes[:limite]:
        duree = _date(t.maj_le) - _date(t.pris_le) if t.statut == "terminé" and t.pris_le else None
        lignes.append({"tache": t, "duree": duree, "resultats": sorted(enfants.get(t.id, []), key=lambda e: e.id),
                       "active": t.statut in ("en_cours", "à_faire"), "publication": dans_publication(t)})
    return lignes


def chaines(file: File, limite: int = 6) -> list[dict]:
    """Les dernières chaînes de production : chaque brief et toutes les tâches qui en découlent, dans l'ordre."""
    taches = file.lister()
    enfants: dict[int, list[Tache]] = {}
    for t in taches:
        if t.parent_id:
            enfants.setdefault(t.parent_id, []).append(t)
    par_id = {t.id: t for t in taches}

    def premier_brief(tache: Tache) -> bool:
        """Une nouvelle version après corrections ou refus descend du premier brief : elle reste dans sa chaîne."""
        parent = par_id.get(tache.parent_id)
        while parent:
            if parent.type in TYPES_RACINES:
                return False
            parent = par_id.get(parent.parent_id)
        return True

    racines = sorted((t for t in taches if t.type in TYPES_RACINES and premier_brief(t)),
                     key=lambda t: t.id, reverse=True)[:limite]
    resultat = []
    for racine in racines:
        etapes, pile = [], [racine]
        while pile:
            tache = pile.pop()
            etapes.append(tache)
            pile.extend(enfants.get(tache.id, []))
        etapes.sort(key=lambda t: t.id)
        resultat.append({"racine": racine, "etapes": etapes, "erreur": any(t.statut == "échec" for t in etapes),
                         "versions": sum(t.type in TYPES_RACINES for t in etapes),
                         "texte": next((t.contenu.get("texte") for t in reversed(etapes) if t.type == "post_brouillon"), None)})
    return resultat


def duree(valeur: timedelta | None) -> str:
    if valeur is None:
        return "—"
    secondes = int(valeur.total_seconds())
    if secondes < 60:
        return f"{secondes} s"
    if secondes < 3600:
        return f"{secondes // 60} min {secondes % 60:02d} s"
    return f"{secondes // 3600} h {secondes % 3600 // 60:02d}"


def centres(nombre: int) -> list[float]:
    """Centre horizontal (en %) de chaque carte d'un étage : un étage de n cartes occupe n quarts de la largeur."""
    debut = (100 - 25 * nombre) / 2
    return [debut + 25 * (i + 0.5) for i in range(nombre)]
