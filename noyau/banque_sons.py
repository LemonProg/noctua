"""Banque de bruitages : de vrais enregistrements trouvés sur Openverse, CC0 uniquement (aucun crédit à donner).

Constituée une fois (`agence sons importer`), puis réutilisée par agent_video, qui choisit un son par son
identifiant au lieu de le synthétiser (les bruitages Web Audio de noyau/sons.py restent en secours).

Pour chaque catégorie (whoosh, pop, clic…) : une seule recherche Openverse (licence CC0, source Freesound,
5 s au plus), puis pour chaque son retenu :
- licence revérifiée sur la page Freesound elle-même (l'auteur a pu la changer depuis l'indexation) : un son dont
  la licence n'est pas confirmée CC0 n'entre pas ;
- MP3 converti en WAV 48 kHz stéréo, silences du début et de la fin coupés, crête ramenée à -3 dBFS (tous les
  sons au même niveau : le volume choisi par Claude est alors comparable d'un son à l'autre) ;
- attaque mesurée : l'instant où le son frappe le plus fort, pour caler cette frappe pile sur l'image.

Les fichiers passent par le module `stockage` (chemin `sons/<id openverse>.wav`). Un son écarté par l'humain
(« à ne pas utiliser ») part en corbeille et est supprimé après le délai de la corbeille ; sa ligne reste, pour
qu'un nouvel import ne le reprenne pas. Seuls `urllib` et FFmpeg sont utilisés : aucune dépendance.
"""

import array
import json
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path

from noyau.base import transaction
from noyau.horloge import Horloge, maintenant_utc
from noyau.stockage import Stockage

API = "https://api.openverse.org/v1/audio/"
AGENT = "agence-ia/0.1 (banque de bruitages CC0)"
PAGE_FREESOUND = "https://freesound.org/"
FICHIER_FREESOUND = "https://cdn.freesound.org/"
LICENCE_PAGE = re.compile(r'title="Go to the full license text"\s+href="([^"]+)"')
DELAI_RESEAU = 60  # secondes ; l'API d'Openverse est parfois lente
ESSAIS = 3
PAUSE = 1.0  # secondes entre deux requêtes, par politesse
DUREE_MIN, DUREE_MAX = 0.08, 5.0  # secondes
CRETE = 10 ** (-3 / 20)  # -3 dBFS
FREQUENCE = 48000
PAR_CATEGORIE = 5

# Catégorie → (requête Openverse, usage expliqué à Claude).
CATEGORIES = {
    "whoosh": ("whoosh", "souffle de transition entre deux scènes"),
    "swipe": ("swipe whoosh", "glissement rapide d'un élément qui entre ou sort"),
    "swish": ("swish", "petit souffle vif, mouvement court"),
    "whoosh-inverse": ("reverse whoosh", "aspiration qui monte vers un moment fort"),
    "pop": ("pop", "apparition d'un élément (mot, forme, icône)"),
    "bulle": ("bubble pop", "apparition légère et ronde"),
    "clic": ("click ui", "clic d'interface, sélection"),
    "tap": ("tap soft", "petite touche discrète, élément posé"),
    "ding": ("ding", "mise en valeur positive, réussite"),
    "notification": ("notification chime", "carillon court, message ou rappel"),
    "impact": ("soft impact thud", "impact doux, mot ou logo qui tombe en place"),
    "montee": ("riser", "montée de tension avant une révélation"),
    "scintillement": ("sparkle", "brillance, effet « magique », révélation élégante"),
    "clavier": ("keyboard typing", "frappe au clavier, texte qui s'écrit"),
    "papier": ("paper slide", "papier ou carte qui glisse"),
}


class BanqueErreur(Exception):
    pass


@dataclass(frozen=True)
class SonBanque:
    id: int
    openverse_id: str
    categorie: str
    titre: str
    mots_cles: str
    auteur: str
    page: str
    licence_url: str
    chemin: str | None
    duree: float
    attaque: float
    etat: str
    corbeille_le: str | None
    cree_le: str


Telecharger = Callable[[str], bytes]


def telecharger(url: str) -> bytes:
    """GET avec nouvelles tentatives (l'API d'Openverse expire parfois)."""
    derniere = None
    for essai in range(ESSAIS):
        try:
            requete = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(requete, timeout=DELAI_RESEAU) as reponse:
                return reponse.read()
        except OSError as erreur:  # URLError, délai dépassé, coupure
            derniere = erreur
            time.sleep(2 * (essai + 1))
    raise BanqueErreur(f"Réseau : {url} injoignable ({derniere})")


def rechercher(requete: str, obtenir: Telecharger = telecharger, nombre: int = 20) -> list[dict]:
    parametres = urllib.parse.urlencode({"q": requete, "license": "cc0", "source": "freesound",
                                         "length": "shortest", "page_size": nombre, "mature": "false"})
    try:
        return json.loads(obtenir(f"{API}?{parametres}")).get("results", [])
    except ValueError:
        raise BanqueErreur(f"Réponse illisible d'Openverse pour « {requete} »") from None


def licence_confirmee(page: str, obtenir: Telecharger = telecharger) -> str | None:
    """Adresse de la licence CC0 lue sur la page Freesound du son, ou None si elle n'est pas CC0."""
    if not page.startswith(PAGE_FREESOUND):
        return None
    trouve = LICENCE_PAGE.search(obtenir(page).decode("utf-8", "replace"))
    if trouve and "creativecommons.org/publicdomain/zero/" in trouve.group(1):
        return trouve.group(1)
    return None


def _lire_wav(fichier: Path) -> tuple[array.array, int]:
    with wave.open(str(fichier), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise BanqueErreur("WAV inattendu (16 bits attendus)")
        echantillons = array.array("h", wav.readframes(wav.getnframes()))
        return echantillons, wav.getnchannels()


def preparer(source: Path, cible: Path) -> tuple[float, float]:
    """Convertit, coupe les silences, normalise la crête. Renvoie (durée, attaque) en secondes."""
    coupe = "silenceremove=start_periods=1:start_threshold=-50dB"
    brut = cible.with_name(cible.stem + "-brut.wav")
    try:
        resultat = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-af", f"{coupe},areverse,{coupe},areverse",
             "-ar", str(FREQUENCE), "-ac", "2", "-c:a", "pcm_s16le", "-t", f"{DUREE_MAX:g}", str(brut)],
            capture_output=True, text=True, timeout=60)
        if resultat.returncode != 0:
            raise BanqueErreur(f"Conversion impossible : {resultat.stderr[-300:]}")
        echantillons, canaux = _lire_wav(brut)
    finally:
        brut.unlink(missing_ok=True)
    trames = len(echantillons) // canaux
    duree = trames / FREQUENCE
    crete = max((abs(e) for e in echantillons), default=0)
    if duree < DUREE_MIN or crete < 300:
        raise BanqueErreur("son vide ou trop court après la coupe des silences")
    # Attaque : centre de la fenêtre de 10 ms la plus forte.
    fenetre = FREQUENCE // 100 * canaux
    forces = [max(abs(e) for e in echantillons[i:i + fenetre]) for i in range(0, len(echantillons), fenetre)]
    attaque = min((forces.index(max(forces)) + 0.5) * 0.01, duree)
    gain = CRETE * 32767 / crete
    normalises = array.array("h", (max(-32768, min(32767, round(e * gain))) for e in echantillons))
    with wave.open(str(cible), "wb") as wav:
        wav.setnchannels(canaux)
        wav.setsampwidth(2)
        wav.setframerate(FREQUENCE)
        wav.writeframes(normalises.tobytes())
    return round(duree, 3), round(attaque, 3)


class Banque:
    def __init__(self, conn: sqlite3.Connection, stockage: Stockage, dossier_temp: Path,
                 horloge: Horloge = maintenant_utc, obtenir: Telecharger = telecharger, pause: float = PAUSE):
        self.conn = conn
        self.stockage = stockage
        self.dossier_temp = dossier_temp
        self.horloge = horloge
        self.obtenir = obtenir
        self.pause = pause

    def _date(self) -> str:
        return self.horloge().astimezone(UTC).isoformat()

    @staticmethod
    def _son(ligne: sqlite3.Row) -> SonBanque:
        return SonBanque(**{cle: ligne[cle] for cle in ligne.keys()})

    def lister(self, etat: str | None = "actif") -> list[SonBanque]:
        requete = "SELECT * FROM sons" + (" WHERE etat = ?" if etat else " WHERE etat != 'supprimé'")
        return [self._son(l) for l in self.conn.execute(requete + " ORDER BY categorie, id", (etat,) if etat else ())]

    def lire(self, son_id: int) -> SonBanque:
        ligne = self.conn.execute("SELECT * FROM sons WHERE id = ?", (son_id,)).fetchone()
        if not ligne:
            raise BanqueErreur(f"Son {son_id} introuvable")
        return self._son(ligne)

    def chemin_local(self, son_id: int) -> Path:
        son = self.lire(son_id)
        if son.etat != "actif" or not son.chemin:
            raise BanqueErreur(f"Son {son_id} indisponible ({son.etat})")
        return self.stockage.chemin_local(son.chemin)

    def catalogue(self) -> str:
        """Liste des sons utilisables, telle que Claude la lit."""
        lignes = []
        for son in self.lister():
            usage = CATEGORIES.get(son.categorie, ("", ""))[1]
            lignes.append(f"- {son.id} · {son.categorie} ({usage}) · {son.duree:g} s, frappe à {son.attaque:g} s · "
                          f"« {son.titre} »" + (f" ({son.mots_cles})" if son.mots_cles else ""))
        return "\n".join(lignes)

    # --- Import ---

    def importer(self, par_categorie: int = PAR_CATEGORIE, seuil_octets: int = 0,
                 journal: Callable[[str], None] = print) -> int:
        """Complète chaque catégorie jusqu'à `par_categorie` sons actifs. Renvoie le nombre de sons ajoutés."""
        ajoutes = 0
        connus = {l[0] for l in self.conn.execute("SELECT openverse_id FROM sons")}
        self.dossier_temp.mkdir(parents=True, exist_ok=True)
        for categorie, (requete, _) in CATEGORIES.items():
            manquants = par_categorie - self.conn.execute(
                "SELECT COUNT(*) FROM sons WHERE categorie = ? AND etat = 'actif'", (categorie,)).fetchone()[0]
            if manquants <= 0:
                continue
            if self.stockage.espace_libre() < seuil_octets:
                raise BanqueErreur("Disque presque plein : import arrêté")
            try:
                resultats = rechercher(requete, self.obtenir)
            except BanqueErreur as erreur:
                journal(f"{categorie} : {erreur}")
                continue
            time.sleep(self.pause)
            for resultat in resultats:
                if manquants <= 0:
                    break
                if resultat.get("id") in connus or not self._candidat(resultat):
                    continue
                connus.add(resultat["id"])
                try:
                    self._ajouter(categorie, resultat)
                except BanqueErreur as erreur:
                    journal(f"{categorie} : « {resultat.get('title')} » écarté ({erreur})")
                    continue
                finally:
                    time.sleep(self.pause)
                manquants -= 1
                ajoutes += 1
                journal(f"{categorie} : « {resultat.get('title')} » ajouté")
            if manquants > 0:
                journal(f"{categorie} : {manquants} son(s) manquant(s), aucun autre candidat")
        return ajoutes

    @staticmethod
    def _candidat(resultat: dict) -> bool:
        duree = (resultat.get("duration") or 0) / 1000  # millisecondes dans Openverse
        return (resultat.get("license") == "cc0" and resultat.get("source") == "freesound"
                and not resultat.get("mature") and DUREE_MIN <= duree <= DUREE_MAX
                and str(resultat.get("url", "")).startswith(FICHIER_FREESOUND))

    def _ajouter(self, categorie: str, resultat: dict) -> None:
        licence = licence_confirmee(str(resultat.get("foreign_landing_url", "")), self.obtenir)
        if not licence:
            raise BanqueErreur("licence CC0 non confirmée sur la page Freesound")
        mp3 = self.dossier_temp / f"son-{resultat['id']}.mp3"
        wav = self.dossier_temp / f"son-{resultat['id']}.wav"
        try:
            mp3.write_bytes(self.obtenir(resultat["url"]))
            duree, attaque = preparer(mp3, wav)
            chemin = f"sons/{resultat['id']}.wav"
            self.stockage.ecrire(chemin, wav)
        finally:
            mp3.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
        mots_cles = ", ".join(t["name"] for t in (resultat.get("tags") or [])[:6] if t.get("name"))
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO sons (openverse_id, categorie, titre, mots_cles, auteur, page, licence_url, chemin, "
                "duree, attaque, etat, cree_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'actif', ?)",
                (resultat["id"], categorie, str(resultat.get("title") or "sans titre")[:120], mots_cles,
                 str(resultat.get("creator") or ""), resultat["foreign_landing_url"], licence, chemin,
                 duree, attaque, self._date()))

    # --- Tri par l'humain ---

    def ecarter(self, son_id: int) -> SonBanque:
        with transaction(self.conn):
            self.conn.execute("UPDATE sons SET etat = 'corbeille', corbeille_le = ? WHERE id = ? AND etat = 'actif'",
                              (self._date(), son_id))
        return self.lire(son_id)

    def restaurer(self, son_id: int) -> SonBanque:
        with transaction(self.conn):
            self.conn.execute("UPDATE sons SET etat = 'actif', corbeille_le = NULL WHERE id = ? AND etat = 'corbeille'",
                              (son_id,))
        return self.lire(son_id)

    def purger(self, delai: timedelta) -> list[int]:
        """Supprime les fichiers des sons en corbeille depuis plus de `delai`."""
        limite = (self.horloge().astimezone(UTC) - delai).isoformat()
        supprimes = []
        for ligne in self.conn.execute("SELECT id, chemin FROM sons WHERE etat = 'corbeille' AND corbeille_le <= ?",
                                       (limite,)).fetchall():
            if ligne["chemin"]:
                self.stockage.supprimer(ligne["chemin"])
            with transaction(self.conn):
                self.conn.execute("UPDATE sons SET etat = 'supprimé', chemin = NULL WHERE id = ?", (ligne["id"],))
            supprimes.append(ligne["id"])
        return supprimes
