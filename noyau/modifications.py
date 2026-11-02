"""Demandes de modification de l'humain, sans refus.

L'humain peut demander une retouche sur un post à valider, ou validé mais pas encore publié, sans le refuser :
ni analyse par agent_erreurs, ni leçon proposée, rien n'est compté comme un refus.

- Portée « texte » : le visuel est gardé ; un message `modification_texte` part vers le rédacteur, qui retouche
  le texte actuel (mêmes médias, même horaire). Un seul appel à Claude.
- Portée « tout » : des `corrections` (origine « humain ») partent vers l'orchestrateur, qui relance le brief
  d'origine ; les médias créés partent en corbeille, sans attendre d'analyse.

Dans les deux cas, le post d'origine sort de la file de validation (et ne peut plus être publié) : il passe
« refusé » dans la file, avec la demande en commentaire, et la table `modifications` le distingue d'un vrai
refus. La nouvelle version repasse par le relecteur puis par la validation. La demande peut aussi devenir une
règle pour les prochains contenus (leçon d'origine « humain », active tout de suite).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from noyau.base import transaction
from noyau.creneaux import Creneaux
from noyau.file import File, Tache
from noyau.horloge import Horloge, maintenant_utc
from noyau.lecons import LeconErreur, Lecons
from noyau.messages import Corrections, ModificationTexte
from noyau.stockage import Bibliotheque, StockageErreur

Portee = Literal["texte", "tout"]
LONGUEUR_MAX = 1000
PREFIXE = "Modification demandée"


class ModificationErreur(Exception):
    pass


@dataclass(frozen=True)
class Modification:
    id: int
    post_id: int
    tache_id: int
    consignes: str
    portee: str
    lecon_id: int | None
    cree_le: str


class Modifications:
    def __init__(self, conn: sqlite3.Connection, file: File, biblio: Bibliotheque, lecons: Lecons,
                 creneaux: Creneaux, horloge: Horloge = maintenant_utc):
        self.conn = conn
        self.file = file
        self.biblio = biblio
        self.lecons = lecons
        self.creneaux = creneaux
        self.horloge = horloge

    def modifiables(self) -> list[Tache]:
        """Posts à valider, ou validés sans publication ni demande de publication, par horaire prévu."""
        publies = {l["tache_id"] for l in self.conn.execute("SELECT tache_id FROM publications")}
        en_publication = {t.contenu["post_id"] for t in self.file.lister() if t.type == "demande_publication"}
        posts = [t for statut in ("à_valider", "validé") for t in self.file.lister(statut=statut)
                 if t.type == "post_brouillon" and t.id not in publies and t.id not in en_publication]
        return sorted(posts, key=lambda t: datetime.fromisoformat(t.contenu["horaire_prevu"]))

    def lire(self, post_id: int) -> Modification | None:
        ligne = self.conn.execute("SELECT * FROM modifications WHERE post_id = ?", (post_id,)).fetchone()
        return Modification(**dict(ligne)) if ligne else None

    def posts_modifies(self) -> set[int]:
        return {l["post_id"] for l in self.conn.execute("SELECT post_id FROM modifications")}

    def demander(self, post_id: int, consignes: str, portee: Portee, immediat: bool = False,
                 en_regle: bool = False, agents_regle: list[str] | None = None) -> Modification:
        consignes = (consignes or "").strip()
        if not consignes:
            raise ModificationErreur("Décrivez la modification souhaitée")
        if len(consignes) > LONGUEUR_MAX:
            raise ModificationErreur(f"Demande trop longue ({len(consignes)} caractères, {LONGUEUR_MAX} au plus)")
        if portee not in ("texte", "tout"):
            raise ModificationErreur(f"Portée inconnue : {portee}")
        if post_id not in {t.id for t in self.modifiables()}:
            raise ModificationErreur(f"Contenu {post_id} non modifiable : seul un post à valider, ou validé et pas "
                                     "encore publié, peut l'être")
        post = self.file.lire(post_id)
        horaire = datetime.fromisoformat(post.contenu["horaire_prevu"])

        if en_regle and not agents_regle:
            raise ModificationErreur("Règle non ajoutée : choisissez au moins un agent concerné")

        with transaction(self.conn):
            # Revérifié dans la transaction : le worker peut avoir créé la demande de publication entre-temps.
            if self.conn.execute("SELECT 1 FROM publications WHERE tache_id = ?", (post_id,)).fetchone() or \
                    self.file.lire(post_id).statut not in ("à_valider", "validé") or \
                    self.conn.execute("SELECT 1 FROM taches WHERE type = 'demande_publication' "
                                      "AND json_extract(contenu, '$.post_id') = ?", (post_id,)).fetchone():
                raise ModificationErreur(f"Contenu {post_id} déjà publié ou retiré entre-temps")
            # Le post d'origine sort de la file (validé ou non) : il ne sera ni validé ni publié.
            self.conn.execute("UPDATE taches SET statut = 'refusé', commentaire = ?, maj_le = ? WHERE id = ?",
                              (f"{PREFIXE} : {consignes}", self._date(), post_id))
            if portee == "texte":
                creneau = "jour" if immediat else self.creneaux.decider(horaire)
                tache_id = self.file.creer(ModificationTexte(post_id=post_id, consignes=[consignes]),
                                           parent_id=post_id, creneau=creneau)
            else:
                tache_id = self.file.creer(
                    Corrections(contenu_id=post_id, problemes=[consignes], origine="humain", immediat=immediat),
                    agent="orchestrateur_marketing", parent_id=post_id)
            modification_id = self.conn.execute(
                "INSERT INTO modifications (post_id, tache_id, consignes, portee, cree_le) VALUES (?, ?, ?, ?, ?)",
                (post_id, tache_id, consignes, portee, self._date()),
            ).lastrowid

        if portee == "tout":
            self._jeter_medias(post)
        if en_regle:
            try:
                lecon_id = self.lecons.ajouter(consignes, agents_regle).id
            except LeconErreur as erreur:
                raise ModificationErreur(f"Modification demandée, mais règle non ajoutée : {erreur}") from None
            self.conn.execute("UPDATE modifications SET lecon_id = ? WHERE id = ?", (lecon_id, modification_id))
        return Modification(**dict(self.conn.execute("SELECT * FROM modifications WHERE id = ?",
                                                     (modification_id,)).fetchone()))

    def _jeter_medias(self, post: Tache) -> None:
        """Visuels créés refaits : en corbeille, purgés sous 24 h sans analyse (ce n'est pas un rejet)."""
        for asset_id in post.contenu.get("medias", []):
            try:
                asset = self.biblio.lire(asset_id)
                if asset.origine == "généré" and asset.etat == "actif":
                    self.biblio.mettre_en_corbeille(asset_id, "refusé")
                    self.biblio.marquer_analyse(asset_id)
            except StockageErreur:
                pass

    def _date(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()
