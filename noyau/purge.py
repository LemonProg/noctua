"""Purges automatiques et surveillance du disque.

- Corbeille : suppression après le délai (24 h), si l'élément est un rush ou un contenu déjà analysé.
  Un contenu pas encore analysé reste en corbeille, sans limite, tant que le disque le permet.
- Publication : suppression après le délai (7 jours) suivant la publication.
- Banque de bruitages : un son écarté (« à ne pas utiliser ») est supprimé après le délai de la corbeille.
- Disque sous le seuil d'alerte : alerte. Rien n'est supprimé sans analyse : c'est à l'humain
  de choisir (supprimer sans analyse, ou libérer de la place).
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from noyau.banque_sons import Banque
from noyau.config import Config
from noyau.stockage import GO, Bibliotheque

journal = logging.getLogger("agence.purge")


@dataclass(frozen=True)
class EtatDisque:
    libre_octets: int
    seuil_alerte_octets: int
    contenus_non_analyses: list[int]

    @property
    def sous_seuil(self) -> bool:
        return self.libre_octets < self.seuil_alerte_octets

    @property
    def alerte(self) -> str | None:
        if not self.sous_seuil:
            return None
        message = (
            f"Espace disque faible : {self.libre_octets / GO:.1f} Go libres "
            f"(seuil d'alerte : {self.seuil_alerte_octets / GO:.1f} Go)."
        )
        if self.contenus_non_analyses:
            message += (
                f" {len(self.contenus_non_analyses)} contenu(s) en corbeille attendent encore leur analyse : "
                "les supprimer sans analyse, ou libérer de la place."
            )
        return message


@dataclass
class RapportPurge:
    corbeille: list[int] = field(default_factory=list)
    publies: list[int] = field(default_factory=list)
    prolonges: list[int] = field(default_factory=list)
    sons: list[int] = field(default_factory=list)
    erreurs: dict[int, str] = field(default_factory=dict)
    octets_liberes: int = 0
    disque: EtatDisque | None = None


class Purge:
    def __init__(self, biblio: Bibliotheque, delai_corbeille: timedelta, delai_publication: timedelta,
                 seuil_alerte_octets: int):
        self.biblio = biblio
        self.delai_corbeille = delai_corbeille
        self.delai_publication = delai_publication
        self.seuil_alerte_octets = seuil_alerte_octets

    @classmethod
    def depuis_config(cls, biblio: Bibliotheque, config: Config) -> "Purge":
        return cls(
            biblio,
            timedelta(hours=config.corbeille_delai_heures),
            timedelta(days=config.publication_delai_jours),
            int(config.disque_seuil_alerte_go * GO),
        )

    def _limite(self, delai: timedelta) -> str:
        return (self.biblio.horloge().astimezone(UTC) - delai).isoformat()

    def _ids(self, requete: str, *parametres) -> list[int]:
        return [l["id"] for l in self.biblio.conn.execute(requete, parametres)]

    def etat_disque(self) -> EtatDisque:
        return EtatDisque(
            libre_octets=self.biblio.stockage.espace_libre(),
            seuil_alerte_octets=self.seuil_alerte_octets,
            contenus_non_analyses=self._ids(
                "SELECT id FROM assets WHERE etat = 'corbeille' AND analyse = 'en_attente' ORDER BY id"
            ),
        )

    def executer(self) -> RapportPurge:
        rapport = RapportPurge()
        limite_corbeille = self._limite(self.delai_corbeille)

        rapport.corbeille = self._ids(
            "SELECT id FROM assets WHERE etat = 'corbeille' AND corbeille_le <= ? "
            "AND analyse IN ('non_requise', 'terminée') ORDER BY id",
            limite_corbeille,
        )
        rapport.prolonges = self._ids(
            "SELECT id FROM assets WHERE etat = 'corbeille' AND corbeille_le <= ? AND analyse = 'en_attente' ORDER BY id",
            limite_corbeille,
        )
        rapport.publies = self._ids(
            "SELECT id FROM assets WHERE etat != 'supprimé' AND publie_le IS NOT NULL AND publie_le <= ? ORDER BY id",
            self._limite(self.delai_publication),
        )

        for asset_id in rapport.corbeille + rapport.publies:
            try:
                taille = self.biblio.lire(asset_id).taille_octets
                self.biblio.supprimer(asset_id)
                rapport.octets_liberes += taille
            except Exception as erreur:  # une erreur sur un fichier ne bloque pas les autres
                rapport.erreurs[asset_id] = str(erreur)
                journal.exception("Échec de suppression de l'asset %s", asset_id)
        rapport.corbeille = [i for i in rapport.corbeille if i not in rapport.erreurs]
        rapport.publies = [i for i in rapport.publies if i not in rapport.erreurs]

        try:
            rapport.sons = Banque(self.biblio.conn, self.biblio.stockage, self.biblio.dossier_temp,
                                  self.biblio.horloge).purger(self.delai_corbeille)
        except Exception:  # la banque ne bloque pas la purge des contenus
            journal.exception("Échec de la purge des sons écartés")

        rapport.disque = self.etat_disque()
        if rapport.prolonges:
            journal.info("%s contenu(s) gardé(s) en corbeille en attendant leur analyse", len(rapport.prolonges))
        if rapport.disque.alerte:
            journal.warning(rapport.disque.alerte)
        return rapport
