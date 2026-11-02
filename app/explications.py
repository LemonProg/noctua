"""Les erreurs techniques des tâches, dites en français simple, avec ce qu'il faut faire.

Le message d'origine reste affiché en petit (utile pour un diagnostic) ; l'explication passe devant. Les motifs
couvrent les erreurs déjà rencontrées et celles que le code peut lever (noyau/claude.py, video.py, rendu.py, instagram.py).
"""

import re

# (motif, explication, que faire). Le premier motif reconnu l'emporte.
EXPLICATIONS = (
    (r"code 137|killed|out of memory|mémoire|ENOMEM|heap out of memory",
     "Le serveur a manqué de mémoire pendant le travail (souvent un rendu vidéo).",
     "Relancez : une seule production lourde tourne à la fois, la mémoire devrait suffire. Si l'erreur revient, "
     "raccourcissez la vidéo demandée."),
    (r"Abonnement Claude utilisé|rate.?limit|usage limit|quota",
     "L'abonnement Claude était presque plein : l'agence s'est mise en pause.",
     "Relancez après la remise à zéro du quota (heure affichée en bas du menu)."),
    (r"n'a pas répondu en|trop long|timed? ?out|délai",
     "Le travail a pris trop de temps et a été interrompu.",
     "Relancez : c'est souvent passager. Si cela se répète, la tâche est peut-être trop ambitieuse."),
    (r"Réponse illisible|Réponse sans sortie structurée|non conforme au schéma|Erreur de Claude",
     "Claude a renvoyé une réponse incomplète ou mal formée.",
     "Relancez : une nouvelle tentative suffit presque toujours."),
    (r"Commande introuvable|Claude Code est-il installé",
     "Claude Code n'est pas installé ou pas trouvé sur le serveur.",
     "Installez Claude Code sur le serveur et connectez-le à l'abonnement, puis relancez."),
    (r"HyperFrames n'est pas installé|FFmpeg introuvable|Chromium introuvable",
     "Un outil nécessaire à la production manque sur le serveur.",
     "Relancez le script d'installation du serveur (deploy/installer.sh), puis la tâche."),
    (r"Ressources externes interdites",
     "La vidéo voulait charger un élément depuis Internet, ce qui est interdit.",
     "Relancez : l'agent recommencera la composition."),
    (r"Rendu Chromium|aucune image|Image rendue en",
     "Le visuel n'a pas pu être dessiné correctement.",
     "Relancez : l'agent refera le rendu."),
    (r"OAuthException|access token|jeton|Error validating|\b190\b|\(#10\)|\(#200\)",
     "Instagram a refusé l'accès : le jeton n'est plus valable ou manque d'une autorisation.",
     "Vérifiez le jeton Instagram dans .env (commande agence instagram verifier), puis relancez."),
    (r"\(#9\)|limite de publication|content_publishing_limit",
     "La limite de publications Instagram du jour est atteinte.",
     "Relancez demain : la limite se réinitialise sur 24 heures."),
    (r"URLError|Connection|connexion|Name or service not known|Network is unreachable|temporairement",
     "Le serveur n'a pas pu joindre un service sur Internet.",
     "Relancez dans quelques minutes : c'est en général passager."),
    (r"No space left|disque",
     "Le disque du serveur est plein.",
     "Libérez de la place (Réglages → Corbeille), puis relancez."),
    (r"OpenAI|gpt-image",
     "Le service d'images d'OpenAI a refusé la demande.",
     "Relancez ; si l'erreur revient, elle vient d'un réglage du modèle d'images à corriger dans le code."),
)


def expliquer(erreur: str | None) -> dict | None:
    """{"explication", "conseil"} pour un message d'erreur reconnu, sinon None (le message brut suffit alors)."""
    if not erreur:
        return None
    for motif, explication, conseil in EXPLICATIONS:
        if re.search(motif, erreur, re.IGNORECASE):
            return {"explication": explication, "conseil": conseil}
    return None
