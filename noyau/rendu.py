"""Rendu d'une page HTML/CSS en image PNG avec Chromium sans fenêtre (pour les visuels du graphiste).

Le HTML et le CSS écrits par Claude sont placés dans un gabarit qui verrouille la page :
- aucun accès réseau (politique de sécurité du contenu + résolution DNS coupée dans Chromium),
- aucun script exécuté,
- seules les polices de marque et les fichiers du dossier de travail (photos) sont accessibles.
Un seul rendu à la fois, pour ménager la mémoire du VPS.
"""

import shutil
import subprocess
import threading
from pathlib import Path

from noyau.config import RACINE
from noyau.erreurs import ErreurDefinitive
from noyau.images import ImageErreur, dimensions

DOSSIER_POLICES = RACINE / "app" / "polices-marque"
DOSSIER_MARQUE = RACINE / "app" / "marque"  # logo et éléments graphiques de la marque, utilisables dans les visuels
CANDIDATS_CHROMIUM = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
)
DELAI_MAX_SECONDES = 60

_un_rendu_a_la_fois = threading.Lock()

GABARIT = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: data:; font-src file: data:; style-src 'unsafe-inline'">
<style>
{polices}
html, body {{ margin: 0; padding: 0; width: {largeur}px; height: {hauteur}px; overflow: hidden; }}
* {{ box-sizing: border-box; -webkit-font-smoothing: antialiased; }}
</style>
<style>
{css}
</style>
</head>
<body>
{html}
</body>
</html>
"""


class RenduErreur(Exception):
    """Rendu raté (délai dépassé, image absente ou mauvaise taille) : la tâche sera retentée."""


def trouver_chromium() -> str:
    for candidat in CANDIDATS_CHROMIUM:
        if Path(candidat).is_file() or shutil.which(candidat):
            return candidat
    raise ErreurDefinitive("Chromium introuvable : installez Chromium ou Google Chrome pour produire les visuels.")


def polices_disponibles() -> dict[str, str]:
    """Familles de police installées dans app/polices-marque (nom → fichier)."""
    return {f.stem.split("-")[0]: f.name for f in sorted(DOSSIER_POLICES.glob("*.woff2"))}


def elements_marque() -> list[str]:
    """Fichiers de la marque (logo…) copiés à côté de chaque page, citables par leur nom."""
    return sorted(f.name for f in DOSSIER_MARQUE.glob("*") if f.suffix.lower() in (".svg", ".png", ".jpg"))


def _declarations_polices(dossier: Path) -> str:
    for nom in elements_marque():
        shutil.copyfile(DOSSIER_MARQUE / nom, dossier / nom)
    declarations = []
    for famille, fichier in polices_disponibles().items():
        shutil.copyfile(DOSSIER_POLICES / fichier, dossier / fichier)
        declarations.append(f'@font-face {{ font-family: "{famille}"; src: url("{fichier}") format("woff2"); '
                            "font-weight: 100 900; font-display: block; }")
    return "\n".join(declarations)


def rendre(html: str, css: str, cible: Path, largeur: int, hauteur: int, dossier_travail: Path,
           chromium: str | None = None, delai_max: float = DELAI_MAX_SECONDES) -> Path:
    """Rend la page dans `cible` (PNG) aux dimensions exactes. Les fichiers cités par le HTML
    (photos) doivent se trouver dans `dossier_travail`."""
    chromium = chromium or trouver_chromium()
    page = dossier_travail / f"{cible.stem}.html"
    page.write_text(GABARIT.format(polices=_declarations_polices(dossier_travail), largeur=largeur,
                                   hauteur=hauteur, css=css, html=html), encoding="utf-8")
    profil = dossier_travail / "profil-chromium"
    commande = [
        chromium, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--mute-audio",
        "--disable-background-networking", "--force-device-scale-factor=1",
        "--host-resolver-rules=MAP * ~NOTFOUND",  # aucune résolution DNS : pas d'accès à Internet
        f"--user-data-dir={profil}", f"--window-size={largeur},{hauteur}",
        "--virtual-time-budget=5000", f"--screenshot={cible}", page.as_uri(),
    ]
    with _un_rendu_a_la_fois:
        try:
            subprocess.run(commande, capture_output=True, timeout=delai_max, cwd=dossier_travail)
        except subprocess.TimeoutExpired:
            raise RenduErreur(f"Rendu Chromium trop long (plus de {delai_max:g} s)") from None
        finally:
            shutil.rmtree(profil, ignore_errors=True)
    try:
        taille = dimensions(cible)
    except (ImageErreur, FileNotFoundError):
        raise RenduErreur("Chromium n'a produit aucune image") from None
    if taille != (largeur, hauteur):
        raise RenduErreur(f"Image rendue en {taille[0]}×{taille[1]} au lieu de {largeur}×{hauteur}")
    return cible
