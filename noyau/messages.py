"""Messages typés échangés via la file de tâches (voir docs/architecture.md).

Chaque message déclare son type, ses destinataires possibles et sa voie.
Un message invalide est refusé ici, avant d'entrer dans la file.
"""

from datetime import date, datetime
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from noyau.modeles import EFFORTS, MODELES, OPUS, TYPES_CONTENU

AGENTS = (
    "orchestrateur_marketing",
    "agent_veille",
    "agent_visuel",
    "agent_video",
    "agent_redacteur",
    "agent_analyste",
    "orchestrateur_prospection",
    "agent_recherche",
    "agent_message",
    "agent_suivi",
    "agent_relecteur",
    "agent_erreurs",
    "agent_publication",
)
# Destinataire des messages qui attendent une décision humaine (leçons).
HUMAIN = "humain"

Agent = Literal[AGENTS]
Texte = Annotated[str, Field(min_length=1)]
Identifiant = Annotated[int, Field(gt=0)]


class MessageInvalide(ValueError):
    pass


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    type: ClassVar[str]
    destinataires: ClassVar[tuple[str, ...]]
    voie: ClassVar[str] = "normale"

    # Modèle et effort que l'agent destinataire doit utiliser, choisis par l'émetteur (l'orchestrateur
    # les précise toujours). Absents : valeurs par défaut de l'agent. Contenu : Opus 5.5 imposé.
    modele: Literal[MODELES] | None = None
    effort: Literal[EFFORTS] | None = None

    @model_validator(mode="after")
    def _opus_pour_le_contenu(self):
        if self.type in TYPES_CONTENU and self.modele not in (None, OPUS):
            raise ValueError(f"la génération de contenu se fait avec {OPUS}, pas {self.modele}")
        return self


class Periode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    debut: date
    fin: date

    @model_validator(mode="after")
    def _ordre(self):
        if self.fin < self.debut:
            raise ValueError("la fin de la période est avant son début")
        return self


class Objectif(Message):
    type = "objectif"
    destinataires = ("orchestrateur_marketing",)

    but: Texte
    periode: Periode
    reseaux: list[Texte] = Field(min_length=1)


class Cibles(Message):
    type = "cibles"
    destinataires = ("orchestrateur_prospection",)

    type_clients: Texte
    zone: Texte
    volume: Identifiant


class _Brief(Message):
    destinataires: ClassVar[tuple[str, ...]]

    objectif: Texte
    format: Texte
    reseau: Texte
    rushs: list[Identifiant] = []
    date_prevue: datetime


class BriefVisuel(_Brief):
    type = "brief_visuel"
    destinataires = ("agent_visuel",)
    voie = "rendu"


class BriefVideo(_Brief):
    type = "brief_video"
    destinataires = ("agent_video",)
    voie = "rendu"


class BriefTexte(_Brief):
    """Post photo : le texte accompagne des rushs publiés tels quels. Jamais de post texte seul."""

    type = "brief_texte"
    destinataires = ("agent_redacteur",)

    @model_validator(mode="after")
    def _au_moins_une_photo(self):
        if not self.rushs:
            raise ValueError("un post sans visuel n'est pas accepté : choisir au moins un rush, ou un brief visuel")
        return self


class Asset(Message):
    type = "asset"
    destinataires = ("agent_redacteur",)

    brief_id: Identifiant
    type_media: Literal["image", "vidéo"]
    asset_id: Identifiant  # l'image, ou la première slide d'un carrousel
    autres_slides: list[Identifiant] = []  # slides suivantes d'un carrousel, dans l'ordre
    prompt_ou_script: Texte


class PostBrouillon(Message):
    type = "post_brouillon"
    destinataires = ("agent_relecteur",)

    texte: Texte
    medias: list[Identifiant] = []
    reseau: Texte
    horaire_prevu: datetime
    # Étiquette « créé avec l'IA » sur le réseau, cochée ou non par l'humain à la validation.
    etiquette_ia: bool = False


class MessageBrouillon(Message):
    type = "message_brouillon"
    destinataires = ("agent_relecteur",)

    prospect: Texte
    canal: Texte
    texte: Texte


class Corrections(Message):
    type = "corrections"
    destinataires = ("orchestrateur_marketing", "orchestrateur_prospection")

    contenu_id: Identifiant
    problemes: list[Texte] = Field(min_length=1)
    origine: Literal["relecteur", "humain"] = "relecteur"
    # Demande de modification de l'humain avec « tout de suite » : nouvelle version produite sans attendre la nuit.
    immediat: bool = False


class ModificationTexte(Message):
    """Demande de modification de l'humain qui ne touche que le texte : le rédacteur retouche le texte actuel du
    post, avec les mêmes médias et le même horaire (le visuel n'est pas refait)."""

    type = "modification_texte"
    destinataires = ("agent_redacteur",)

    post_id: Identifiant
    consignes: list[Texte] = Field(min_length=1)


class Rapport(Message):
    type = "rapport"
    destinataires = ("orchestrateur_marketing",)

    periode: Periode
    chiffres_cles: dict[Texte, int | float | str] = Field(min_length=1)
    recommandations: list[Texte] = Field(min_length=1)
    synthese: str | None = None
    ce_qui_marche: list[Texte] = []
    ce_qui_ne_marche_pas: list[Texte] = []


class Statut(Message):
    type = "statut"
    destinataires = ("orchestrateur_prospection",)

    prospect: Texte
    etape: Texte
    prochaine_action: Texte


class ContenuRejete(Message):
    type = "contenu_rejete"
    destinataires = ("agent_erreurs",)

    contenu_id: Identifiant
    origine: Literal["refus_humain", "ne_pas_utiliser", "relecteur"]
    contenu: dict[str, Any] = Field(min_length=1)
    brief: dict[str, Any] | None = None
    commentaire: Texte | None = None
    corrections: list[Texte] = []

    @model_validator(mode="after")
    def _justification(self):
        if self.origine == "refus_humain" and not self.commentaire:
            raise ValueError("un refus humain doit avoir un commentaire")
        if self.origine == "relecteur" and not self.corrections:
            raise ValueError("un rejet du relecteur doit lister ses corrections")
        return self


class Lecon(Message):
    type = "lecon"
    destinataires = (HUMAIN,)

    contenu_id: Identifiant
    erreur: Texte
    cause: Texte
    regle: Texte
    agents_concernes: list[Agent] = Field(min_length=1)
    # Leçon déjà validée dont celle-ci est proche (l'humain peut alors la refuser).
    rappel_de: Identifiant | None = None


class AnalyseStyle(Message):
    type = "analyse_style"
    destinataires = ("agent_visuel",)
    voie = "rendu"

    references: list[Identifiant] = Field(min_length=1)


class DemandeVeille(Message):
    type = "demande_veille"
    destinataires = ("agent_veille",)

    semaine: date  # lundi de la semaine préparée


class DemandeAnalyse(Message):
    """Le planificateur la crée chaque dimanche, avant la veille et le planning ; l'humain peut la lancer."""

    type = "demande_analyse"
    destinataires = ("agent_analyste",)

    semaine: date  # lundi de la semaine qui sera planifiée
    forcer: bool = False  # analyser même sans nouvelle statistique depuis le dernier rapport


class DemandePublication(Message):
    """Le worker la crée quand l'horaire d'un post validé arrive (publication automatique activée)."""

    type = "demande_publication"
    destinataires = ("agent_publication",)

    post_id: Identifiant


class DemandePlanning(Message):
    type = "demande_planning"
    destinataires = ("orchestrateur_marketing",)

    semaine: date  # lundi de la semaine à planifier
    nombre_posts: int = Field(4, ge=1, le=14)


class Concurrent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nom: Texte
    site: str | None = None
    zone: str | None = None
    ce_qui_marche: Texte


class RapportVeille(Message):
    type = "rapport_veille"
    destinataires = ("orchestrateur_marketing",)

    semaine: date
    concurrents: list[Concurrent] = Field(min_length=1)
    tendances: list[Texte] = Field(min_length=1)
    recommandations: list[Texte] = Field(min_length=1)
    sources: list[Texte] = Field(min_length=1)
    synthese: Texte


MESSAGES: dict[str, type[Message]] = {
    m.type: m
    for m in (
        Objectif, Cibles, BriefVisuel, BriefVideo, BriefTexte, Asset, PostBrouillon,
        MessageBrouillon, Corrections, ModificationTexte, Rapport, Statut, ContenuRejete, Lecon, AnalyseStyle,
        DemandeVeille, DemandePlanning, RapportVeille, DemandePublication, DemandeAnalyse,
    )
}

_TRADUCTIONS = {
    "missing": "champ obligatoire manquant",
    "extra_forbidden": "champ inconnu",
    "string_too_short": "ne doit pas être vide",
    "too_short": "doit contenir au moins un élément",
    "greater_than": "doit être un identifiant positif",
    "int_parsing": "doit être un nombre entier",
    "int_type": "doit être un nombre entier",
    "datetime_from_date_parsing": "date et heure invalides",
    "datetime_parsing": "date et heure invalides",
    "datetime_type": "date et heure invalides",
    "date_from_datetime_parsing": "date invalide",
    "date_parsing": "date invalide",
    "literal_error": "valeur non autorisée",
    "list_type": "doit être une liste",
    "dict_type": "doit être un objet",
    "string_type": "doit être du texte",
}


def _traduire(erreur: ValidationError) -> str:
    lignes = []
    for e in erreur.errors():
        champ = ".".join(str(p) for p in e["loc"]) or "message"
        texte = _TRADUCTIONS.get(e["type"])
        if texte is None:
            texte = str(e.get("ctx", {}).get("error", e["msg"]))
        lignes.append(f"{champ} : {texte}")
    return "\n- ".join(lignes)


def valider(type_message: str, donnees: dict[str, Any]) -> Message:
    """Construit le message typé, ou lève MessageInvalide avec la liste des problèmes en français."""
    classe = MESSAGES.get(type_message)
    if classe is None:
        raise MessageInvalide(f"Type de message inconnu : {type_message!r}")
    try:
        return classe.model_validate(donnees)
    except ValidationError as erreur:
        raise MessageInvalide(f"Message {type_message} invalide :\n- {_traduire(erreur)}") from None
