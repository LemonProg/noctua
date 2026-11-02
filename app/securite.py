"""Protections de l'interface exposée sur Internet : essais de mot de passe limités, en-têtes de sécurité, liens sûrs.

Aucune dépendance : un compteur en mémoire suffit (un seul processus web, redémarrer remet les compteurs à zéro).
"""

from collections import deque
from urllib.parse import urlsplit

ESSAIS_PAR_ADRESSE = 5          # échecs tolérés par adresse IP sur la fenêtre
ESSAIS_EN_TOUT = 30             # échecs tolérés toutes adresses confondues (attaque répartie)
FENETRE_SECONDES = 15 * 60
BLOCAGE_SECONDES = 15 * 60

# Politique de contenu : tout vient du site, sauf les aperçus de posts servis par le CDN de Meta (accueil).
# Les styles en ligne restent permis (variables CSS posées dans les gabarits, styles injectés par HTMX).
POLITIQUE_CONTENU = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: https://*.cdninstagram.com https://*.fbcdn.net",
    "media-src 'self'",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
ENTETES = {
    "Content-Security-Policy": POLITIQUE_CONTENU,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class LimiteConnexions:
    """Bloque une adresse après ESSAIS_PAR_ADRESSE échecs (et tout le monde après ESSAIS_EN_TOUT) pendant 15 minutes.
    Une connexion déjà ouverte (cookie valide) n'est jamais concernée."""

    def __init__(self):
        self._echecs: dict[str, deque[float]] = {}
        self._tous: deque[float] = deque()
        self._bloques: dict[str, float] = {}

    @staticmethod
    def _purger(file: deque[float], maintenant: float) -> None:
        while file and maintenant - file[0] > FENETRE_SECONDES:
            file.popleft()

    def attente(self, adresse: str, maintenant: float) -> int:
        """Secondes avant de pouvoir réessayer (0 : autorisé)."""
        fin = max(self._bloques.get(adresse, 0), self._bloques.get("*", 0))
        return max(0, int(fin - maintenant) + 1) if fin > maintenant else 0

    def echec(self, adresse: str, maintenant: float) -> bool:
        """Note un échec ; renvoie True si l'adresse (ou tout le site) vient d'être bloquée."""
        file = self._echecs.setdefault(adresse, deque())
        for f in (file, self._tous):
            self._purger(f, maintenant)
            f.append(maintenant)
        bloque = False
        if len(file) >= ESSAIS_PAR_ADRESSE:
            self._bloques[adresse] = maintenant + BLOCAGE_SECONDES
            file.clear()
            bloque = True
        if len(self._tous) >= ESSAIS_EN_TOUT:
            self._bloques["*"] = maintenant + BLOCAGE_SECONDES
            self._tous.clear()
            bloque = True
        return bloque

    def reussite(self, adresse: str) -> None:
        self._echecs.pop(adresse, None)


def lien_sur(url: str | None) -> str | None:
    """Adresse web cliquable seulement si elle est en http(s) : un rapport de veille ou une réponse d'API ne doit pas
    pouvoir glisser un lien « javascript: » dans l'interface."""
    if not url or not isinstance(url, str):
        return None
    return url if urlsplit(url.strip()).scheme in ("http", "https") else None
