"""agent_veille : veille hebdomadaire du marché et des concurrents, par recherche web.

Entrée : demande_veille (semaine préparée). Sortie : un rapport de veille enregistré (contenu du message
rapport_veille), lu par l'orchestrateur au moment du planning. Les concurrents sont mémorisés d'une semaine
sur l'autre : la veille suivante approfondit la liste au lieu de tout redécouvrir, ce qui ménage le quota.
L'agence s'inspire des formats, thèmes et angles qui marchent ; elle ne copie jamais un texte ou un visuel.
"""

import json

from pydantic import BaseModel, Field

from agents.base import Agent, Outils
from noyau.contexte import Contexte
from noyau.erreurs import ErreurDefinitive
from noyau.file import Tache
from noyau.messages import Concurrent, DemandeVeille, RapportVeille
from noyau.modeles import SONNET, Execution, execution

PAR_DEFAUT = Execution(SONNET, "high")
MAX_CONCURRENTS = 8

SYSTEME = """Tu es agent_veille, responsable de la veille marketing de la marque décrite ci-dessous.
Ta seule tâche : observer, par recherche web, le marché et les principaux concurrents de la marque, pour
nourrir le calendrier éditorial de la semaine à venir.

Méthode :
- Identifie toi-même les principaux concurrents : marques et comptes qui visent la même clientèle
  dans la zone de la charte, et quelques références nationales qui réussissent sur
  Instagram. Garde les concurrents déjà connus (liste ci-dessous) s'ils restent pertinents ; complète la liste
  sans dépasser {max_concurrents}.
- Pour chacun, observe ce qui marche dans sa communication : formats (carrousels, avant / après, coulisses,
  conseils), thèmes, angles, ton, cadence. Appuie-toi sur des sources vérifiables (sites, articles, pages publiques).
- Dégage les tendances et des recommandations concrètes pour la marque, cohérentes avec sa charte.
- Lisibilité : chaque élément de liste commence par une phrase courte (12 mots au plus) qui le résume,
  suivie d'un point, puis le détail ; la synthèse tient en 3 à 5 phrases courtes.
- Économise les recherches : quelques requêtes ciblées valent mieux que beaucoup de pages lues.

Interdits : copier un texte ou un visuel ; inventer un concurrent, un chiffre ou une source.

# Charte de marque

{charte}
"""


class ReponseVeille(BaseModel):
    concurrents: list[Concurrent] = Field(min_length=1, max_length=MAX_CONCURRENTS)
    tendances: list[str] = Field(min_length=1)
    recommandations: list[str] = Field(min_length=1)
    sources: list[str] = Field(min_length=1, description="URL consultées")
    synthese: str = Field(min_length=1, description="Synthèse en quelques phrases, pour l'humain")


class AgentVeille(Agent):
    id = "agent_veille"

    def traiter(self, tache: Tache, contexte: Contexte, outils: Outils) -> str:
        if tache.type != "demande_veille":
            raise ErreurDefinitive(f"agent_veille ne traite pas les messages {tache.type}")
        demande: DemandeVeille = tache.message
        choix = execution(demande, PAR_DEFAUT)
        conn = outils.file.conn

        connus = [dict(l) for l in conn.execute("SELECT nom, site, zone, ce_qui_marche FROM concurrents ORDER BY maj_le DESC")]
        precedent = conn.execute("SELECT contenu FROM rapports_veille ORDER BY id DESC LIMIT 1").fetchone()
        consigne = [f"Veille pour la semaine du {demande.semaine:%d/%m/%Y}."]
        consigne.append("Concurrents déjà connus :\n" + (json.dumps(connus, ensure_ascii=False, indent=1) if connus else "aucun"))
        if precedent:
            consigne.append("Synthèse de la veille précédente : " + json.loads(precedent["contenu"])["synthese"])

        reponse = outils.claude.demander(
            systeme=SYSTEME.format(charte=contexte.charte, max_concurrents=MAX_CONCURRENTS),
            consigne="\n\n".join(consigne),
            schema=ReponseVeille,
            dossier_travail=outils.dossier_travail,
            tache_id=tache.id,
            modele=choix.modele,
            effort=choix.effort,
            web=True,
        )
        rapport = RapportVeille(semaine=demande.semaine, **reponse.model_dump())
        date = outils.file.horloge().isoformat()
        conn.execute("INSERT INTO rapports_veille (tache_id, semaine, contenu, cree_le) VALUES (?, ?, ?, ?)",
                     (tache.id, demande.semaine.isoformat(), rapport.model_dump_json(), date))
        for concurrent in rapport.concurrents:
            conn.execute(
                "INSERT INTO concurrents (nom, site, zone, ce_qui_marche, decouvert_le, maj_le) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (nom) DO UPDATE SET site = COALESCE(excluded.site, site), zone = COALESCE(excluded.zone, zone), "
                "ce_qui_marche = excluded.ce_qui_marche, maj_le = excluded.maj_le",
                (concurrent.nom, concurrent.site, concurrent.zone, concurrent.ce_qui_marche, date, date),
            )
        return "terminé"
