"""agent_publication : publie sur Instagram, par l'API officielle de Meta, un post validé par l'humain.

Entrée : demande_publication (créée par le worker quand l'horaire prévu d'un post validé arrive).
Aucun appel à Claude. Contrôles avant tout appel : publication automatique activée, post validé
et pas encore publié (vérifié deux fois : au début, puis par la réservation juste avant la publication).

Les images sont converties en JPEG, les vidéos (reels) gardées en MP4 ; chaque média est exposé à une adresse
publique au nom aléatoire, le temps que Meta le récupère, puis supprimé. Si le résultat de la publication est incertain (coupure
pendant l'appel), la tâche passe en échec avec un message : ne jamais risquer un double post.
"""

import secrets
import shutil
from pathlib import Path

from agents.base import Agent, Outils
from noyau.config import RACINE, Config, charger
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive, ErreurTemporaire
from noyau.file import Tache
from noyau.images import en_jpeg
from noyau.instagram import ClientInstagram
from noyau.publication import PublicationErreur, Publications

DOSSIER_PUBLIC = "public"


def dossier_public(config: Config) -> Path:
    """Fichiers exposés sans mot de passe à `{URL_PUBLIQUE}/public/<nom>` (voir app/web.py)."""
    return config.dossier_temp / DOSSIER_PUBLIC


class AgentPublication(Agent):
    id = "agent_publication"

    def __init__(self, racine: Path = RACINE, config: Config | None = None, client: ClientInstagram | None = None):
        super().__init__(racine)
        self._config = config
        self._client = client

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = charger()
        return self._config

    @property
    def client(self) -> ClientInstagram:
        if self._client is None:
            self._client = ClientInstagram(self.config.instagram_api_url, self.config.instagram_compte_id,
                                           self.config.instagram_jeton)
        return self._client

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if not self.config.publication_automatique:
            raise ErreurDefinitive("Publication automatique désactivée (PUBLICATION_AUTOMATIQUE) : "
                                   "télécharger le post depuis le calendrier et le publier à la main")
        publications = Publications(outils.file, outils.biblio)
        post_id = tache.message.post_id
        try:
            post = publications.post_publiable(post_id)
        except PublicationErreur as erreur:
            raise ErreurDefinitive(str(erreur)) from None
        if post.contenu["reseau"] != "instagram":
            raise ErreurDefinitive(f"Publication automatique sur {post.contenu['reseau']} non prise en charge")

        dossier = dossier_public(self.config)
        dossier.mkdir(parents=True, exist_ok=True)
        fichiers = []
        try:
            medias = []
            for asset_id in post.contenu.get("medias", []):
                type_media = outils.biblio.lire(asset_id).type
                if type_media not in ("image", "vidéo"):
                    raise ErreurDefinitive(f"Média {asset_id} : seules les images et les vidéos se publient")
                fichier = dossier / f"{secrets.token_urlsafe(24)}{'.mp4' if type_media == 'vidéo' else '.jpg'}"
                fichiers.append(fichier)
                if type_media == "vidéo":
                    shutil.copyfile(outils.biblio.chemin_local(asset_id), fichier)
                else:
                    en_jpeg(outils.biblio.chemin_local(asset_id), fichier)
                medias.append((f"{self.config.url_publique}/{DOSSIER_PUBLIC}/{fichier.name}", type_media))
            conteneur = self.client.preparer(medias, post.contenu["texte"], post.contenu.get("etiquette_ia", False))

            try:
                publications.reserver(post_id)
            except PublicationErreur as erreur:
                raise ErreurDefinitive(str(erreur)) from None
            try:
                media_id = self.client.publier(conteneur)
            except ErreurTemporaire as erreur:
                publications.annuler(post_id)
                raise ErreurDefinitive(f"Résultat incertain ({erreur}). Vérifier sur Instagram si le post est en "
                                       "ligne : sinon, relancer cette tâche depuis « Tâches en échec ».") from None
            except BaseException:
                publications.annuler(post_id)
                raise
            publications.confirmer(post_id, media_id, self.client.lien(media_id))
        finally:
            for fichier in fichiers:
                fichier.unlink(missing_ok=True)
        return "terminé"
