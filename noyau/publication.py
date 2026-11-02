"""Publication des contenus validés.

Phase 5a : export manuel (archive ZIP des médias en JPEG + texte) et « marquer comme publié ».
Phase 5b : publication automatique par agent_publication, quand l'horaire prévu arrive.
Seul un `post_brouillon` au statut « validé » peut être exporté ou publié, et une seule fois :
la publication par l'API est réservée en base juste avant l'appel irréversible à Meta.
La date de publication fait partir le délai de suppression (7 jours) des médias produits par
l'agence ; les rushs restent dans la bibliothèque pour pouvoir resservir.
"""

import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

from noyau.base import transaction
from noyau.file import File, Tache
from noyau.horloge import Horloge, maintenant_utc
from noyau.images import en_jpeg
from noyau.stockage import Bibliotheque

FUSEAU = ZoneInfo("Europe/Paris")
# Plage horaire de publication automatique (heure de Paris) : un post en retard n'est jamais publié la nuit.
PLAGE_PUBLICATION = (time(8, 0), time(22, 0))
# Au-delà, l'archive passe de la mémoire à un fichier temporaire (vidéos).
TAILLE_MEMOIRE = 20 * 1024 * 1024


class PublicationErreur(Exception):
    pass


@dataclass(frozen=True)
class Publication:
    tache_id: int
    mode: str
    identifiant_externe: str | None
    publie_le: str
    lien: str | None = None


class Publications:
    def __init__(self, file: File, biblio: Bibliotheque, horloge: Horloge = maintenant_utc):
        self.file = file
        self.biblio = biblio
        self.conn: sqlite3.Connection = file.conn
        self.horloge = horloge

    # --- Lecture ---

    def lire(self, tache_id: int) -> Publication | None:
        ligne = self.conn.execute(
            "SELECT tache_id, mode, identifiant_externe, publie_le, lien FROM publications WHERE tache_id = ?", (tache_id,)
        ).fetchone()
        return Publication(**dict(ligne)) if ligne else None

    def a_publier(self) -> list[Tache]:
        """Contenus validés pas encore publiés, du plus proche au plus lointain horaire prévu."""
        publies = {l["tache_id"] for l in self.conn.execute("SELECT tache_id FROM publications")}
        taches = [t for t in self.file.lister(statut="validé")
                  if t.type == "post_brouillon" and t.id not in publies]
        return sorted(taches, key=lambda t: datetime.fromisoformat(t.contenu["horaire_prevu"]))

    def a_echeance(self, maintenant: datetime) -> list[Tache]:
        """Posts à publier maintenant : validés, non publiés, horaire atteint, dans la plage de jour,
        et sans demande de publication déjà créée (une demande en échec se relance à la main)."""
        local = maintenant.astimezone(FUSEAU).time()
        if not PLAGE_PUBLICATION[0] <= local < PLAGE_PUBLICATION[1]:
            return []
        demandes = {t.contenu["post_id"] for t in self.file.lister() if t.type == "demande_publication"}
        return [t for t in self.a_publier()
                if datetime.fromisoformat(t.contenu["horaire_prevu"]) <= maintenant and t.id not in demandes]

    def post_publiable(self, tache_id: int) -> Tache:
        """Contrôle avant publication : post validé et pas encore publié."""
        tache = self._post_valide(tache_id)
        if self.lire(tache_id):
            raise PublicationErreur(f"Contenu {tache_id} déjà publié")
        return tache

    def _post_valide(self, tache_id: int) -> Tache:
        tache = self.file.lire(tache_id)
        if tache.type != "post_brouillon":
            raise PublicationErreur(f"Tâche {tache_id} : seul un post peut être publié")
        if tache.statut != "validé":
            raise PublicationErreur(f"Tâche {tache_id} : contenu non validé (statut « {tache.statut} »)")
        return tache

    # --- Export manuel ---

    def exporter(self, tache_id: int, dossier_temp: Path) -> IO[bytes]:
        """Archive ZIP du post : médias numérotés dans l'ordre (images en JPEG) et texte.

        Renvoie un fichier temporaire ouvert, positionné au début, supprimé à sa fermeture.
        """
        tache = self._post_valide(tache_id)
        dossier_temp.mkdir(parents=True, exist_ok=True)
        archive = tempfile.SpooledTemporaryFile(max_size=TAILLE_MEMOIRE, dir=dossier_temp)
        try:
            with zipfile.ZipFile(archive, "w") as zip_:
                for numero, asset_id in enumerate(tache.contenu.get("medias", []), start=1):
                    asset = self.biblio.lire(asset_id)
                    source = self.biblio.chemin_local(asset_id)
                    if asset.type == "image":
                        with zip_.open(f"{numero:02d}.jpg", "w") as cible:
                            en_jpeg(source, cible)
                    else:
                        zip_.write(source, f"{numero:02d}{source.suffix.lower()}")
                horaire = datetime.fromisoformat(tache.contenu["horaire_prevu"]).astimezone(FUSEAU)
                zip_.writestr("texte.txt", f"{tache.contenu['texte']}\n\n---\nRéseau : {tache.contenu['reseau']}\n"
                                           f"Horaire prévu : {horaire:%d/%m/%Y %H:%M}\n",
                              compress_type=zipfile.ZIP_DEFLATED)
        except BaseException:
            archive.close()
            raise
        archive.seek(0)
        return archive

    # --- Publication ---

    def marquer_publie(self, tache_id: int, mode: str = "manuel",
                       identifiant_externe: str | None = None) -> Publication:
        self.reserver(tache_id, mode)
        try:
            return self.confirmer(tache_id, identifiant_externe)
        except BaseException:
            self.conn.execute("DELETE FROM publications WHERE tache_id = ?", (tache_id,))
            raise

    def reserver(self, tache_id: int, mode: str = "instagram") -> None:
        """Note la publication avant l'appel à Meta : une reprise ne pourra pas publier une seconde fois."""
        self._post_valide(tache_id)
        try:
            self.conn.execute("INSERT INTO publications (tache_id, mode, publie_le) VALUES (?, ?, ?)",
                              (tache_id, mode, self.horloge().astimezone(UTC).isoformat()))
        except sqlite3.IntegrityError:
            raise PublicationErreur(f"Contenu {tache_id} déjà publié") from None

    def annuler(self, tache_id: int) -> None:
        """Retire une réservation dont la publication n'a pas eu lieu."""
        self.conn.execute("DELETE FROM publications WHERE tache_id = ? AND identifiant_externe IS NULL "
                          "AND mode != 'manuel'", (tache_id,))

    def confirmer(self, tache_id: int, identifiant_externe: str | None = None, lien: str | None = None) -> Publication:
        """Publication faite : date définitive, et suppression des médias produits dans 7 jours (les rushs restent)."""
        tache = self.file.lire(tache_id)
        with transaction(self.conn):
            self.conn.execute("UPDATE publications SET identifiant_externe = ?, lien = ?, publie_le = ? WHERE tache_id = ?",
                              (identifiant_externe, lien, self.horloge().astimezone(UTC).isoformat(), tache_id))
            for asset_id in tache.contenu.get("medias", []):
                if self.biblio.lire(asset_id).origine != "rush":
                    self.biblio.marquer_publie(asset_id)
        return self.lire(tache_id)
