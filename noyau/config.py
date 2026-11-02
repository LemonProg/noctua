"""Chargement de la configuration depuis .env et l'environnement.

Toutes les erreurs sont regroupées dans un seul message, pour tout corriger en une fois.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path

from dotenv import dotenv_values

RACINE = Path(__file__).resolve().parent.parent


class ConfigErreur(Exception):
    pass


@dataclass(frozen=True)
class Config:
    dossier_contenus: Path
    dossier_temp: Path
    chemin_base: Path
    disque_seuil_alerte_go: float
    disque_seuil_refus_go: float
    corbeille_delai_heures: float
    publication_delai_jours: float
    plafond_couts_mensuel_usd: float
    quota_seuil_pourcent: float
    claude_code_oauth_token: str
    dossier_config_claude: Path
    hote_web: str
    port_web: int
    mot_de_passe_interface: str
    secret_session: str
    # Notifications push par ntfy (noyau/notifications.py) : désactivées sans sujet.
    ntfy_serveur: str = "https://ntfy.sh"
    ntfy_sujet: str = ""
    notifications_silence: str = "22:00-08:00"
    # Sauvegarde quotidienne de la base (noyau/sauvegarde.py).
    dossier_sauvegardes: Path = Path("/srv/agence-ia/sauvegardes")
    sauvegardes_conservees: int = 7
    # Production de nuit (noyau/creneaux.py), heure de Paris.
    nuit_debut: time = time(0, 0)
    abonnement_libre: time = time(8, 0)
    abonnement_libre_max: time = time(9, 0)
    marge_validation_heures: float = 2
    # Publication automatique sur Instagram : désactivée tant que tout n'est pas réglé.
    publication_automatique: bool = False
    instagram_api_url: str = ""
    instagram_compte_id: str = ""
    instagram_jeton: str = ""
    url_publique: str = ""
    # Niveau sonore des reels, réglé après le rendu (noyau/video.py).
    son_cible_lufs: float = -16
    son_crete_dbtp: float = -1.5
    # Nom affiché dans l'interface, l'application installée et les notifications.
    nom_agence: str = "Mon agence"


def _chemin(valeur: str) -> Path:
    chemin = Path(valeur).expanduser()
    return chemin if chemin.is_absolute() else RACINE / chemin


def charger(valeurs: Mapping[str, str | None] | None = None) -> Config:
    """Lit .env puis l'environnement (qui a priorité), ou `valeurs` si fourni (tests)."""
    if valeurs is None:
        valeurs = {**dotenv_values(RACINE / ".env"), **os.environ}

    erreurs: list[str] = []

    def texte(nom: str, obligatoire: bool = True) -> str:
        valeur = (valeurs.get(nom) or "").strip()
        if obligatoire and not valeur:
            erreurs.append(f"{nom} manquant")
        return valeur

    def nombre(nom: str, minimum: float = 0) -> float:
        brut = texte(nom)
        if not brut:
            return 0
        try:
            valeur = float(brut)
        except ValueError:
            erreurs.append(f"{nom} doit être un nombre (reçu : {brut!r})")
            return 0
        if valeur < minimum:
            erreurs.append(f"{nom} doit être supérieur ou égal à {minimum:g} (reçu : {brut})")
        return valeur

    def heure(nom: str, defaut: time) -> time:
        brut = texte(nom, obligatoire=False)
        if not brut:
            return defaut
        try:
            return time.fromisoformat(brut)
        except ValueError:
            erreurs.append(f"{nom} doit être une heure HH:MM (reçu : {brut!r})")
            return defaut

    config = Config(
        dossier_contenus=_chemin(texte("DOSSIER_CONTENUS")),
        dossier_temp=_chemin(texte("DOSSIER_TEMP")),
        chemin_base=_chemin(texte("CHEMIN_BASE")),
        disque_seuil_alerte_go=nombre("DISQUE_SEUIL_ALERTE_GO"),
        disque_seuil_refus_go=nombre("DISQUE_SEUIL_REFUS_GO"),
        corbeille_delai_heures=nombre("CORBEILLE_DELAI_HEURES", minimum=1),
        publication_delai_jours=nombre("PUBLICATION_DELAI_JOURS", minimum=1),
        plafond_couts_mensuel_usd=nombre("PLAFOND_COUTS_MENSUEL_USD"),
        quota_seuil_pourcent=nombre("QUOTA_SEUIL_POURCENT", minimum=1),
        claude_code_oauth_token=texte("CLAUDE_CODE_OAUTH_TOKEN", obligatoire=False),
        dossier_config_claude=_chemin(texte("DOSSIER_CONFIG_CLAUDE")),
        hote_web=texte("HOTE_WEB"),
        port_web=int(nombre("PORT_WEB", minimum=1)),
        mot_de_passe_interface=texte("MOT_DE_PASSE_INTERFACE", obligatoire=False),
        secret_session=texte("SECRET_SESSION", obligatoire=False),
        ntfy_serveur=(texte("NTFY_SERVEUR", obligatoire=False) or "https://ntfy.sh").rstrip("/"),
        ntfy_sujet=texte("NTFY_SUJET", obligatoire=False),
        notifications_silence=texte("NOTIFICATIONS_SILENCE", obligatoire=False) or "22:00-08:00",
        dossier_sauvegardes=_chemin(texte("DOSSIER_SAUVEGARDES", obligatoire=False) or "/srv/agence-ia/sauvegardes"),
        sauvegardes_conservees=int(nombre("SAUVEGARDES_CONSERVEES", minimum=1)) if texte("SAUVEGARDES_CONSERVEES", obligatoire=False) else 7,
        nuit_debut=heure("NUIT_DEBUT", time(0, 0)),
        abonnement_libre=heure("ABONNEMENT_LIBRE", time(8, 0)),
        abonnement_libre_max=heure("ABONNEMENT_LIBRE_MAX", time(9, 0)),
        marge_validation_heures=float(texte("MARGE_VALIDATION_HEURES", obligatoire=False) or 2),
        publication_automatique=texte("PUBLICATION_AUTOMATIQUE", obligatoire=False).lower() in ("oui", "1", "true"),
        instagram_api_url=texte("INSTAGRAM_API_URL", obligatoire=False).rstrip("/"),
        instagram_compte_id=texte("INSTAGRAM_COMPTE_ID", obligatoire=False),
        instagram_jeton=texte("INSTAGRAM_JETON", obligatoire=False),
        url_publique=texte("URL_PUBLIQUE", obligatoire=False).rstrip("/"),
        son_cible_lufs=float(texte("SON_CIBLE_LUFS", obligatoire=False) or -16),
        son_crete_dbtp=float(texte("SON_CRETE_DBTP", obligatoire=False) or -1.5),
        nom_agence=texte("NOM_AGENCE", obligatoire=False) or "Mon agence",
    )

    try:
        debut, fin = config.notifications_silence.split("-")
        time.fromisoformat(debut.strip()), time.fromisoformat(fin.strip())
    except ValueError:
        erreurs.append(f"NOTIFICATIONS_SILENCE doit être une plage HH:MM-HH:MM (reçu : {config.notifications_silence!r})")

    if config.abonnement_libre_max < config.abonnement_libre:
        erreurs.append("ABONNEMENT_LIBRE_MAX doit être après ABONNEMENT_LIBRE")

    if config.publication_automatique:
        manquants = [nom for nom, valeur in (("INSTAGRAM_API_URL", config.instagram_api_url),
                                             ("INSTAGRAM_COMPTE_ID", config.instagram_compte_id),
                                             ("INSTAGRAM_JETON", config.instagram_jeton),
                                             ("URL_PUBLIQUE", config.url_publique)) if not valeur]
        erreurs += [f"{nom} manquant (PUBLICATION_AUTOMATIQUE=oui)" for nom in manquants]
        if config.url_publique and not config.url_publique.startswith("https://"):
            erreurs.append("URL_PUBLIQUE doit commencer par https:// (Meta va y chercher les images)")

    if not erreurs and config.disque_seuil_refus_go >= config.disque_seuil_alerte_go:
        erreurs.append("DISQUE_SEUIL_REFUS_GO doit être inférieur à DISQUE_SEUIL_ALERTE_GO")

    if erreurs:
        raise ConfigErreur(
            "Configuration invalide (voir .env.example) :\n- " + "\n- ".join(erreurs)
        )
    return config


def ecrire_env(valeurs: Mapping[str, str], chemin: Path | None = None) -> None:
    """Remplace (ou ajoute) des lignes CLE=valeur dans .env, entre apostrophes (aucune interpolation)."""
    chemin = chemin or RACINE / ".env"
    lignes = chemin.read_text(encoding="utf-8").splitlines() if chemin.exists() else []
    restantes = dict(valeurs)
    for i, ligne in enumerate(lignes):
        cle = ligne.split("=", 1)[0].strip()
        if cle in restantes:
            lignes[i] = f"{cle}='{restantes.pop(cle)}'"
    lignes += [f"{cle}='{valeur}'" for cle, valeur in restantes.items()]
    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    chemin.chmod(0o600)
