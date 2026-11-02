"""Traitement des images avec Pillow : métadonnées, formats des réseaux, vignettes.

- Les métadonnées (EXIF, dont la position GPS des téléphones) sont retirées des rushs à l'import :
  elles ne doivent partir ni chez un modèle, ni dans une publication.
- Les formats par réseau recadrent au centre puis redimensionnent.
"""

from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

# Format conseillé d'une image de publication, par réseau (largeur, hauteur).
FORMATS_RESEAUX = {
    "instagram": (1080, 1350),  # portrait 4:5
    "facebook": (1080, 1350),
    "linkedin": (1200, 1200),
    "tiktok": (1080, 1920),
    "x": (1600, 900),
}
FORMAT_PAR_DEFAUT = (1080, 1080)
LARGEUR_VIGNETTE = 800
# Plus grand côté des copies envoyées aux modèles : au-delà, coût et lenteur augmentent sans gain visible.
COTE_MAX_MODELES = 1600
FORMATS_ENREGISTRES = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


class ImageErreur(Exception):
    pass


def ouvrir(chemin: Path) -> Image.Image:
    """Ouvre l'image et applique son orientation EXIF (photos de téléphone prises en portrait)."""
    try:
        image = Image.open(chemin)
        image.load()
    except (UnidentifiedImageError, OSError):
        raise ImageErreur(
            f"Image illisible : {chemin.name}. Les formats acceptés sont JPEG, PNG et WebP "
            "(une photo HEIC d'iPhone doit être exportée en JPEG)."
        ) from None
    return ImageOps.exif_transpose(image)


def _enregistrer(image: Image.Image, cible: Path) -> None:
    """Enregistre une image reconstruite à partir de ses seuls pixels : aucune métadonnée ne survit
    (ni EXIF, ni GPS, ni profil d'appareil, ni texte PNG)."""
    propre = Image.frombytes(image.mode, image.size, image.tobytes())
    if image.mode == "P":
        propre.putpalette(image.getpalette())
    image = propre
    format_pil = FORMATS_ENREGISTRES.get(cible.suffix.lower(), "JPEG")
    if format_pil == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    options = {"quality": 90, "optimize": True} if format_pil in ("JPEG", "WEBP") else {"optimize": True}
    image.save(cible, format_pil, **options)


def nettoyer_metadonnees(source: Path, cible: Path) -> None:
    """Copie l'image sans ses métadonnées, avec la bonne orientation."""
    _enregistrer(ouvrir(source), cible)


def adapter_au_reseau(source: Path, cible: Path, reseau: str) -> tuple[int, int]:
    """Recadre au centre et redimensionne au format conseillé du réseau. Renvoie la taille obtenue."""
    taille = FORMATS_RESEAUX.get(reseau.lower(), FORMAT_PAR_DEFAUT)
    image = ImageOps.fit(ouvrir(source), taille, method=Image.Resampling.LANCZOS)
    _enregistrer(image, cible)
    return image.size


def vignette(source: Path, cible: Path, largeur: int = LARGEUR_VIGNETTE) -> None:
    """Version légère pour l'affichage sur téléphone (jamais agrandie)."""
    image = ouvrir(source)
    if image.width > largeur:
        image = image.resize((largeur, round(image.height * largeur / image.width)), Image.Resampling.LANCZOS)
    _enregistrer(image, cible)


def reduire(source: Path, cible: Path, cote_max: int = COTE_MAX_MODELES) -> None:
    """Copie réduite (jamais agrandie) pour l'envoi à Claude ou au rendu."""
    image = ouvrir(source)
    image.thumbnail((cote_max, cote_max), Image.Resampling.LANCZOS)
    _enregistrer(image, cible)


def en_jpeg(source: Path, cible) -> None:
    """Copie JPEG sans métadonnées (seul format d'image accepté par la publication Instagram).
    `cible` peut être un chemin ou un fichier ouvert."""
    image = ouvrir(source)
    if image.mode not in ("RGB", "L"):
        # La transparence éventuelle est posée sur du blanc plutôt que sur du noir.
        fond = Image.new("RGB", image.size, "white")
        fond.paste(image, mask=image.convert("RGBA").getchannel("A"))
        image = fond
    image.save(cible, "JPEG", quality=92, optimize=True)


def dimensions(chemin: Path) -> tuple[int, int]:
    return ouvrir(chemin).size
