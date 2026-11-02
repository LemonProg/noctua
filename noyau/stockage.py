"""Stockage des rushs et contenus produits.

Deux niveaux :
- `StockageLocal` : les fichiers sur le disque du VPS. Seule cette classe changera
  le jour où un S3 sera branché (même interface que `Stockage`).
- `Bibliotheque` : les fichiers et leur suivi dans la table `assets`
  (corbeille, restauration, suppression). C'est elle que les agents utilisent.
"""

import shutil
import subprocess
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Protocol

from noyau.base import transaction
from noyau.config import Config
from noyau.horloge import Horloge, maintenant_utc
from noyau.images import FORMATS_ENREGISTRES, ImageErreur, nettoyer_metadonnees, vignette

GO = 1024**3

EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".avif", ".gif", ".tif", ".tiff"},
    "vidéo": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
}
MOTIFS_CORBEILLE = ("refusé", "ne_pas_utiliser", "relecteur")


class StockageErreur(Exception):
    pass


class EspaceInsuffisant(StockageErreur):
    pass


class AssetIntrouvable(StockageErreur):
    pass


class Stockage(Protocol):
    def ecrire(self, chemin: str, source: Path) -> None: ...
    def chemin_local(self, chemin: str) -> Path: ...
    def supprimer(self, chemin: str) -> None: ...
    def espace_libre(self) -> int: ...


class StockageLocal:
    def __init__(self, racine: Path, espace_libre: Callable[[], int] | None = None):
        self.racine = racine
        self.racine.mkdir(parents=True, exist_ok=True)
        self._espace_libre = espace_libre or (lambda: shutil.disk_usage(self.racine).free)

    def _resoudre(self, chemin: str) -> Path:
        complet = (self.racine / chemin).resolve()
        if not complet.is_relative_to(self.racine.resolve()):
            raise StockageErreur(f"Chemin hors du stockage : {chemin}")
        return complet

    def ecrire(self, chemin: str, source: Path) -> None:
        cible = self._resoudre(chemin)
        cible.parent.mkdir(parents=True, exist_ok=True)
        partiel = cible.with_name(cible.name + ".partiel")
        try:
            shutil.copyfile(source, partiel)
            partiel.replace(cible)
        finally:
            partiel.unlink(missing_ok=True)

    def chemin_local(self, chemin: str) -> Path:
        complet = self._resoudre(chemin)
        if not complet.is_file():
            raise AssetIntrouvable(f"Fichier absent du stockage : {chemin}")
        return complet

    def supprimer(self, chemin: str) -> None:
        self._resoudre(chemin).unlink(missing_ok=True)

    def espace_libre(self) -> int:
        return self._espace_libre()


@dataclass(frozen=True)
class AssetInfo:
    id: int
    origine: str
    type: str
    chemin: str
    nom_original: str | None
    taille_octets: int
    tache_id: int | None
    etat: str
    motif_corbeille: str | None
    analyse: str
    genere_par_ia: bool
    cree_le: str
    corbeille_le: str | None
    publie_le: str | None
    supprime_le: str | None


def type_de(fichier: Path) -> str:
    extension = fichier.suffix.lower()
    for type_media, extensions in EXTENSIONS.items():
        if extension in extensions:
            return type_media
    return "autre"


class Bibliotheque:
    def __init__(self, conn: sqlite3.Connection, stockage: Stockage, seuil_refus_octets: int,
                 horloge: Horloge = maintenant_utc, dossier_temp: Path | None = None):
        self.conn = conn
        self.stockage = stockage
        self.seuil_refus_octets = seuil_refus_octets
        self.horloge = horloge
        self.dossier_temp = dossier_temp or Path(tempfile.gettempdir()) / "agence-ia"

    @classmethod
    def depuis_config(cls, conn: sqlite3.Connection, config: Config, horloge: Horloge = maintenant_utc):
        return cls(conn, StockageLocal(config.dossier_contenus), int(config.disque_seuil_refus_go * GO), horloge,
                   config.dossier_temp)

    def _date(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()

    # --- Ajout et lecture ---

    def ajouter(self, fichier: Path, origine: str, *, tache_id: int | None = None,
                genere_par_ia: bool = False) -> int:
        """Copie le fichier dans le stockage et l'enregistre. Le fichier source n'est pas modifié.

        Les photos de rush sont enregistrées sans leurs métadonnées (EXIF, position GPS).
        """
        if not fichier.is_file():
            raise StockageErreur(f"Fichier introuvable : {fichier}")
        type_media = type_de(fichier)
        if origine == "rush" and type_media == "autre":
            raise StockageErreur(f"Format non pris en charge pour un rush : {fichier.name} (photos et vidéos uniquement)")
        if origine == "rush" and type_media == "image":
            with dossier_de_travail(self.dossier_temp) as travail:
                suffixe = fichier.suffix.lower() if fichier.suffix.lower() in FORMATS_ENREGISTRES else ".jpg"
                propre = travail / f"photo{suffixe}"
                try:
                    nettoyer_metadonnees(fichier, propre)
                except ImageErreur as erreur:
                    raise StockageErreur(str(erreur)) from None
                return self._enregistrer(propre, fichier.name, origine, type_media, tache_id, genere_par_ia)
        return self._enregistrer(fichier, fichier.name, origine, type_media, tache_id, genere_par_ia)

    def _enregistrer(self, fichier: Path, nom_original: str, origine: str, type_media: str, tache_id: int | None,
                     genere_par_ia: bool) -> int:
        taille = fichier.stat().st_size
        restant = self.stockage.espace_libre() - taille
        if restant < self.seuil_refus_octets:
            raise EspaceInsuffisant(
                f"Espace disque insuffisant pour {nom_original} ({taille / GO:.2f} Go) : il resterait "
                f"{max(restant, 0) / GO:.2f} Go, sous le seuil de {self.seuil_refus_octets / GO:.2f} Go. "
                "Videz la corbeille ou libérez de la place."
            )

        date = self.horloge().astimezone(UTC)
        dossier = "rushs" if origine == "rush" else "generes"
        chemin = f"{dossier}/{date:%Y/%m}/{uuid.uuid4().hex}{fichier.suffix.lower()}"
        self.stockage.ecrire(chemin, fichier)
        try:
            return self.conn.execute(
                "INSERT INTO assets (origine, type, chemin, nom_original, taille_octets, tache_id, genere_par_ia, cree_le) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (origine, type_media, chemin, nom_original, taille, tache_id, int(genere_par_ia), date.isoformat()),
            ).lastrowid
        except BaseException:
            self.stockage.supprimer(chemin)
            raise

    def lire(self, asset_id: int) -> AssetInfo:
        ligne = self.conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if ligne is None:
            raise AssetIntrouvable(f"Asset {asset_id} introuvable")
        valeurs = dict(ligne)
        valeurs["genere_par_ia"] = bool(valeurs["genere_par_ia"])
        return AssetInfo(**valeurs)

    def chemin_local(self, asset_id: int) -> Path:
        asset = self.lire(asset_id)
        if asset.etat == "supprimé":
            raise AssetIntrouvable(f"Asset {asset_id} supprimé")
        return self.stockage.chemin_local(asset.chemin)

    def chemin_vignette(self, asset_id: int) -> Path:
        """Version légère d'une image pour l'affichage, créée au premier appel puis conservée.
        Pour une vidéo, une image tirée de la vidéo (FFmpeg). Si rien n'est lisible, le fichier d'origine."""
        original = self.chemin_local(asset_id)
        asset = self.lire(asset_id)
        if asset.type not in ("image", "vidéo"):
            return original
        chemin = _chemin_vignette(asset.chemin)
        try:
            return self.stockage.chemin_local(chemin)
        except AssetIntrouvable:
            pass
        with dossier_de_travail(self.dossier_temp) as travail:
            fichier = travail / "vignette.webp"
            source = original
            try:
                if asset.type == "vidéo":
                    source = image_de_video(original, travail / "image.png")
                vignette(source, fichier)
            except (ImageErreur, OSError, subprocess.SubprocessError):
                return original
            self.stockage.ecrire(chemin, fichier)
        return self.stockage.chemin_local(chemin)

    def lister(self, etat: str = "actif", origine: str | None = None) -> list[AssetInfo]:
        requete, parametres = "SELECT id FROM assets WHERE etat = ?", [etat]
        if origine:
            requete += " AND origine = ?"
            parametres.append(origine)
        return [self.lire(l["id"]) for l in self.conn.execute(requete + " ORDER BY id", parametres)]

    # --- Corbeille ---

    def mettre_en_corbeille(self, asset_id: int, motif: str) -> AssetInfo:
        """Un contenu généré attend son analyse par agent_erreurs ; un rush n'en a pas besoin."""
        if motif not in MOTIFS_CORBEILLE:
            raise StockageErreur(f"Motif de corbeille inconnu : {motif}")
        with transaction(self.conn):
            asset = self.lire(asset_id)
            if asset.etat != "actif":
                raise StockageErreur(f"Asset {asset_id} n'est pas actif (état : {asset.etat})")
            analyse = "non_requise" if asset.origine == "rush" else "en_attente"
            if asset.analyse == "terminée":
                analyse = "terminée"
            self.conn.execute(
                "UPDATE assets SET etat = 'corbeille', motif_corbeille = ?, corbeille_le = ?, analyse = ? WHERE id = ?",
                (motif, self._date(), analyse, asset_id),
            )
        return self.lire(asset_id)

    def restaurer(self, asset_id: int) -> AssetInfo:
        with transaction(self.conn):
            asset = self.lire(asset_id)
            if asset.etat != "corbeille":
                raise StockageErreur(f"Asset {asset_id} n'est pas dans la corbeille (état : {asset.etat})")
            self.conn.execute(
                "UPDATE assets SET etat = 'actif', motif_corbeille = NULL, corbeille_le = NULL WHERE id = ?",
                (asset_id,),
            )
        return self.lire(asset_id)

    def marquer_publie(self, asset_id: int) -> AssetInfo:
        """Date de publication : point de départ de la suppression après publication."""
        asset = self.lire(asset_id)
        if asset.etat != "actif":
            raise StockageErreur(f"Asset {asset_id} n'est pas actif (état : {asset.etat})")
        self.conn.execute("UPDATE assets SET publie_le = COALESCE(publie_le, ?) WHERE id = ?", (self._date(), asset_id))
        return self.lire(asset_id)

    def marquer_analyse(self, asset_id: int) -> AssetInfo:
        """Appelé une fois l'analyse par agent_erreurs terminée : le contenu peut alors être purgé."""
        self.conn.execute("UPDATE assets SET analyse = 'terminée' WHERE id = ? AND analyse = 'en_attente'", (asset_id,))
        return self.lire(asset_id)

    def supprimer(self, asset_id: int) -> AssetInfo:
        """Suppression définitive, réservée aux assets en corbeille ou déjà publiés."""
        asset = self.lire(asset_id)
        if asset.etat == "supprimé":
            return asset
        if asset.etat != "corbeille" and asset.publie_le is None:
            raise StockageErreur(f"Asset {asset_id} actif et non publié : suppression refusée")
        self.stockage.supprimer(asset.chemin)
        self.stockage.supprimer(_chemin_vignette(asset.chemin))
        self.conn.execute(
            "UPDATE assets SET etat = 'supprimé', motif_corbeille = NULL, corbeille_le = NULL, supprime_le = ? "
            "WHERE id = ?",
            (self._date(), asset_id),
        )
        return self.lire(asset_id)


def image_de_video(video: Path, cible: Path, instant: float = 1.0) -> Path:
    """Une image de la vidéo (vignette), extraite avec FFmpeg."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{instant:g}", "-i", str(video), "-frames:v", "1",
                    str(cible)], capture_output=True, timeout=120, check=True)
    if not cible.exists():
        raise ImageErreur(f"Aucune image extraite de {video.name}")
    return cible


def _chemin_vignette(chemin: str) -> str:
    return f"vignettes/{chemin}.webp"


# --- Fichiers de travail ---

@contextmanager
def dossier_de_travail(racine: Path) -> Iterator[Path]:
    """Dossier temporaire pour une tâche, supprimé à la sortie, même en cas d'erreur."""
    racine.mkdir(parents=True, exist_ok=True)
    dossier = Path(tempfile.mkdtemp(prefix="tache-", dir=racine))
    try:
        yield dossier
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


def nettoyer_travail(racine: Path, garder: Collection[str] = ()) -> int:
    """Au démarrage du worker : supprime les restes laissés par un plantage, sauf les éléments nommés dans
    `garder` (étapes déjà payées d'une tâche à reprendre). Renvoie le nombre d'éléments supprimés."""
    if not racine.is_dir():
        return 0
    elements = [e for e in racine.iterdir() if e.name not in garder]
    for element in elements:
        if element.is_dir():
            shutil.rmtree(element, ignore_errors=True)
        else:
            element.unlink(missing_ok=True)
    return len(elements)
