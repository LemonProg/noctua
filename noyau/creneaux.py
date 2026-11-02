"""Créneaux de production : les tâches gourmandes tournent la nuit, sans entamer la matinée de l'humain.

L'abonnement compte l'usage par fenêtres de 5 heures, ouvertes au premier appel. Un appel de l'agence à
4 h ouvrirait une fenêtre jusqu'à 9 h : la matinée de l'humain serait entamée. D'où la règle de la nuit
(de NUIT_DEBUT à ABONNEMENT_LIBRE_MAX) : un appel n'est permis que si la fenêtre qui le contient se termine
avant ABONNEMENT_LIBRE (8 h) pour une nouvelle production, avant ABONNEMENT_LIBRE_MAX (9 h, retard toléré)
pour la suite d'une chaîne déjà commencée. La fin de la fenêtre en cours est connue par le suivi du quota ;
sans fenêtre ouverte, un appel en ouvre une de 5 heures.

Créneau de chaque tâche :
- `jour` : peut tourner à tout moment (de nuit, sous la règle de la fenêtre) ;
- `nuit` : production qui peut attendre la nuit (décidée selon la date de publication) ;
- `nuit_suite` : suite d'une chaîne de nuit (texte, relecture, correction d'un post produit la nuit).

La nuit ne fait gagner que sur la fenêtre de 5 heures : la limite de la semaine compte autant de nuit.
"""

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from noyau.file import File, Tache
from noyau.horloge import Horloge, maintenant_utc
from noyau.quota import Quota

FUSEAU = ZoneInfo("Europe/Paris")
CRENEAUX = ("jour", "nuit", "nuit_suite")
DUREE_FENETRE = timedelta(hours=5)
# Temps laissé à une tâche lancée la nuit pour finir avant la fin de sa fenêtre (un visuel : quelques appels).
MARGE_AVANT_FIN = timedelta(minutes=40)
# Tâches sans date de publication : l'échéance est le lundi midi de la semaine préparée.
ECHEANCE_SEMAINE = time(12, 0)


class Creneaux:
    def __init__(self, conn: sqlite3.Connection | None = None, *, nuit_debut: time = time(0, 0),
                 abonnement_libre: time = time(8, 0), abonnement_libre_max: time = time(9, 0),
                 marge_validation: timedelta = timedelta(hours=2), horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.nuit_debut = nuit_debut
        self.libre = abonnement_libre
        self.libre_max = abonnement_libre_max
        self.marge_validation = marge_validation
        self.horloge = horloge

    @classmethod
    def depuis_config(cls, config, conn: sqlite3.Connection | None = None,
                      horloge: Horloge = maintenant_utc) -> "Creneaux":
        return cls(conn, nuit_debut=config.nuit_debut, abonnement_libre=config.abonnement_libre,
                   abonnement_libre_max=config.abonnement_libre_max,
                   marge_validation=timedelta(hours=config.marge_validation_heures), horloge=horloge)

    # --- La nuit ---

    def _dans(self, heure: time) -> bool:
        if self.nuit_debut <= self.libre_max:
            return self.nuit_debut <= heure < self.libre_max
        return heure >= self.nuit_debut or heure < self.libre_max  # nuit qui commence avant minuit

    def periode_nuit(self, maintenant: datetime | None = None) -> bool:
        return self._dans((maintenant or self.horloge()).astimezone(FUSEAU).time())

    def _matin(self, maintenant: datetime, heure: time) -> datetime:
        """Heure `heure` du matin qui termine la nuit en cours."""
        local = maintenant.astimezone(FUSEAU)
        jour = local.date() if local.time() < self.libre_max else local.date() + timedelta(days=1)
        return datetime.combine(jour, heure, FUSEAU)

    def fin_fenetre(self, maintenant: datetime) -> datetime:
        """Fin de la fenêtre de 5 heures qui contiendra un appel fait maintenant."""
        cinq_heures = Quota(self.conn, self.horloge).actuel().get("five_hour") if self.conn else None
        if cinq_heures and cinq_heures.reprise and cinq_heures.reprise > maintenant:
            return cinq_heures.reprise
        return maintenant + DUREE_FENETRE

    def autorises(self, maintenant: datetime | None = None) -> tuple[str, ...]:
        """Créneaux des tâches qui peuvent être lancées maintenant (aucun : l'agence attend)."""
        maintenant = maintenant or self.horloge()
        if not self.periode_nuit(maintenant):
            return ("jour",)
        fin = self.fin_fenetre(maintenant)
        if maintenant + MARGE_AVANT_FIN > fin:
            return ()
        if fin <= self._matin(maintenant, self.libre):
            return CRENEAUX
        if fin <= self._matin(maintenant, self.libre_max):
            return ("jour", "nuit_suite")
        return ()

    # --- Décision : la production peut-elle attendre la nuit ? ---

    def pret_le(self, maintenant: datetime | None = None) -> datetime:
        """Heure à laquelle un contenu confié maintenant à la nuit sera prêt à valider."""
        maintenant = maintenant or self.horloge()
        if self.periode_nuit(maintenant) and "nuit" in self.autorises(maintenant):
            return self._matin(maintenant, self.libre)
        local = maintenant.astimezone(FUSEAU)
        debut = datetime.combine(local.date(), self.nuit_debut, FUSEAU)
        if debut <= local:
            debut += timedelta(days=1)
        return self._matin(debut, self.libre)

    def plus_tot_publiable(self, maintenant: datetime | None = None) -> datetime:
        """Première publication qu'un contenu produit la nuit permet, validation comprise."""
        return self.pret_le(maintenant) + self.marge_validation

    def decider(self, date_publication: datetime, maintenant: datetime | None = None) -> str:
        """« nuit » si le contenu peut être produit la nuit et validé à temps, sinon « jour » (tout de suite)."""
        return "nuit" if self.plus_tot_publiable(maintenant) <= date_publication else "jour"

    # --- Réévaluation des tâches qui attendent la nuit ---

    def echeance(self, file: File, tache: Tache) -> datetime | None:
        """Date de publication visée par une tâche (la sienne ou celle de sa lignée)."""
        if tache.type in ("demande_veille", "demande_planning"):
            return datetime.combine(date.fromisoformat(tache.contenu["semaine"]), ECHEANCE_SEMAINE, FUSEAU)
        while tache:
            for champ in ("horaire_prevu", "date_prevue"):
                if tache.contenu.get(champ):
                    return datetime.fromisoformat(tache.contenu[champ])
            tache = file.lire(tache.parent_id) if tache.parent_id else None
        return None

    def reevaluer(self, file: File, maintenant: datetime | None = None) -> list[int]:
        """Les tâches de nuit qui ne peuvent plus attendre la nuit suivante passent en journée."""
        maintenant = maintenant or self.horloge()
        passees = []
        for tache in file.lister(statut="à_faire"):
            if tache.creneau == "jour" or "nuit" in self.autorises(maintenant):
                continue
            echeance = self.echeance(file, tache)
            if echeance and self.decider(echeance, maintenant) == "jour":
                file.conn.execute("UPDATE taches SET creneau = 'jour', maj_le = ? WHERE id = ?",
                                  (maintenant.astimezone(UTC).isoformat(), tache.id))
                passees.append(tache.id)
        return passees
