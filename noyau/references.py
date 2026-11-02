"""Références de style : réalisations passées de l'humain (ses posts Instagram), qui guident le graphiste.

Elles ne sont jamais publiées comme contenu : rangées à part des rushs, dans `references/` du stockage.
Les métadonnées sont retirées à l'import, comme pour les rushs.
"""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from noyau.horloge import Horloge, maintenant_utc
from noyau.images import ImageErreur, nettoyer_metadonnees
from noyau.stockage import Stockage, StockageErreur, dossier_de_travail


@dataclass(frozen=True)
class Reference:
    id: int
    chemin: str
    nom_original: str | None
    taille_octets: int
    ajoute_le: str


class References:
    def __init__(self, conn: sqlite3.Connection, stockage: Stockage, dossier_temp: Path,
                 horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.stockage = stockage
        self.dossier_temp = dossier_temp
        self.horloge = horloge

    def ajouter(self, fichier: Path) -> int:
        with dossier_de_travail(self.dossier_temp) as travail:
            propre = travail / "reference.jpg"
            try:
                nettoyer_metadonnees(fichier, propre)
            except ImageErreur as erreur:
                raise StockageErreur(str(erreur)) from None
            chemin = f"references/{uuid.uuid4().hex}.jpg"
            self.stockage.ecrire(chemin, propre)
            taille = propre.stat().st_size
        try:
            return self.conn.execute(
                "INSERT INTO references_style (chemin, nom_original, taille_octets, ajoute_le) VALUES (?, ?, ?, ?)",
                (chemin, fichier.name, taille, self.horloge().astimezone(UTC).isoformat()),
            ).lastrowid
        except BaseException:
            self.stockage.supprimer(chemin)
            raise

    def lire(self, reference_id: int) -> Reference:
        ligne = self.conn.execute("SELECT * FROM references_style WHERE id = ?", (reference_id,)).fetchone()
        if ligne is None:
            raise StockageErreur(f"Référence {reference_id} introuvable")
        return Reference(**dict(ligne))

    def lister(self) -> list[Reference]:
        return [Reference(**dict(l)) for l in self.conn.execute("SELECT * FROM references_style ORDER BY id")]

    def chemin_local(self, reference_id: int) -> Path:
        return self.stockage.chemin_local(self.lire(reference_id).chemin)

    def retirer(self, reference_id: int) -> None:
        """Retrait définitif, à la demande de l'humain (ce n'est pas un contenu : pas de corbeille)."""
        reference = self.lire(reference_id)
        self.stockage.supprimer(reference.chemin)
        self.conn.execute("DELETE FROM references_style WHERE id = ?", (reference_id,))
