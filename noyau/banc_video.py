"""Banc de test du rendu vidéo : temps et mémoire maximale, pour fixer les limites sur le VPS (2 cœurs, 4 Go).

`agence video banc` rend des reels synthétiques représentatifs (scènes de 5 s, titres animés, textes rythmés,
formes en mouvement, un bruitage Web Audio par scène) avec exactement les réglages de production, et mesure la
mémoire de tous les processus du rendu (Node, Chrome, FFmpeg) en les échantillonnant. Aucun appel à Claude.
"""

import json
import os
import platform
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from noyau import sons, video

COULEURS = ("#F5F0E8", "#0A0A0A", "#713CF5", "#F5F0E8")
MOTS = ("Votre image", "enfin", "à vous", "sur la vitrine", "la carte", "et Instagram", "une même", "identité")


@dataclass(frozen=True)
class Mesure:
    duree_video: float
    secondes: float
    memoire_max_mo: float
    taille_mo: float

    @property
    def temps_reel(self) -> float:
        """Secondes de calcul par seconde de vidéo."""
        return self.secondes / self.duree_video


def composition(duree: int) -> tuple[str, str, str]:
    """CSS, scènes et animation d'un reel synthétique de `duree` secondes."""
    css = """.scene { display: flex; flex-direction: column; justify-content: center; padding: 0 90px; }
.titre { font-family: Geist; font-size: 128px; line-height: .95; letter-spacing: -2px; }
.surligne { background: #713CF5; color: #F5F0E8; padding: 0 16px; }
.forme { position: absolute; width: 360px; height: 360px; border-radius: 50%; background: #713CF5; opacity: .25; }
.sous-titre { position: absolute; left: 90px; right: 90px; bottom: 520px; font-family: Geist; font-size: 56px;
  font-weight: 600; text-align: center; color: #F5F0E8; background: rgba(10,10,10,.75); padding: 18px 24px; border-radius: 14px; }"""
    html, animation = [], []
    for i, debut in enumerate(range(0, duree, 5)):
        fin = min(debut + 5, duree)
        fond = COULEURS[i % len(COULEURS)]
        texte = "#F5F0E8" if fond in ("#0A0A0A", "#713CF5") else "#0A0A0A"
        html.append(f'<section id="scene{i}" class="clip scene" data-start="{debut}" data-duration="{fin - debut}" '
                    f'style="background:{fond};color:{texte}"><div id="forme{i}" class="forme"></div>'
                    f'<h1 id="titre{i}" class="titre">{MOTS[i % len(MOTS)]},<br><span class="surligne" id="surligne{i}">'
                    f'{MOTS[(i + 1) % len(MOTS)]}</span></h1></section>')
        animation.append(f'tl.from("#titre{i}", {{y: 140, opacity: 0, duration: .7, ease: "power3.out"}}, {debut + .2})'
                         f'.from("#surligne{i}", {{scaleX: 0, transformOrigin: "left", duration: .5}}, {debut + .7})'
                         f'.fromTo("#forme{i}", {{x: -200, y: 300, scale: .6}}, {{x: 700, y: 1300, scale: 1.4, '
                         f'duration: {fin - debut}, ease: "none"}}, {debut});')
    for n, instant in enumerate(x * 0.4 for x in range(int(duree / 0.4))):  # textes rythmés
        html.append(f'<div id="st{n}" class="clip sous-titre" data-start="{instant:.1f}" data-duration="0.4">'
                    f'{MOTS[n % len(MOTS)]}</div>')
        animation.append(f'tl.from("#st{n}", {{scale: .9, opacity: 0, duration: .15}}, {instant:.1f});')
    return css, "\n".join(html), "\n".join(animation)


def _memoire_arbre_ko(racine: int) -> int:
    """Mémoire résidente totale d'un processus et de tous ses descendants (en Ko)."""
    sortie = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    enfants, rss = {}, {}
    for ligne in sortie.splitlines():
        pid, ppid, memoire = (int(x) for x in ligne.split())
        enfants.setdefault(ppid, []).append(pid)
        rss[pid] = memoire
    total, a_voir = 0, [racine]
    while a_voir:
        pid = a_voir.pop()
        total += rss.get(pid, 0)
        a_voir += enfants.get(pid, [])
    return total


def mesurer(duree: int, dossier: Path) -> Mesure:
    projet = dossier / f"banc-{duree}s"
    projet.mkdir(parents=True, exist_ok=True)
    whoosh = sons.synthetiser(json.loads(sons.EXEMPLES)["whoosh (0,6 s)"], 0.6, projet / "whoosh.wav", dossier)
    places = [video.Son(whoosh.name, debut + 0.1, 0.4) for debut in range(0, duree, 5)]
    css, html, animation = composition(duree)
    video.assembler(projet, css=css, html=html, animation=animation, duree=duree, sons=places)
    sortie = dossier / f"banc-{duree}s.mp4"
    debut, maximum = time.monotonic(), 0
    processus = subprocess.Popen([str(video.HYPERFRAMES), "render", *video.ARGUMENTS_MP4, "--output", str(sortie),
                                  *video.REGLAGES_RENDU], cwd=projet, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, env={**os.environ, **video.ENVIRONNEMENT})
    while processus.poll() is None:
        maximum = max(maximum, _memoire_arbre_ko(processus.pid))
        time.sleep(0.5)
    if processus.returncode != 0:
        raise RuntimeError(f"Rendu du banc ({duree} s) en échec : {processus.stderr.read()[-500:]}")
    return Mesure(duree, time.monotonic() - debut, maximum / 1024, sortie.stat().st_size / 1024 ** 2)


def lancer(durees: list[int]) -> list[Mesure]:
    video.verifier_hyperframes()
    print(f"Banc de rendu sur {platform.node()} : {os.cpu_count()} cœurs, réglages de production "
          f"({' '.join(video.REGLAGES_RENDU)})")
    mesures = []
    with tempfile.TemporaryDirectory(prefix="banc-video-") as dossier:
        for duree in durees:
            mesure = mesurer(duree, Path(dossier))
            mesures.append(mesure)
            print(f"- {duree:>3} s de vidéo : {mesure.secondes:6.1f} s de rendu ({mesure.temps_reel:.1f} s par seconde), "
                  f"mémoire max {mesure.memoire_max_mo:6.0f} Mo, fichier {mesure.taille_mo:.1f} Mo")
    return mesures
