"""Sauvegarde quotidienne de la base : tout l'historique de l'agence (posts, leçons, direction artistique,
statistiques, plannings) y est.

Copie cohérente même pendant que le worker écrit (API de sauvegarde de SQLite), compressée, horodatée ;
les SAUVEGARDES_CONSERVEES plus récentes sont gardées. Ni les médias (contenus, rushs) ni `.env` (secrets)
ne sont sauvegardés ici. Restauration : arrêter les services, décompresser une sauvegarde à la place de la base.
"""

import gzip
import shutil
import sqlite3
from datetime import UTC
from pathlib import Path

from noyau.config import Config
from noyau.horloge import Horloge, maintenant_utc

PREFIXE = "agence-"
SUFFIXE = ".sqlite3.gz"


def lister(dossier: Path) -> list[Path]:
    """Sauvegardes existantes, de la plus ancienne à la plus récente."""
    return sorted(dossier.glob(f"{PREFIXE}*{SUFFIXE}")) if dossier.is_dir() else []


def sauvegarder(config: Config, horloge: Horloge = maintenant_utc) -> Path:
    dossier = config.dossier_sauvegardes
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / f"{PREFIXE}{horloge().astimezone(UTC):%Y%m%d-%H%M%S}{SUFFIXE}"
    copie = dossier / f".{cible.name}.sqlite3"
    try:
        source = sqlite3.connect(config.chemin_base)
        destination = sqlite3.connect(copie)
        try:
            source.backup(destination)  # copie cohérente, même pendant une écriture du worker
        finally:
            destination.close()
            source.close()
        partiel = cible.with_name(cible.name + ".partiel")
        with copie.open("rb") as entree, gzip.open(partiel, "wb", compresslevel=6) as sortie:
            shutil.copyfileobj(entree, sortie)
        partiel.replace(cible)
    finally:
        copie.unlink(missing_ok=True)
    for ancienne in lister(dossier)[:-config.sauvegardes_conservees]:
        ancienne.unlink()
    return cible
