"""Lecture de charte.md et lecons.md, obligatoire pour chaque agent avant d'agir."""

from dataclasses import dataclass
from pathlib import Path

from noyau.config import RACINE

MARQUEUR_GABARIT = "[À REMPLIR]"

ENTETE_LECONS = """# Leçons

Leçons tirées des contenus rejetés, validées par l'humain. Lues par tous les agents avant chaque tâche.
Ce fichier est régénéré depuis la base : ne pas le modifier à la main.
"""


class CharteErreur(Exception):
    pass


@dataclass(frozen=True)
class Contexte:
    charte: str
    lecons: str
    direction: str = ""  # direction artistique validée (vide tant qu'aucune ne l'est)


def _sections_a_remplir(charte: str) -> list[str]:
    sections: list[str] = []
    section = "(début du fichier)"
    for ligne in charte.splitlines():
        if ligne.startswith("## "):
            section = ligne[3:].strip()
        elif MARQUEUR_GABARIT in ligne and section not in sections:
            sections.append(section)
    return sections


def lire(racine: Path = RACINE) -> Contexte:
    chemin_charte = racine / "charte.md"
    if not chemin_charte.is_file():
        raise CharteErreur(f"charte.md introuvable ({chemin_charte}). Les agents ne peuvent pas démarrer.")

    charte = chemin_charte.read_text(encoding="utf-8")
    a_remplir = _sections_a_remplir(charte)
    if a_remplir:
        raise CharteErreur(
            "charte.md n'est pas encore remplie. Sections à compléter : " + ", ".join(a_remplir) + "."
        )

    chemin_lecons = racine / "lecons.md"
    if not chemin_lecons.is_file():
        chemin_lecons.write_text(ENTETE_LECONS, encoding="utf-8")

    chemin_direction = racine / "direction_artistique.md"
    direction = chemin_direction.read_text(encoding="utf-8") if chemin_direction.is_file() else ""
    return Contexte(charte=charte, lecons=chemin_lecons.read_text(encoding="utf-8"), direction=direction)
