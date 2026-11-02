"""Mot de passe de l'interface (scrypt) et cookie de session signé (HMAC), sans dépendance.

Le hachage utilise « : » comme séparateur, pour ne jamais être interprété par python-dotenv.
"""

import base64
import hashlib
import hmac
import secrets
import time

N, R, P = 2**14, 8, 1
DUREE_SESSION = 30 * 24 * 3600  # 30 jours
LONGUEUR_MINIMALE = 10


def _b64(octets: bytes) -> str:
    return base64.urlsafe_b64encode(octets).decode().rstrip("=")


def _deb64(texte: str) -> bytes:
    return base64.urlsafe_b64decode(texte + "=" * (-len(texte) % 4))


def hacher(mot_de_passe: str) -> str:
    sel = secrets.token_bytes(16)
    empreinte = hashlib.scrypt(mot_de_passe.encode(), salt=sel, n=N, r=R, p=P)
    return f"scrypt:{N}:{R}:{P}:{_b64(sel)}:{_b64(empreinte)}"


def verifier(mot_de_passe: str, hachage: str) -> bool:
    try:
        nom, n, r, p, sel, empreinte = hachage.split(":")
        if nom != "scrypt":
            return False
        calcule = hashlib.scrypt(mot_de_passe.encode(), salt=_deb64(sel), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calcule, _deb64(empreinte))


def _signature(secret: str, donnee: str) -> str:
    return _b64(hmac.new(secret.encode(), donnee.encode(), hashlib.sha256).digest())


def creer_session(secret: str, maintenant: float | None = None) -> str:
    expiration = str(int((maintenant or time.time()) + DUREE_SESSION))
    return f"{expiration}.{_signature(secret, expiration)}"


def session_valide(jeton: str | None, secret: str, maintenant: float | None = None) -> bool:
    if not jeton or not secret or "." not in jeton:
        return False
    expiration, signature = jeton.split(".", 1)
    if not hmac.compare_digest(signature, _signature(secret, expiration)):
        return False
    return expiration.isdigit() and int(expiration) > (maintenant or time.time())


def nouveau_secret() -> str:
    return secrets.token_urlsafe(32)
