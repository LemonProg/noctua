"""Statistiques des posts Instagram : relevé automatique par l'API de Meta, import du CSV de Meta Business Suite,
saisie à la main, lecture.

Une ligne par post Instagram. Les posts de l'agence y sont rattachés (`post_id`) ; les autres posts
du compte sont gardés comme historique, utile à l'analyste. Les chiffres évoluent après la publication :
une nouvelle saisie ou un nouvel import remplace les précédents (date de relevé mise à jour).
Le relevé par l'API (source « api ») est fait chaque jour par le worker ; l'import CSV et la saisie restent
possibles. Un même post a le même identifiant dans l'API et dans l'export CSV : les sources se complètent.
"""

import csv
import difflib
import io
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from noyau.erreurs import ErreurDefinitive
from noyau.file import File
from noyau.horloge import Horloge, maintenant_utc
from noyau.publication import Publications

FUSEAU = ZoneInfo("Europe/Paris")
CHIFFRES = ("vues", "couverture", "jaime", "commentaires", "partages", "enregistrements", "abonnes")
LIBELLES = {"vues": "Vues", "couverture": "Couverture", "jaime": "J'aime", "commentaires": "Commentaires",
            "partages": "Partages", "enregistrements": "Enregistrements", "abonnes": "Abonnés gagnés"}
# Délai après publication à partir duquel l'interface rappelle de saisir les chiffres d'un post.
DELAI_RAPPEL = timedelta(hours=48)
# Rattachement d'une ligne du CSV à un post de l'agence par sa légende : ressemblance et écart de date.
RESSEMBLANCE_MIN = 0.8
ECART_MAX = timedelta(days=2)

# Colonnes de l'export Meta Business Suite (français ou anglais), sous forme normalisée → champ.
COLONNES = {
    "identifiant de la publication": "identifiant", "post id": "identifiant",
    "description": "description",
    "duree (s)": "duree_s", "duration (sec)": "duree_s",
    "heure de publication": "publie_le", "publish time": "publie_le",
    "permalien": "lien", "permalink": "lien",
    "type de publication": "type", "post type": "type",
    "vues": "vues", "views": "vues",
    "couverture": "couverture", "reach": "couverture",
    "mentions j'aime": "jaime", "likes": "jaime",
    "partages": "partages", "shares": "partages",
    "followers en plus": "abonnes", "follows": "abonnes",
    "commentaires": "commentaires", "comments": "commentaires",
    "enregistrements": "enregistrements", "saves": "enregistrements",
}
TYPES = {"ig image": "image", "ig carousel": "carrousel", "ig reel": "reel"}
TYPES_API = {"IMAGE": "image", "CAROUSEL_ALBUM": "carrousel", "VIDEO": "reel"}
# Métrique de l'API → colonne (les reels n'ont pas « follows »).
METRIQUES_API = {"views": "vues", "reach": "couverture", "likes": "jaime", "comments": "commentaires",
                 "shares": "partages", "saved": "enregistrements", "follows": "abonnes"}


class StatistiquesErreur(Exception):
    pass


@dataclass
class RapportImport:
    lignes: int = 0
    rattachees: list[int] = field(default_factory=list)  # posts de l'agence
    erreurs: list[str] = field(default_factory=list)


def _normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte.replace("’", "'")).encode("ascii", "ignore").decode()
    return " ".join(texte.lower().split())


def _legende(texte: str) -> str:
    """Légende comparable : sans mots-dièse, émojis, ponctuation ni casse."""
    texte = re.sub(r"#\w+", " ", texte or "")
    return " ".join(re.sub(r"[^\w\s]", " ", _normaliser(texte)).split())


def _date(brute: str) -> datetime:
    """Meta écrit les dates au format américain (MM/JJ/AAAA HH:MM), sans fuseau : heure de Paris supposée."""
    for format_date in ("%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(brute.strip(), format_date).replace(tzinfo=FUSEAU)
        except ValueError:
            continue
    raise ValueError(f"date illisible : {brute!r}")


def _entier(brut: str | None) -> int | None:
    brut = (brut or "").strip().replace(" ", "").replace(" ", "")
    return int(float(brut)) if brut else None


class Statistiques:
    def __init__(self, file: File, publications: Publications, horloge: Horloge = maintenant_utc):
        self.file = file
        self.publications = publications
        self.conn: sqlite3.Connection = file.conn
        self.horloge = horloge

    def _maintenant(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()

    # --- Lecture ---

    def du_post(self, post_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM statistiques WHERE post_id = ?", (post_id,)).fetchone()

    def lister(self, limite: int | None = None) -> list[sqlite3.Row]:
        """Du plus récent au plus ancien post."""
        requete = "SELECT * FROM statistiques ORDER BY publie_le DESC, id DESC"
        return self.conn.execute(requete + (f" LIMIT {int(limite)}" if limite else "")).fetchall()

    def dernier_releve(self) -> str | None:
        return self.conn.execute("SELECT max(releve_le) FROM statistiques").fetchone()[0]

    def a_saisir(self) -> list[int]:
        """Posts de l'agence publiés depuis plus de 48 h et encore sans chiffres."""
        limite = (self.horloge() - DELAI_RAPPEL).astimezone(UTC).isoformat()
        return [l["tache_id"] for l in self.conn.execute(
            "SELECT tache_id FROM publications WHERE publie_le <= ? AND tache_id NOT IN "
            "(SELECT post_id FROM statistiques WHERE post_id IS NOT NULL) ORDER BY publie_le", (limite,))]

    # --- Saisie à la main ---

    def saisir(self, post_id: int, **chiffres: int | None) -> sqlite3.Row:
        publication = self.publications.lire(post_id)
        if publication is None:
            raise StatistiquesErreur(f"Contenu {post_id} pas encore publié : pas de statistiques à saisir")
        inconnus = set(chiffres) - set(CHIFFRES)
        if inconnus:
            raise StatistiquesErreur(f"Chiffres inconnus : {', '.join(sorted(inconnus))}")
        if any(v is not None and v < 0 for v in chiffres.values()):
            raise StatistiquesErreur("Les chiffres ne peuvent pas être négatifs")
        post = self.file.lire(post_id)
        valeurs = {c: chiffres.get(c) for c in CHIFFRES}
        if self.du_post(post_id):
            self.conn.execute(f"UPDATE statistiques SET {', '.join(f'{c} = ?' for c in CHIFFRES)}, releve_le = ? "
                              "WHERE post_id = ?", (*valeurs.values(), self._maintenant(), post_id))
        else:
            self.conn.execute(
                f"INSERT INTO statistiques (post_id, publie_le, type, description, lien, {', '.join(CHIFFRES)}, "
                f"source, releve_le) VALUES (?, ?, ?, ?, ?, {', '.join('?' for _ in CHIFFRES)}, 'manuel', ?)",
                (post_id, publication.publie_le, "carrousel" if len(post.contenu.get("medias", [])) > 1 else "image",
                 post.contenu["texte"], publication.lien, *valeurs.values(), self._maintenant()))
        return self.du_post(post_id)

    # --- Import du CSV de Meta Business Suite ---

    def importer_csv(self, contenu: bytes | str) -> RapportImport:
        texte = contenu.decode("utf-8-sig") if isinstance(contenu, bytes) else contenu.lstrip("﻿")
        lecteur = csv.DictReader(io.StringIO(texte, newline=""))
        champs = {colonne: COLONNES.get(_normaliser(colonne)) for colonne in lecteur.fieldnames or []}
        if not {"identifiant", "publie_le"} <= set(champs.values()):
            raise StatistiquesErreur("Fichier non reconnu : il faut l'export des publications de Meta Business Suite "
                                     "(colonnes « Identifiant de la publication » et « Heure de publication »)")
        rapport = RapportImport()
        for numero, ligne in enumerate(lecteur, start=2):
            valeurs = {champ: ligne.get(colonne) for colonne, champ in champs.items() if champ}
            try:
                self._importer_ligne(valeurs, rapport)
            except (ValueError, sqlite3.Error) as erreur:
                rapport.erreurs.append(f"Ligne {numero} : {erreur}")
        return rapport

    def _importer_ligne(self, valeurs: dict, rapport: RapportImport) -> None:
        identifiant = (valeurs.get("identifiant") or "").strip()
        if not identifiant:
            raise ValueError("identifiant de la publication manquant")
        rapport.lignes += 1
        publie_le = _date(valeurs["publie_le"])
        ligne = {
            "identifiant_instagram": identifiant,
            "publie_le": publie_le.astimezone(UTC).isoformat(),
            "type": TYPES.get(_normaliser(valeurs.get("type") or ""), valeurs.get("type") or None),
            "description": (valeurs.get("description") or "").strip(),
            "lien": (valeurs.get("lien") or "").strip() or None,
            "duree_s": _entier(valeurs.get("duree_s")),
            **{c: _entier(valeurs.get(c)) for c in CHIFFRES},
        }
        self._enregistrer(ligne, publie_le, "csv", rapport)

    def _enregistrer(self, ligne: dict, publie_le: datetime, source: str, rapport: RapportImport) -> None:
        """Crée ou met à jour la ligne d'un post Instagram ; une valeur absente ne remplace pas une valeur connue."""
        identifiant = ligne["identifiant_instagram"]
        existante = self.conn.execute("SELECT id, post_id FROM statistiques WHERE identifiant_instagram = ?",
                                      (identifiant,)).fetchone()
        post_id = existante["post_id"] if existante and existante["post_id"] else \
            self._rattacher(identifiant, ligne["lien"], ligne["description"], publie_le)
        manuelle = self.du_post(post_id) if post_id and not existante else None
        cible = existante or manuelle
        if cible:
            connues = {c: v for c, v in ligne.items() if v is not None}
            self.conn.execute(f"UPDATE statistiques SET {', '.join(f'{c} = ?' for c in connues)}, post_id = ?, "
                              "source = ?, releve_le = ? WHERE id = ?",
                              (*connues.values(), post_id, source, self._maintenant(), cible["id"]))
        else:
            colonnes = list(ligne)
            self.conn.execute(f"INSERT INTO statistiques ({', '.join(colonnes)}, post_id, source, releve_le) "
                              f"VALUES ({', '.join('?' for _ in colonnes)}, ?, ?, ?)",
                              (*ligne.values(), post_id, source, self._maintenant()))
        if post_id:
            rapport.rattachees.append(post_id)

    # --- Relevé par l'API de Meta ---

    def relever_api(self, client, limite: int = 25) -> RapportImport:
        """Relève le nombre d'abonnés puis les statistiques des derniers posts (lectures seules, un appel par post)."""
        rapport = RapportImport()
        try:
            self.conn.execute("INSERT OR REPLACE INTO abonnes (date, nombre) VALUES (?, ?)",
                              (self._maintenant(), client.abonnes()))
        except (ErreurDefinitive, KeyError, ValueError) as erreur:
            rapport.erreurs.append(f"Abonnés : {erreur}")
        for media in client.medias(limite):
            try:
                chiffres = client.statistiques(media)
                publie_le = datetime.strptime(media["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
            except (ErreurDefinitive, KeyError, ValueError) as erreur:
                rapport.erreurs.append(f"Post {media.get('id')} : {erreur}")
                continue
            rapport.lignes += 1
            ligne = {
                "identifiant_instagram": media["id"],
                "publie_le": publie_le.astimezone(UTC).isoformat(),
                "type": "reel" if media.get("media_product_type") == "REELS"
                        else TYPES_API.get(media.get("media_type"), (media.get("media_type") or "").lower() or None),
                "description": (media.get("caption") or "").strip(),
                "lien": media.get("permalink"),
                "duree_s": None,
                "apercu": media.get("thumbnail_url") or media.get("media_url"),
                **{colonne: chiffres.get(metrique) for metrique, colonne in METRIQUES_API.items()},
            }
            self._enregistrer(ligne, publie_le, "api", rapport)
        return rapport

    def dernier_releve_api(self) -> datetime | None:
        valeur = self.conn.execute("SELECT max(releve_le) FROM statistiques WHERE source = 'api'").fetchone()[0]
        return datetime.fromisoformat(valeur) if valeur else None

    def post_star(self, jours: int = 30) -> tuple[sqlite3.Row, bool] | None:
        """Post le plus vu des `jours` derniers jours (à couverture égale, le plus touchant). Sans post récent, le
        meilleur des 12 derniers ; le booléen dit si le post est bien du mois."""
        debut = (self.horloge() - timedelta(days=jours)).astimezone(UTC).isoformat()
        ordre = "ORDER BY coalesce(vues, 0) DESC, coalesce(couverture, 0) DESC LIMIT 1"
        recent = self.conn.execute(f"SELECT * FROM statistiques WHERE publie_le >= ? {ordre}", (debut,)).fetchone()
        if recent:
            return recent, True
        ancien = self.conn.execute(f"SELECT * FROM (SELECT * FROM statistiques ORDER BY publie_le DESC LIMIT 12) {ordre}")
        ligne = ancien.fetchone()
        return (ligne, False) if ligne else None

    # --- Abonnés ---

    def abonnes(self) -> dict | None:
        """Dernier nombre d'abonnés et écart sur 7 et 30 jours. Faute d'un relevé assez ancien, l'écart part du
        premier relevé (`depuis` le dit) ; aucun écart avec un seul relevé."""
        releves = self.conn.execute("SELECT date, nombre FROM abonnes ORDER BY date").fetchall()
        if not releves:
            return None
        dernier = releves[-1]
        fin = datetime.fromisoformat(dernier["date"])
        resultat = {"nombre": dernier["nombre"], "le": fin}
        for jours in (7, 30):
            limite = fin - timedelta(days=jours)
            avant = [r for r in releves if datetime.fromisoformat(r["date"]) <= limite]
            reference = avant[-1] if avant else releves[0]
            resultat[f"j{jours}"] = None if reference is dernier else {
                "ecart": dernier["nombre"] - reference["nombre"],
                "depuis": None if avant else datetime.fromisoformat(reference["date"])}
        return resultat

    def courbe_abonnes(self, jours: int = 30) -> list[tuple[datetime, int]]:
        """Un point par jour (le dernier relevé du jour), sur les `jours` derniers jours."""
        debut = (self.horloge() - timedelta(days=jours)).astimezone(UTC).isoformat()
        parjour: dict = {}
        for r in self.conn.execute("SELECT date, nombre FROM abonnes WHERE date >= ? ORDER BY date", (debut,)):
            quand = datetime.fromisoformat(r["date"])
            parjour[quand.date()] = (quand, r["nombre"])
        return list(parjour.values())

    def _rattacher(self, identifiant: str, lien: str | None, description: str, publie_le: datetime) -> int | None:
        """Post de l'agence correspondant : identifiant ou lien Instagram, sinon légende ressemblante publiée
        à deux jours près. Un post déjà rattaché à une autre ligne n'est pas repris."""
        deja = {l["post_id"] for l in self.conn.execute(
            "SELECT post_id FROM statistiques WHERE post_id IS NOT NULL AND identifiant_instagram IS NOT NULL")}
        legende = _legende(description)
        meilleur, score_max = None, RESSEMBLANCE_MIN
        for publication in self.conn.execute("SELECT tache_id, identifiant_externe, lien, publie_le FROM publications"):
            post_id = publication["tache_id"]
            if post_id in deja:
                continue
            if identifiant == publication["identifiant_externe"] or (lien and lien == publication["lien"]):
                return post_id
            post = self.file.lire(post_id)
            dates = [datetime.fromisoformat(publication["publie_le"]), datetime.fromisoformat(post.contenu["horaire_prevu"])]
            if legende and min(abs(publie_le - d) for d in dates) <= ECART_MAX:
                score = difflib.SequenceMatcher(None, legende, _legende(post.contenu["texte"])).ratio()
                if score >= score_max:
                    meilleur, score_max = post_id, score
        return meilleur


def explication(rapport: dict | None, publie_le: datetime) -> str | None:
    """Ce que le dernier rapport de l'analyste dit d'un post, repéré par sa date (« 03/05 ») : d'abord dans
    « ce qui marche », sinon dans une phrase de la synthèse."""
    if not rapport:
        return None
    jour = publie_le.astimezone(FUSEAU).strftime("%d/%m")
    for element in rapport.get("ce_qui_marche", []):
        if jour in element:
            return element
    for phrase in re.split(r"(?<=[.!?])\s+", rapport.get("synthese") or ""):
        if jour in phrase:
            return phrase
    return None


def engagement(ligne) -> float | None:
    """Interactions (j'aime, commentaires, partages, enregistrements) rapportées à la couverture, en %."""
    if not ligne["couverture"]:
        return None
    interactions = sum(ligne[c] or 0 for c in ("jaime", "commentaires", "partages", "enregistrements"))
    return round(100 * interactions / ligne["couverture"], 1)
