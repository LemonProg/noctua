"""Erreurs que le worker traite à part des erreurs ordinaires (3 tentatives puis échec)."""

from datetime import timedelta


class ErreurDefinitive(Exception):
    """Une nouvelle tentative ne changerait rien : la tâche passe directement en échec."""


class ErreurTemporaire(Exception):
    """Ressource indisponible pour un temps (ex. quota d'abonnement atteint).

    La tâche est remise dans la file sans consommer de tentative, et sa voie se met en pause.
    """

    def __init__(self, message: str, reessayer_dans: timedelta):
        super().__init__(message)
        self.reessayer_dans = reessayer_dans
