"""Recherche dans les légendes : posts de l'agence (toutes versions) et posts Instagram relevés.

Insensible aux accents et à la casse (« cafe » trouve « Café »). Peu de données : la recherche se fait en Python,
sans index. Les mots cherchés sont surlignés dans un extrait centré sur la première occurrence.
"""

import unicodedata
from html import escape

from markupsafe import Markup

LONGUEUR_MIN = 2
CONTEXTE = 90  # caractères gardés de part et d'autre de la première occurrence


def _plier(caractere: str) -> str:
    return unicodedata.normalize("NFKD", caractere.replace("’", "'")).encode("ascii", "ignore").decode().lower()


def _plie(texte: str) -> tuple[str, list[int]]:
    """Texte sans accents ni majuscules, et pour chacun de ses caractères la position dans le texte d'origine."""
    plie, positions = [], []
    for i, caractere in enumerate(texte):
        for c in _plier(caractere):
            plie.append(c)
            positions.append(i)
    return "".join(plie), positions


def termes(requete: str) -> list[str]:
    return [t for t in _plier(requete).split() if len(t) >= LONGUEUR_MIN]


def correspond(texte: str | None, mots: list[str]) -> bool:
    """Tous les mots sont présents (dans n'importe quel ordre)."""
    plie = _plie(texte or "")[0]
    return bool(mots) and all(m in plie for m in mots)


def extrait(texte: str, mots: list[str]) -> Markup:
    """Extrait échappé autour de la première occurrence, chaque occurrence entourée de <mark>."""
    plie, positions = _plie(texte)
    zones = []
    for mot in mots:
        debut = plie.find(mot)
        while debut != -1:
            zones.append((positions[debut], positions[debut + len(mot) - 1] + 1))
            debut = plie.find(mot, debut + len(mot))
    if not zones:
        return Markup(escape(texte[:2 * CONTEXTE]))
    zones.sort()
    fusionnees = [list(zones[0])]
    for debut, fin in zones[1:]:
        if debut <= fusionnees[-1][1]:
            fusionnees[-1][1] = max(fusionnees[-1][1], fin)
        else:
            fusionnees.append([debut, fin])
    gauche = max(0, fusionnees[0][0] - CONTEXTE)
    droite = min(len(texte), fusionnees[0][1] + CONTEXTE)
    morceaux, curseur = [], gauche
    for debut, fin in fusionnees:
        if debut >= droite:
            break
        morceaux.append(escape(texte[curseur:debut]))
        morceaux.append(f"<mark>{escape(texte[debut:min(fin, droite)])}</mark>")
        curseur = min(fin, droite)
    morceaux.append(escape(texte[curseur:droite]))
    return Markup(("…" if gauche else "") + "".join(morceaux) + ("…" if droite < len(texte) else ""))
