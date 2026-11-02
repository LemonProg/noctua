"""Graphiques du tableau de bord, dessinés en SVG côté serveur (aucune bibliothèque, aucun JavaScript).

Couleurs des formats tirées de la charte (violet, framboise, turquoise), validées pour le daltonisme
(skill dataviz : bande de luminosité, écart CVD ≥ 14). Le turquoise étant peu contrasté sur fond clair, chaque
graphique a une légende et des valeurs lisibles ; le détail complet reste dans la page Statistiques.
Règles appliquées : colonnes de 24 px au plus à bout arrondi de 4 px, ligne de 2 px, points de 8 px cerclés
du fond, grille fine et discrète, un seul axe, texte jamais dans la couleur des données, info-bulle par marque.
"""

from html import escape
from math import ceil, log10

from markupsafe import Markup

COULEURS_FORMATS = {"image": "#713CF5", "carrousel": "#E8405A", "reel": "#2ABFBF"}
# En mode sombre, style.css redéfinit --serie-<format> (turquoise plus foncé, validé sur fond sombre).


def couleur(format_: str | None) -> str:
    return f"var(--serie-{format_}, {COULEURS_FORMATS[format_]})" if format_ in COULEURS_FORMATS else "var(--texte-pale)"
NOMS_FORMATS = {"image": "Image", "carrousel": "Carrousel", "reel": "Reel"}
LARGEUR, HAUTEUR = 640, 230
MARGE_GAUCHE, MARGE_BAS, MARGE_HAUT, MARGE_DROITE = 44, 30, 14, 10


def nombre(valeur: float, decimales: int = 0) -> str:
    texte = f"{valeur:,.{decimales}f}".replace(",", " ").replace(".", ",")
    return texte


def graduations(maximum: float, nombre_traits: int = 4) -> list[float]:
    """Graduations « rondes » de 0 au-dessus du maximum."""
    if maximum <= 0:
        return [0, 1]
    brut = maximum / nombre_traits
    puissance = 10 ** int(log10(brut))
    pas = next(m * puissance for m in (1, 2, 2.5, 5, 10) if m * puissance >= brut)
    return [pas * i for i in range(int(ceil(maximum / pas)) + 1)]


def _barre_verticale(x: float, y: float, largeur: float, hauteur: float, rayon: float = 4) -> str:
    """Colonne à bout arrondi (4 px), carrée sur la ligne de base."""
    r = min(rayon, largeur / 2, hauteur)
    return (f"M{x:.1f},{y + hauteur:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + largeur - r:.1f} Q{x + largeur:.1f},{y:.1f} {x + largeur:.1f},{y + r:.1f} V{y + hauteur:.1f} Z")


def _cadre(titre_accessible: str, contenu: list[str], hauteur: int = HAUTEUR) -> Markup:
    return Markup(f'<svg class="graphique" viewBox="0 0 {LARGEUR} {hauteur}" role="img" '
                  f'aria-label="{escape(titre_accessible)}" preserveAspectRatio="xMidYMid meet">'
                  + "".join(contenu) + "</svg>")


def _axe_y(ticks: list[float], echelle, suffixe: str = "") -> list[str]:
    parties = []
    for valeur in ticks:
        y = echelle(valeur)
        parties.append(f'<line x1="{MARGE_GAUCHE}" x2="{LARGEUR - MARGE_DROITE}" y1="{y:.1f}" y2="{y:.1f}" class="g-grille"/>')
        etiquette = nombre(valeur, 0 if valeur >= 10 or valeur == int(valeur) else 1)
        parties.append(f'<text x="{MARGE_GAUCHE - 8}" y="{y + 4:.1f}" text-anchor="end" class="g-axe">'
                       f"{etiquette}{suffixe}</text>")
    return parties


def colonnes_par_post(posts: list[dict], valeur: str, libelle: str) -> Markup:
    """Une colonne par post (dans l'ordre chronologique), colorée par format."""
    if not posts:
        return Markup("")
    ticks = graduations(max(p[valeur] or 0 for p in posts))
    bas, haut = HAUTEUR - MARGE_BAS, MARGE_HAUT
    echelle = lambda v: bas - (v / ticks[-1]) * (bas - haut)  # noqa: E731
    bande = (LARGEUR - MARGE_GAUCHE - MARGE_DROITE) / len(posts)
    largeur = min(24, bande * 0.62)
    parties = _axe_y(ticks, echelle)
    for i, p in enumerate(posts):
        v = p[valeur] or 0
        x = MARGE_GAUCHE + i * bande + (bande - largeur) / 2
        y = echelle(v)
        teinte = couleur(p["type"])
        info = f'{p["date"]} · {NOMS_FORMATS.get(p["type"], p["type"] or "?")} · {nombre(v)} {libelle}'
        parties.append(f'<g class="g-marque"><title>{escape(info)}</title>'
                       f'<rect x="{x - 4:.1f}" y="{haut}" width="{largeur + 8:.1f}" height="{bas - haut}" fill="transparent"/>'
                       f'<path d="{_barre_verticale(x, y, largeur, bas - y)}" style="fill:{teinte}"/></g>')
        if len(posts) <= 14 or i % 2 == 0:
            parties.append(f'<text x="{x + largeur / 2:.1f}" y="{HAUTEUR - 10}" text-anchor="middle" class="g-axe">'
                           f'{p["date"]}</text>')
    maximum = max(range(len(posts)), key=lambda i: posts[i][valeur] or 0)
    xm = MARGE_GAUCHE + maximum * bande + bande / 2
    parties.append(f'<text x="{xm:.1f}" y="{echelle(posts[maximum][valeur] or 0) - 6:.1f}" text-anchor="middle" '
                   f'class="g-valeur">{nombre(posts[maximum][valeur] or 0)}</text>')
    parties.append(f'<line x1="{MARGE_GAUCHE}" x2="{LARGEUR - MARGE_DROITE}" y1="{bas}" y2="{bas}" class="g-base"/>')
    return _cadre(f"{libelle} par post", parties)


def ligne_par_post(posts: list[dict], valeur: str, moyenne: float | None) -> Markup:
    """Courbe d'une valeur en % par post, avec la moyenne du compte en repère."""
    points = [(i, p) for i, p in enumerate(posts) if p[valeur] is not None]
    if len(points) < 2:
        return Markup("")
    ticks = graduations(max(p[valeur] for _, p in points) * 1.1)
    bas, haut = HAUTEUR - MARGE_BAS, MARGE_HAUT
    echelle = lambda v: bas - (v / ticks[-1]) * (bas - haut)  # noqa: E731
    bande = (LARGEUR - MARGE_GAUCHE - MARGE_DROITE) / len(posts)
    centre = lambda i: MARGE_GAUCHE + i * bande + bande / 2  # noqa: E731
    parties = _axe_y(ticks, echelle, " %")
    if moyenne is not None:
        y = echelle(moyenne)
        parties.append(f'<line x1="{MARGE_GAUCHE}" x2="{LARGEUR - MARGE_DROITE}" y1="{y:.1f}" y2="{y:.1f}" class="g-moyenne"/>')
        parties.append(f'<text x="{LARGEUR - MARGE_DROITE}" y="{y - 6:.1f}" text-anchor="end" class="g-axe">'
                       f'moyenne {nombre(moyenne, 1)} %</text>')
    trace = " ".join(f"{'M' if n == 0 else 'L'}{centre(i):.1f},{echelle(p[valeur]):.1f}" for n, (i, p) in enumerate(points))
    parties.append(f'<path d="{trace}" class="g-courbe"/>')
    for i, p in points:
        info = f'{p["date"]} · {NOMS_FORMATS.get(p["type"], p["type"] or "?")} · {nombre(p[valeur], 1)} %'
        parties.append(f'<g class="g-marque"><title>{escape(info)}</title>'
                       f'<circle cx="{centre(i):.1f}" cy="{echelle(p[valeur]):.1f}" r="12" fill="transparent"/>'
                       f'<circle cx="{centre(i):.1f}" cy="{echelle(p[valeur]):.1f}" r="4.5" class="g-point"/></g>')
        if len(posts) <= 14 or i % 2 == 0:
            parties.append(f'<text x="{centre(i):.1f}" y="{HAUTEUR - 10}" text-anchor="middle" class="g-axe">'
                           f'{p["date"]}</text>')
    # Valeur du dernier post, du côté opposé au segment qui y arrive pour ne pas le chevaucher.
    dernier_i, dernier = points[-1]
    dessous = points[-2][1][valeur] > dernier[valeur]
    parties.append(f'<text x="{centre(dernier_i):.1f}" y="{echelle(dernier[valeur]) + (20 if dessous else -12):.1f}" '
                   f'text-anchor="middle" class="g-valeur">{nombre(dernier[valeur], 1)} %</text>')
    return _cadre("Taux d'engagement par post", parties)


def barres_par_format(formats: list[dict]) -> Markup:
    """Barres horizontales en HTML (lisibles dans une carte étroite) : moyenne par format, valeur au bout."""
    if not formats:
        return Markup("")
    maximum = max(f["valeur"] for f in formats) or 1
    lignes = []
    for f in formats:
        nom = NOMS_FORMATS.get(f["format"], f["format"])
        info = f'{nom} · {nombre(f["valeur"])} en moyenne sur {f["posts"]} post{"s" if f["posts"] > 1 else ""}'
        lignes.append(f'<div class="barre-format" title="{escape(info)}">'
                      f'<span class="barre-nom">{escape(nom)}<small>{f["posts"]} post{"s" if f["posts"] > 1 else ""}</small></span>'
                      f'<span class="barre-piste"><i style="width:{max(f["valeur"] / maximum * 100, 2):.1f}%;'
                      f'background:{couleur(f["format"])}"></i></span>'
                      f'<span class="barre-valeur">{nombre(f["valeur"])}</span></div>')
    return Markup('<div class="barres-formats" role="img" aria-label="Couverture moyenne par format">'
                  + "".join(lignes) + "</div>")


def courbe_abonnes(points: list[tuple]) -> Markup:
    """Petite courbe sans axe (dans la tuile des abonnés) : un point par jour, le dernier marqué."""
    if len(points) < 2:
        return Markup("")
    largeur, hauteur, marge = 200, 44, 4
    valeurs = [n for _, n in points]
    bas, haut = min(valeurs), max(valeurs)
    etendue = (haut - bas) or 1
    x = lambda i: marge + i * (largeur - 2 * marge) / (len(points) - 1)  # noqa: E731
    y = lambda v: hauteur - marge - (v - bas) / etendue * (hauteur - 2 * marge)  # noqa: E731
    trace = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(valeurs))
    titre = f"{nombre(valeurs[0])} → {nombre(valeurs[-1])} abonnés en {len(points)} jours"
    return Markup(f'<svg class="courbe-abonnes" viewBox="0 0 {largeur} {hauteur}" preserveAspectRatio="none" role="img" '
                  f'aria-label="{escape(titre)}"><title>{escape(titre)}</title>'
                  f'<path d="{trace}" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" '
                  f'stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>')


def legende_formats(formats: set[str]) -> Markup:
    elements = [f'<span class="legende-element"><i style="background:{couleur(f)}"></i>{NOMS_FORMATS[f]}</span>'
                for f in COULEURS_FORMATS if f in formats]
    return Markup('<div class="legende">' + "".join(elements) + "</div>") if len(elements) > 1 else Markup("")
