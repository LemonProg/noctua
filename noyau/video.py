"""Rendu des vidéos (motion design) avec HyperFrames : composition HTML/GSAP → MP4.

La composition écrite par Claude (CSS, scènes HTML, animation GSAP) est placée dans un gabarit verrouillé :
- 1080×1920, 30 images par seconde, durée fixée ; pas de voix ni de musique, seulement des bruitages (vrais
  enregistrements de la banque, noyau/banque_sons.py, ou synthétisés en Web Audio, noyau/sons.py) ;
- après le rendu, niveau sonore réglé par un gain linéaire (sonie visée, crête maximale), image non réencodée ;
- GSAP, polices de marque et logo copiés à côté de la page : aucune ressource externe. Le compilateur
  d'HyperFrames télécharge lui-même les adresses distantes (la politique de sécurité du navigateur ne suffit
  pas) : une composition qui en cite une est refusée avant tout rendu ;
- l'animation est une timeline GSAP en pause, qu'HyperFrames fait avancer image par image.

Réglages pour un VPS de 2 cœurs et 4 Go : un seul worker (un Chrome), profil mémoire réduite, pas de cache
d'images hors du dossier de travail. HyperFrames et GSAP sont installés en versions figées dans `video/`,
sans télémétrie ni mise à jour automatique. Un seul rendu à la fois (voie `rendu` de la file).
"""

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from noyau.config import RACINE
from noyau.erreurs import ErreurDefinitive
from noyau.rendu import _declarations_polices
from noyau.stockage import image_de_video

DOSSIER_VIDEO = RACINE / "video"
HYPERFRAMES = DOSSIER_VIDEO / "node_modules" / ".bin" / "hyperframes"
GSAP = DOSSIER_VIDEO / "node_modules" / "gsap" / "dist" / "gsap.min.js"
LARGEUR, HAUTEUR, FPS = 1080, 1920, 30
ID_COMPOSITION = "principale"
DELAI_LINT_SECONDES = 120
DELAI_RENDU_SECONDES = 30 * 60  # à ajuster après le banc de test du VPS
ENVIRONNEMENT = {
    "HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1",
    "HYPERFRAMES_NO_UPDATE_CHECK": "1", "HYPERFRAMES_NO_OPTIONAL_INSTALL": "1",
    "PRODUCER_LOW_MEMORY_MODE": "true",
}
# Adresses externes : http(s)://, adresse relative au protocole (//hôte) dans src, href ou url(), @import.
EXTERNE = re.compile(r"""(?i)https?://[^\s"')]+|(?:src|href)\s*=\s*["']//[^"']+|url\(\s*["']?//[^)]+|@import""")

_un_rendu_a_la_fois = threading.Lock()

GABARIT = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'self' 'unsafe-inline' data: blob:">
<script src="gsap.min.js"></script>
<style>
{polices}
html, body {{ margin: 0; padding: 0; background: #000; }}
* {{ box-sizing: border-box; -webkit-font-smoothing: antialiased; }}
#{racine} {{ position: relative; width: {largeur}px; height: {hauteur}px; overflow: hidden; }}
.clip {{ position: absolute; inset: 0; }}
</style>
<style>
{css}
</style>
</head>
<body>
<div id="{racine}" data-composition-id="{racine}" data-start="0" data-duration="{duree}" data-width="{largeur}" data-height="{hauteur}" data-fps="{fps}">
{html}
{audio}
</div>
<script>
window.__timelines = window.__timelines || {{}};
const tl = gsap.timeline({{ paused: true }});
{animation}
window.__timelines["{racine}"] = tl;
</script>
</body>
</html>
"""


class VideoErreur(Exception):
    """Composition refusée (adresse externe, erreur de lint) : à corriger par Claude."""


def verifier_hyperframes() -> None:
    if not HYPERFRAMES.exists() or not GSAP.exists():
        raise ErreurDefinitive("HyperFrames n'est pas installé : lancer `npm ci` dans le dossier video/")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ErreurDefinitive("FFmpeg introuvable : l'installer pour produire les vidéos")


def references_externes(*textes: str) -> list[str]:
    return sorted({m.group(0) for texte in textes for m in EXTERNE.finditer(texte)})


@dataclass(frozen=True)
class Son:
    """Bruitage placé dans la vidéo (fichier WAV déjà dans le dossier du projet)."""
    fichier: str
    debut: float
    volume: float


def assembler(dossier: Path, *, css: str, html: str, animation: str, duree: float, sons: list[Son] = ()) -> Path:
    """Écrit le projet HyperFrames (index.html + fichiers) dans `dossier`. Renvoie la page."""
    externes = references_externes(css, html, animation)
    if externes:
        raise VideoErreur(f"Ressources externes interdites dans la composition : {', '.join(externes)}")
    verifier_hyperframes()
    dossier.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GSAP, dossier / "gsap.min.js")
    page = dossier / "index.html"
    page.write_text(GABARIT.format(polices=_declarations_polices(dossier), racine=ID_COMPOSITION, largeur=LARGEUR,
                                   hauteur=HAUTEUR, fps=FPS, duree=f"{duree:g}", css=css, html=html,
                                   audio="\n".join(f'<audio id="son-{i}" src="{s.fichier}" data-start="{s.debut:g}" '
                                                   f'data-volume="{s.volume:g}"></audio>'
                                                   for i, s in enumerate(sons, start=1)),
                                   animation=animation), encoding="utf-8")
    return page


def _lancer(arguments: list[str], dossier: Path, delai: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([str(HYPERFRAMES), *arguments], cwd=dossier, capture_output=True, text=True,
                              timeout=delai, env={**os.environ, **ENVIRONNEMENT})
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"HyperFrames trop long (plus de {delai:g} s) : {arguments[0]}") from None


def lint(dossier: Path) -> list[str]:
    """Erreurs bloquantes de la composition (les avertissements, liés à l'éditeur visuel, sont ignorés)."""
    resultat = _lancer(["lint", "--json"], dossier, DELAI_LINT_SECONDES)
    debut = resultat.stdout.find("{")
    try:
        rapport = json.loads(resultat.stdout[debut:]) if debut >= 0 else {}
    except ValueError:
        raise RuntimeError(f"Réponse illisible de hyperframes lint : {resultat.stdout[-300:]}") from None
    return [f"{f.get('code')} : {f.get('message')}" + (f" ({f['fixHint']})" if f.get("fixHint") else "")
            for f in rapport.get("findings", []) if f.get("severity") == "error"]


# Réglages de rendu pour 2 cœurs et 4 Go : un seul Chrome, profil mémoire réduite, pas de cache d'images.
REGLAGES_RENDU = ["--workers", "1", "--low-memory-mode", "--frames-cache-dir", "off", "--quiet"]
# Niveau sonore visé (réglable dans .env) : bruitages seuls, un peu sous les -14 LUFS des réseaux.
CIBLE_LUFS, CRETE_DBTP = -16.0, -1.5
ARGUMENTS_MP4 = ["--format", "mp4", "--quality", "standard", "--fps", str(FPS)]


def _rendre(dossier: Path, arguments: list[str]) -> None:
    with _un_rendu_a_la_fois:
        resultat = _lancer(["render", *arguments, *REGLAGES_RENDU], dossier, DELAI_RENDU_SECONDES)
    if resultat.returncode != 0:
        raise RuntimeError(f"Rendu HyperFrames en échec : {(resultat.stderr or resultat.stdout)[-500:]}")


def images_cles(dossier: Path, sortie: Path, par_seconde: float = 1) -> list[Path]:
    """Images fixes de la vidéo, en qualité brouillon (relecture par Claude avant le rendu complet)."""
    _rendre(dossier, ["--format", "png-sequence", "--fps", f"{par_seconde:g}", "--quality", "draft",
                      "--output", str(sortie)])
    images = sorted(sortie.glob("*.png"))
    if not images:
        raise RuntimeError("HyperFrames n'a produit aucune image")
    return images


def rendre_mp4(dossier: Path, sortie: Path, cible_lufs: float = CIBLE_LUFS, crete_dbtp: float = CRETE_DBTP) -> Path:
    """Rendu final : MP4 H.264 en 1080×1920, 30 i/s, qualité standard, puis niveau sonore réglé."""
    _rendre(dossier, [*ARGUMENTS_MP4, "--output", str(sortie)])
    if not sortie.exists() or sortie.stat().st_size == 0:
        raise RuntimeError("HyperFrames n'a produit aucune vidéo")
    largeur, hauteur, _ = caracteristiques(sortie)
    if (largeur, hauteur) != (LARGEUR, HAUTEUR):
        raise RuntimeError(f"Vidéo rendue en {largeur}×{hauteur} au lieu de {LARGEUR}×{HAUTEUR}")
    niveler_son(sortie, cible_lufs, crete_dbtp)
    return sortie


def mesurer_son(fichier: Path) -> tuple[float, float] | None:
    """Sonie intégrée (LUFS) et crête vraie (dBTP) de la piste audio ; None sans piste ou si elle est muette."""
    resultat = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(fichier), "-map", "0:a:0?",
                               "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                              capture_output=True, text=True, timeout=120)
    debut = resultat.stderr.rfind("{")
    if resultat.returncode != 0 or debut < 0:
        return None
    try:
        mesures = json.loads(resultat.stderr[debut:resultat.stderr.rfind("}") + 1])
        sonie, crete = float(mesures["input_i"]), float(mesures["input_tp"])
    except (ValueError, KeyError):
        return None
    return (sonie, crete) if sonie > -70 else None  # -inf : silence


def niveler_son(fichier: Path, cible_lufs: float = CIBLE_LUFS, crete_dbtp: float = CRETE_DBTP) -> float:
    """Gain linéaire (aucune compression) vers `cible_lufs`, sans dépasser `crete_dbtp`. L'image est copiée
    telle quelle (pas de réencodage), seule la piste audio est réencodée. Renvoie le gain appliqué (dB)."""
    mesures = mesurer_son(fichier)
    if not mesures:
        return 0.0
    sonie, crete = mesures
    gain = round(min(cible_lufs - sonie, crete_dbtp - crete), 2)
    if abs(gain) < 0.3:
        return 0.0
    temporaire = fichier.with_name(fichier.stem + "-niveau.mp4")
    try:
        resultat = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fichier), "-map", "0", "-c:v", "copy",
                                   "-af", f"volume={gain}dB", "-c:a", "aac", "-b:a", "192k",
                                   "-movflags", "+faststart", str(temporaire)],
                                  capture_output=True, text=True, timeout=300)
        if resultat.returncode != 0:
            raise RuntimeError(f"Réglage du niveau sonore en échec : {resultat.stderr[-300:]}")
        temporaire.replace(fichier)
    finally:
        temporaire.unlink(missing_ok=True)
    return gain


def caracteristiques(fichier: Path) -> tuple[int, int, float]:
    """Largeur, hauteur et durée (s) d'une vidéo."""
    resultat = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
                               "-of", "json", str(fichier)], capture_output=True, text=True, timeout=60)
    donnees = json.loads(resultat.stdout or "{}")
    video = next((s for s in donnees.get("streams", []) if s.get("width")), {})
    return video.get("width", 0), video.get("height", 0), float(donnees.get("format", {}).get("duration", 0))


def extraire_images(video: Path, dossier: Path, prefixe: str, nombre: int = 3) -> list[str]:
    """Quelques images réparties dans la vidéo (début, milieu, fin), pour qu'un agent la « voie »."""
    duree = caracteristiques(video)[2] or 1
    fichiers = []
    for i in range(nombre):
        nom = f"{prefixe}-{i + 1}.png"
        image_de_video(video, dossier / nom, duree * (i + 0.5) / nombre)
        fichiers.append(nom)
    return fichiers
