"""Calendrier hebdomadaire : publications prévues et tâches des agents, jour par jour (heure de Paris).

Une publication = un brief racine (créé par le planning ou par l'humain) et toute sa lignée de tâches :
visuel, post, relecture, corrections, nouveau brief, analyse du rejet, leçon… Son état est celui du
dernier post de la lignée. Une publication refusée sans nouvelle version (écartée ou abandonnée)
n'apparaît pas : un refus avec commentaire relance une version, qui prend sa place. Les autres tâches (veille, planning, analyse de style…) sont placées le jour
où elles ont été créées ; la veille et le planning du dimanche apparaissent à l'avance comme « prévus ».
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from noyau.file import File, Tache
from noyau.planificateur import AGENTS_EVENEMENTS, EVENEMENTS
from noyau.publication import Publication, Publications

FUSEAU = ZoneInfo("Europe/Paris")
BRIEFS = ("brief_texte", "brief_visuel", "brief_video")
EN_COURS = ("à_faire", "en_cours")
# Tâches qui produisent le post ; l'analyse d'un rejet (agent_erreurs) ne retarde pas la publication.
PRODUCTION = (*BRIEFS, "asset", "post_brouillon", "corrections", "modification_texte")


@dataclass
class ElementPublication:
    racine: Tache
    lignee: list[Tache]  # toutes les tâches de la lignée, dans l'ordre de création
    post: Tache | None  # dernier post de la lignée
    publication: Publication | None
    etat: str
    quand: datetime

    @property
    def brief(self) -> Tache:
        """Dernier brief de la lignée (celui qui porte les corrections éventuelles)."""
        return [t for t in self.lignee if t.type in BRIEFS][-1]

    @property
    def pret(self) -> bool:
        return self.etat == "Prêt à publier"


@dataclass
class ElementTache:
    tache: Tache | None  # None pour une tâche prévue mais pas encore créée
    type: str
    agent: str
    etat: str
    quand: datetime


@dataclass
class Jour:
    date: date
    elements: list = field(default_factory=list)


def lundi_de(jour: date) -> date:
    return jour - timedelta(days=jour.weekday())


def etat_publication(lignee: list[Tache], post: Tache | None, publication: Publication | None) -> str:
    if publication:
        return "Publié"
    if any(t.type == "demande_publication" and t.statut == "échec" for t in lignee):
        return "Échec de publication"
    en_cours = [t for t in lignee if t.type in PRODUCTION and t.statut in EN_COURS]
    if en_cours and all(t.statut == "à_faire" and t.creneau != "jour" for t in en_cours):
        return "Prévu cette nuit"
    if en_cours:
        return "En relecture" if all(t.type == "post_brouillon" for t in en_cours) else "En production"
    if post is None:
        return "En échec" if any(t.type in PRODUCTION and t.statut == "échec" for t in lignee) else "En production"
    return {"validé": "Prêt à publier", "à_valider": "À valider", "refusé": "Refusé"}.get(post.statut, "En échec")


class Calendrier:
    def __init__(self, file: File, publications: Publications):
        self.file = file
        self.publications = publications
        self._taches = {t.id: t for t in file.lister()}
        self._enfants: dict[int, list[Tache]] = {}
        for tache in self._taches.values():
            if tache.parent_id:
                self._enfants.setdefault(tache.parent_id, []).append(tache)

    def _lignee(self, racine: Tache) -> list[Tache]:
        lignee, a_explorer = [], [racine]
        while a_explorer:
            tache = a_explorer.pop()
            lignee.append(tache)
            a_explorer.extend(self._enfants.get(tache.id, []))
        return sorted(lignee, key=lambda t: t.id)

    def _est_racine(self, tache: Tache) -> bool:
        """Brief qui ouvre une lignée : pas issu de corrections (sinon c'est une nouvelle version)."""
        if tache.type not in BRIEFS:
            return False
        parent = self._taches.get(tache.parent_id) if tache.parent_id else None
        return parent is None or parent.type != "corrections"

    def publication(self, racine: Tache) -> ElementPublication:
        lignee = self._lignee(racine)
        posts = [t for t in lignee if t.type == "post_brouillon"]
        post = posts[-1] if posts else None
        publication = self.publications.lire(post.id) if post else None
        dernier_brief = [t for t in lignee if t.type in BRIEFS][-1]
        quand = post.contenu["horaire_prevu"] if post else dernier_brief.contenu["date_prevue"]
        return ElementPublication(racine, lignee, post, publication, etat_publication(lignee, post, publication),
                                  datetime.fromisoformat(quand).astimezone(FUSEAU))

    def racine_de(self, tache_id: int) -> Tache | None:
        """Brief racine de la lignée d'une tâche, ou None si la tâche n'appartient à aucune publication."""
        tache = self._taches.get(tache_id)
        while tache:
            if self._est_racine(tache):
                return tache
            tache = self._taches.get(tache.parent_id) if tache.parent_id else None
        return None

    def semaine(self, lundi: date, maintenant: datetime) -> list[Jour]:
        jours = [Jour(lundi + timedelta(days=i)) for i in range(7)]
        debut = datetime.combine(lundi, datetime.min.time(), FUSEAU)
        fin = debut + timedelta(days=7)

        def placer(element) -> None:
            if debut <= element.quand < fin:
                jours[(element.quand.date() - lundi).days].elements.append(element)

        dans_une_lignee = set()
        for tache in self._taches.values():
            if self._est_racine(tache):
                element = self.publication(tache)
                dans_une_lignee.update(t.id for t in element.lignee)
                if element.etat != "Refusé":  # abandonné : visible seulement dans l'historique
                    placer(element)
        for tache in self._taches.values():
            if tache.id not in dans_une_lignee:
                if tache.statut == "à_faire" and tache.creneau != "jour":  # attend la nuit : placée cette nuit
                    local = maintenant.astimezone(FUSEAU)  # avant 9 h, la nuit en cours ; sinon la suivante
                    minuit = datetime.combine(local.date() + timedelta(days=0 if local.hour < 9 else 1),
                                              datetime.min.time(), FUSEAU)
                    placer(ElementTache(tache, tache.type, tache.agent, "Prévu cette nuit", minuit))
                else:
                    quand = datetime.fromisoformat(tache.pris_le or tache.cree_le).astimezone(FUSEAU)
                    placer(ElementTache(tache, tache.type, tache.agent, tache.statut, quand))
        for evenement in EVENEMENTS:
            quand = datetime.combine(lundi + timedelta(days=evenement.jour), evenement.heure, FUSEAU)
            if quand > maintenant:
                placer(ElementTache(None, evenement.nom, AGENTS_EVENEMENTS[evenement.nom], "prévu", quand))
        for jour in jours:
            jour.elements.sort(key=lambda e: e.quand)
        return jours

