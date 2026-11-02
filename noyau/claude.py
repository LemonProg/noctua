"""Moteur Claude : appel de Claude Opus 5.5 via l'abonnement de l'humain (commande `claude -p`).

Pas de clé API Anthropic : toute clé présente dans l'environnement est retirée avant l'appel,
pour que Claude Code utilise le jeton d'abonnement (CLAUDE_CODE_OAUTH_TOKEN).

Chaque appel est isolé : aucun outil (Claude reçoit un texte et rend un JSON), aucune session
enregistrée, dossier de configuration Claude réservé à l'agence, dossier de travail vide.
La réponse est imposée par un schéma JSON, puis revérifiée par Pydantic.
"""

import json
import os
import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from noyau.config import Config
from noyau.couts import JournalCouts
from noyau.erreurs import ErreurDefinitive, ErreurTemporaire
from noyau.modeles import HAIKU, MODELES, OPUS
from noyau.creneaux import Creneaux
from noyau.pause import Pause
from noyau.quota import Quota

MODELE = OPUS  # modèle par défaut
FOURNISSEUR = "claude-abonnement"
DELAI_MAX_SECONDES = 600
ATTENTE_QUOTA_PAR_DEFAUT = timedelta(minutes=30)

# Variables qui feraient passer Claude Code par l'API (facturée) ou par un autre compte.
VARIABLES_RETIREES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)

Modele = TypeVar("Modele", bound=BaseModel)


class ClaudeErreur(Exception):
    """Erreur ordinaire (réseau, surcharge, réponse non conforme) : la tâche sera retentée."""


class AuthentificationClaude(ClaudeErreur, ErreurDefinitive):
    pass


class RefusClaude(ClaudeErreur, ErreurDefinitive):
    pass


class QuotaAtteint(ErreurTemporaire):
    pass


class PauseHumaine(ErreurTemporaire):
    """L'humain a mis l'agence en pause : la tâche attend sa reprise (vérifiée chaque minute)."""


class MoteurClaude:
    def __init__(self, config: Config, couts: JournalCouts, commande: Sequence[str] = ("claude",),
                 delai_max_secondes: float = DELAI_MAX_SECONDES):
        self.config = config
        self.couts = couts
        self.commande = list(commande)
        self.delai_max = delai_max_secondes

    def _environnement(self) -> dict[str, str]:
        if not self.config.claude_code_oauth_token:
            raise AuthentificationClaude(
                "CLAUDE_CODE_OAUTH_TOKEN est vide dans .env : générez un jeton avec `claude setup-token`."
            )
        env = {k: v for k, v in os.environ.items() if k not in VARIABLES_RETIREES}
        self.config.dossier_config_claude.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(self.config.dossier_config_claude)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = self.config.claude_code_oauth_token
        return env

    def demander(self, *, systeme: str, consigne: str, schema: type[Modele], dossier_travail: Path,
                 effort: str = "medium", tache_id: int | None = None, lecture: bool = False,
                 modele: str = MODELE, web: bool = False) -> Modele:
        """Envoie la consigne et renvoie la réponse validée par `schema`.

        `lecture=True` autorise le seul outil Read, limité au dossier de travail (ex. pour voir des photos) :
        toute lecture ailleurs est refusée par le mode de permission « dontAsk ».
        """
        if modele not in MODELES:
            raise ErreurDefinitive(f"Modèle inconnu : {modele} (attendus : {', '.join(MODELES)})")
        if Pause(self.couts.conn).etat():
            raise PauseHumaine("Agence mise en pause par vous : aucun appel à Claude jusqu'à la reprise",
                               timedelta(minutes=1))
        quota = Quota(self.couts.conn)
        # La nuit, la fenêtre de 5 heures se termine avant le réveil de l'humain : elle peut servir entièrement.
        nuit = Creneaux.depuis_config(self.config).periode_nuit()
        attente = quota.attente_avant_appel(self.config.quota_seuil_pourcent, 100 if nuit else None)
        if attente:
            raise QuotaAtteint(f"Abonnement Claude utilisé à plus de {self.config.quota_seuil_pourcent:g} % : "
                               "pause de l'agence jusqu'à la remise à zéro", attente)
        # `web=True` autorise la recherche et la lecture de pages web (veille), pré-approuvées.
        outils_web = ["WebSearch", "WebFetch"] if web else []
        outils = (["Read"] if lecture else []) + outils_web
        fichier_systeme = dossier_travail / "systeme.md"
        fichier_systeme.write_text(systeme, encoding="utf-8")
        arguments = [
            *self.commande, "-p",
            "--model", modele,
            "--effort", effort,
            "--output-format", "stream-json", "--verbose",  # le flux transmet aussi le quota de l'abonnement
            "--json-schema", json.dumps(schema.model_json_schema(), ensure_ascii=False),
            "--system-prompt-file", str(fichier_systeme),
            # Aucun outil de Claude Code sauf ceux demandés (lecture, web) ; l'outil interne de sortie JSON reste permis.
            "--tools", ",".join(outils),
            "--disallowedTools", "mcp__*",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--permission-mode", "dontAsk",
        ]
        if outils_web:
            arguments += ["--allowedTools", ",".join(outils_web)]
        try:
            processus = subprocess.run(
                arguments, input=consigne, capture_output=True, text=True, cwd=dossier_travail,
                env=self._environnement(), timeout=self.delai_max,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeErreur(f"Claude n'a pas répondu en {self.delai_max:g} secondes") from None
        except FileNotFoundError:
            raise ErreurDefinitive(f"Commande introuvable : {self.commande[0]} (Claude Code est-il installé ?)") from None
        finally:
            fichier_systeme.unlink(missing_ok=True)

        evenements = []
        for ligne in processus.stdout.splitlines():
            try:
                evenements.append(json.loads(ligne))
            except json.JSONDecodeError:
                continue
        for evenement in evenements:
            if evenement.get("type") == "rate_limit_event":
                quota.enregistrer(evenement.get("rate_limit_info") or {})
        reponse = next((e for e in reversed(evenements) if e.get("type") == "result"), None)
        if reponse is None:
            detail = (processus.stderr or processus.stdout).strip()[:300]
            raise ClaudeErreur(f"Réponse illisible de Claude (code {processus.returncode}) : {detail}")

        self._enregistrer_usage(reponse, tache_id, modele)
        self._verifier_erreurs(reponse)

        donnees = reponse.get("structured_output")
        if donnees is None:
            raise ClaudeErreur(f"Réponse sans sortie structurée : {str(reponse.get('result'))[:300]}")
        try:
            return schema.model_validate(donnees)
        except ValidationError as erreur:
            raise ClaudeErreur(f"Réponse non conforme au schéma : {erreur}") from None

    def actualiser_quota(self, dossier_travail: Path) -> dict:
        """Mesure le quota de l'abonnement par un appel minimal (Haiku 5.5, effort bas, sans outil).

        Claude Code ne transmet le quota qu'avec une réponse du modèle : il n'existe pas de mesure gratuite.
        Pas de vérification du seuil ici : la mesure doit rester possible quand l'agence est en pause.
        """
        try:
            processus = subprocess.run(
                [*self.commande, "-p", "--model", HAIKU, "--effort", "low", "--output-format", "stream-json",
                 "--verbose", "--system-prompt", "Réponds seulement : OK.", "--tools", "", "--strict-mcp-config",
                 "--disable-slash-commands", "--no-session-persistence", "--permission-mode", "dontAsk"],
                input="OK", capture_output=True, text=True, cwd=dossier_travail, env=self._environnement(),
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeErreur("Mesure du quota trop longue") from None
        except FileNotFoundError:
            raise ErreurDefinitive(f"Commande introuvable : {self.commande[0]} (Claude Code est-il installé ?)") from None
        quota = Quota(self.couts.conn)
        for ligne in processus.stdout.splitlines():
            try:
                evenement = json.loads(ligne)
            except json.JSONDecodeError:
                continue
            if evenement.get("type") == "rate_limit_event":
                quota.enregistrer(evenement.get("rate_limit_info") or {})
            elif evenement.get("type") == "result":
                self._enregistrer_usage(evenement, None, HAIKU)
        return quota.actuel()

    def _enregistrer_usage(self, reponse: dict, tache_id: int | None, modele: str) -> None:
        usage = reponse.get("usage") or {}
        if not usage:
            return
        entree = sum(int(usage.get(k) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        self.couts.enregistrer(FOURNISSEUR, modele, 0, jetons_entree=entree,
                               jetons_sortie=int(usage.get("output_tokens") or 0), tache_id=tache_id)

    def _verifier_erreurs(self, reponse: dict) -> None:
        texte = str(reponse.get("result") or "")
        statut = reponse.get("api_error_status")
        if reponse.get("stop_reason") == "refusal" or reponse.get("safety_stops"):
            raise RefusClaude(f"Claude a refusé la demande : {texte[:300]}")
        if not reponse.get("is_error"):
            return
        if statut in (401, 403) or re.search(r"authenticat|/login|oauth", texte, re.IGNORECASE):
            raise AuthentificationClaude(
                f"Authentification refusée ({texte[:200]}). Régénérez le jeton avec `claude setup-token`."
            )
        if statut == 429 or re.search(r"limit", texte, re.IGNORECASE):
            raise QuotaAtteint(f"Quota de l'abonnement Claude atteint : {texte[:200]}", _attente_quota(texte))
        raise ClaudeErreur(f"Erreur de Claude ({statut or 'sans code'}) : {texte[:300]}")


def _attente_quota(texte: str, maintenant: datetime | None = None) -> timedelta:
    """Délai avant de réessayer, d'après l'heure de reprise indiquée par Claude Code :
    horodatage Unix après « | », ou « resets 10:40pm (Europe/Paris) »."""
    maintenant = maintenant or datetime.now(UTC)
    if trouve := re.search(r"\|(\d{10})\b", texte):
        reprise = datetime.fromtimestamp(int(trouve.group(1)), UTC)
    elif trouve := re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", texte, re.IGNORECASE):
        heure, minute, periode, fuseau = trouve.groups()
        try:
            zone = ZoneInfo(fuseau.strip())
        except (ZoneInfoNotFoundError, ValueError):
            return ATTENTE_QUOTA_PAR_DEFAUT
        heure = int(heure) % 12 + (12 if periode.lower() == "pm" else 0)
        local = maintenant.astimezone(zone)
        reprise = local.replace(hour=heure, minute=int(minute or 0), second=0, microsecond=0)
        if reprise <= local:
            reprise += timedelta(days=1)
    else:
        return ATTENTE_QUOTA_PAR_DEFAUT
    return max(reprise - maintenant + timedelta(minutes=1), timedelta(minutes=1))
