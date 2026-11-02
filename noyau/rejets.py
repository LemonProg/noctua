"""Circuit des rejets : un contenu refusé par l'humain ou marqué « ne pas utiliser ».

1. La tâche passe « refusé » (avec le commentaire de l'humain).
2. Les médias produits par les agents partent en corbeille, où ils attendent leur analyse.
   Les rushs d'origine restent dans la bibliothèque : ils peuvent resservir.
3. Un message `contenu_rejete` part vers agent_erreurs, pour comprendre l'erreur et proposer une leçon.
4. Pour un refus (pas un écart), des `corrections` partent vers l'orchestrateur, qui fait refaire le post.
"""

from typing import Literal

from noyau.file import File, Tache
from noyau.messages import ContenuRejete, Corrections
from noyau.stockage import Bibliotheque, StockageErreur

COMMENTAIRE_ECART = "Écarté : ne pas utiliser."


def rejeter(file: File, biblio: Bibliotheque, tache_id: int,
            origine: Literal["refus_humain", "ne_pas_utiliser"], commentaire: str | None = None) -> Tache:
    commentaire = (commentaire or "").strip()
    if origine == "ne_pas_utiliser":
        commentaire = f"{COMMENTAIRE_ECART} {commentaire}".strip()
    tache = file.refuser(tache_id, commentaire)  # contrôle la transition et le commentaire obligatoire

    motif = "refusé" if origine == "refus_humain" else "ne_pas_utiliser"
    for asset_id in tache.contenu.get("medias", []):
        try:
            asset = biblio.lire(asset_id)
            if asset.origine == "généré" and asset.etat == "actif":
                biblio.mettre_en_corbeille(asset_id, motif)
        except StockageErreur:
            pass  # média déjà supprimé : rien à mettre en corbeille

    brief = file.lire(tache.parent_id).contenu if tache.parent_id else None
    if brief and "prompt_ou_script" in brief:
        # Post issu d'un visuel créé : le brief d'origine et le prompt du visuel servent à l'analyse.
        brief = file.lire(brief["brief_id"]).contenu | {"visuel_cree": brief["prompt_ou_script"]}
    file.creer(ContenuRejete(contenu_id=tache.id, origine=origine, contenu=tache.contenu, brief=brief,
                             commentaire=commentaire or None), parent_id=tache.id)
    if origine == "refus_humain" and tache.type == "post_brouillon":
        # L'orchestrateur refait le post avec le commentaire ; « ne pas utiliser » n'appelle pas de nouvelle version.
        file.creer(Corrections(contenu_id=tache.id, problemes=[commentaire], origine="humain"),
                   agent="orchestrateur_marketing", parent_id=tache.id)
    return tache
