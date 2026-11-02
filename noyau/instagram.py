"""Client de l'API officielle de Meta pour publier sur Instagram (bibliothèque standard, aucune dépendance).

Publication en trois temps (documentation Meta « Content Publishing ») :
1. un conteneur par média (`POST /{compte}/media`) : image (`image_url` publique en JPEG) ou vidéo
   (`video_url` publique en MP4 : `media_type=REELS` seule, `VIDEO` dans un carrousel), puis un conteneur de
   carrousel qui les regroupe (`media_type=CAROUSEL`, `children`) ;
2. attente que Meta ait récupéré et traité les médias (`status_code` = FINISHED ; une vidéo prend plus de temps :
   une vérification toutes les 30 s pendant 6 minutes) ;
3. publication (`POST /{compte}/media_publish`).

L'étiquette « créé avec l'IA » (`is_ai_generated`) se pose sur le conteneur publié, jamais sur
les images d'un carrousel (Meta renvoie une erreur sinon).

Erreurs : jeton refusé, permission manquante ou média refusé → ErreurDefinitive (échec direct) ;
limite de débit ou de publications, panne de Meta ou du réseau → ErreurTemporaire (report).
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import timedelta

from noyau.erreurs import ErreurDefinitive, ErreurTemporaire

DELAI_RESEAU_SECONDES = 30
# Attente du traitement d'un conteneur : Meta conseille une vérification par minute pendant 5 minutes,
# les images sont en pratique prêtes en quelques secondes.
VERIFICATIONS_MAX = 12
PAUSE_VERIFICATION_SECONDES = 10
PAUSE_VERIFICATION_VIDEO_SECONDES = 30
CARROUSEL_MAX = 20
# Statistiques d'un post (API v22 et plus : `views` remplace impressions et plays).
METRIQUES = ("views", "reach", "likes", "comments", "shares", "saved", "follows")
REPORT_PAR_DEFAUT = timedelta(minutes=30)

# Codes d'erreur de l'API Graph.
JETON_INVALIDE = {190, 102}
PERMISSION = {10, 200, 3}
LIMITE_DEBIT = {4, 17, 32, 613, 80002}
LIMITE_PUBLICATION_SOUS_CODE = 2207042

# (méthode, URL, paramètres) → (code HTTP, réponse JSON)
Transport = Callable[[str, str, dict[str, str]], tuple[int, dict]]


def transport_urllib(methode: str, url: str, parametres: dict[str, str]) -> tuple[int, dict]:
    donnees = urllib.parse.urlencode(parametres)
    if methode == "GET":
        requete = urllib.request.Request(f"{url}?{donnees}", method="GET")
    else:
        requete = urllib.request.Request(url, data=donnees.encode(), method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(requete, timeout=DELAI_RESEAU_SECONDES) as reponse:
            return reponse.status, json.loads(reponse.read() or b"{}")
    except urllib.error.HTTPError as erreur:
        try:
            return erreur.code, json.loads(erreur.read() or b"{}")
        except ValueError:
            return erreur.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as erreur:
        raise ErreurTemporaire(f"Meta injoignable : {erreur}", REPORT_PAR_DEFAUT) from None


def _erreur(statut: int, reponse: dict) -> Exception:
    detail = reponse.get("error") or {}
    code, sous_code = detail.get("code"), detail.get("error_subcode")
    message = detail.get("error_user_msg") or detail.get("message") or f"réponse HTTP {statut}"
    if code in JETON_INVALIDE:
        return ErreurDefinitive(f"Jeton Instagram refusé ou expiré : {message}. "
                                "Générer un nouveau jeton dans l'app Meta et le mettre dans .env.")
    if code == 200 and "access blocked" in message.lower():
        return ErreurDefinitive(f"Accès à l'API bloqué par Meta ({message}) : l'application Meta ou le compte "
                                "Facebook est restreint. Voir l'alerte du tableau de bord de l'app "
                                "(developers.facebook.com) et se reconnecter à Facebook.")
    if code in PERMISSION:
        return ErreurDefinitive(f"Permission Instagram manquante : {message}")
    if code in LIMITE_DEBIT or sous_code == LIMITE_PUBLICATION_SOUS_CODE:
        return ErreurTemporaire(f"Limite de Meta atteinte : {message}", REPORT_PAR_DEFAUT)
    if statut >= 500 or detail.get("is_transient"):
        return ErreurTemporaire(f"Meta indisponible : {message}", REPORT_PAR_DEFAUT)
    return ErreurDefinitive(f"Instagram a refusé la demande : {message}")


class ClientInstagram:
    def __init__(self, url_api: str, compte_id: str, jeton: str, transport: Transport = transport_urllib,
                 attendre: Callable[[float], None] = time.sleep):
        self.url_api = url_api.rstrip("/")
        self.compte_id = compte_id
        self.jeton = jeton
        self.transport = transport
        self.attendre = attendre

    def _appel(self, methode: str, chemin: str, **parametres: str) -> dict:
        statut, reponse = self.transport(methode, f"{self.url_api}/{chemin}",
                                         {**parametres, "access_token": self.jeton})
        if statut >= 400 or "error" in reponse:
            raise _erreur(statut, reponse)
        return reponse

    # --- Lecture ---

    def verifier(self) -> dict:
        """Lecture du compte (aucune publication) : confirme que le jeton et l'identifiant fonctionnent."""
        return self._appel("GET", self.compte_id, fields="id,username")

    def abonnes(self) -> int:
        return int(self._appel("GET", self.compte_id, fields="followers_count")["followers_count"])

    def lien(self, media_id: str) -> str | None:
        try:
            return self._appel("GET", media_id, fields="permalink").get("permalink")
        except (ErreurDefinitive, ErreurTemporaire):
            return None  # le post est publié : un lien manquant ne doit pas faire échouer la tâche

    def medias(self, limite: int = 25) -> list[dict]:
        """Derniers posts du compte (du plus récent au plus ancien)."""
        champs = "id,caption,media_type,media_product_type,permalink,timestamp,media_url,thumbnail_url"
        return self._appel("GET", f"{self.compte_id}/media", fields=champs, limit=str(limite)).get("data", [])

    def statistiques(self, media: dict) -> dict[str, int]:
        """Statistiques d'un post en un appel. Les reels n'ont pas `follows` (refusé par l'API)."""
        metriques = list(METRIQUES)
        if media.get("media_product_type") == "REELS":
            metriques.remove("follows")
        donnees = self._appel("GET", f"{media['id']}/insights", metric=",".join(metriques)).get("data", [])
        return {d["name"]: d["values"][0]["value"] for d in donnees if d.get("values")}

    # --- Publication ---

    def _conteneur(self, **parametres: str) -> str:
        return self._appel("POST", f"{self.compte_id}/media", **parametres)["id"]

    def attendre_pret(self, conteneur_id: str, pause: float = PAUSE_VERIFICATION_SECONDES) -> None:
        for numero in range(VERIFICATIONS_MAX):
            etat = self._appel("GET", conteneur_id, fields="status_code,status").get("status_code")
            if etat in ("FINISHED", "PUBLISHED"):
                return
            if etat in ("ERROR", "EXPIRED"):
                raise ErreurDefinitive(f"Instagram n'a pas pu traiter le média (état {etat})")
            if numero < VERIFICATIONS_MAX - 1:
                self.attendre(pause)
        raise ErreurTemporaire("Instagram traite encore le média : nouvel essai plus tard", timedelta(minutes=5))

    def preparer(self, medias: list[tuple[str, str]], legende: str, etiquette_ia: bool) -> str:
        """Crée les conteneurs (image seule, reel, ou carrousel) et attend qu'ils soient prêts.
        `medias` : (adresse publique, « image » ou « vidéo »), dans l'ordre. Renvoie le conteneur à publier."""
        if not medias:
            raise ErreurDefinitive("Instagram exige au moins une image : un post texte seul ne peut pas y être publié")
        if len(medias) > CARROUSEL_MAX:
            raise ErreurDefinitive(f"Carrousel de {len(medias)} médias : Instagram en accepte {CARROUSEL_MAX} au plus")
        ia = {"is_ai_generated": "true"} if etiquette_ia else {}
        video = any(type_media == "vidéo" for _, type_media in medias)
        pause = PAUSE_VERIFICATION_VIDEO_SECONDES if video else PAUSE_VERIFICATION_SECONDES
        if len(medias) == 1:
            url, type_media = medias[0]
            source = {"media_type": "REELS", "video_url": url} if type_media == "vidéo" else {"image_url": url}
            conteneur = self._conteneur(**source, caption=legende, **ia)
        else:
            enfants = [self._conteneur(**({"media_type": "VIDEO", "video_url": url} if type_media == "vidéo"
                                          else {"image_url": url}), is_carousel_item="true")
                       for url, type_media in medias]
            for enfant in enfants:
                self.attendre_pret(enfant, pause)
            conteneur = self._conteneur(media_type="CAROUSEL", children=",".join(enfants), caption=legende, **ia)
        self.attendre_pret(conteneur, pause)
        return conteneur

    def publier(self, conteneur_id: str) -> str:
        """Publie un conteneur prêt. Renvoie l'identifiant du post Instagram."""
        return self._appel("POST", f"{self.compte_id}/media_publish", creation_id=conteneur_id)["id"]
