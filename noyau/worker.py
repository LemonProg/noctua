"""Worker : un seul processus qui exécute les agents à partir de la file de tâches.

Deux fils d'exécution : un pour la voie normale, un pour la voie `rendu`,
pour qu'un long rendu vidéo ne bloque pas les autres agents. Chaque voie ne prend que les tâches
permises à cette heure (production de nuit, voir noyau/creneaux.py).

Lancement : python -m noyau.worker [--agent-factice]
"""

import argparse
import fcntl
import logging
import signal
import threading
import time
from datetime import timedelta
from collections.abc import Sequence
from pathlib import Path

from agents.base import Agent, Outils
from noyau.base import connecter
from noyau.claude import MoteurClaude
from noyau.config import RACINE, Config, charger
from noyau.contexte import CharteErreur, lire
from noyau.creneaux import Creneaux
from noyau.couts import JournalCouts, PlafondAtteint
from noyau.erreurs import ErreurDefinitive, ErreurTemporaire
from noyau.file import File, Tache
from noyau.horloge import Horloge, maintenant_utc
from noyau.messages import AGENTS, DemandePublication
from noyau.instagram import ClientInstagram
from noyau.notifications import Notifications
from noyau.statistiques import Statistiques
from noyau.planificateur import Planificateur
from noyau.purge import Purge
from noyau.publication import Publications
from noyau.pause import Pause
from noyau.quota import Quota
from noyau.stockage import Bibliotheque, dossier_de_travail, nettoyer_travail

journal = logging.getLogger("agence.worker")

# Erreurs qu'une nouvelle tentative ne corrigera pas : la tâche passe directement en échec.
# Mesure du quota de l'abonnement si aucun appel de l'agence ne l'a rafraîchie depuis ce délai.
AGE_MAX_MESURE_QUOTA = timedelta(hours=1)
# Relevé des statistiques Instagram par l'API (lectures seules, sans appel à Claude).
INTERVALLE_RELEVE_STATISTIQUES = timedelta(hours=12)
# Après un échec (accès bloqué par Meta, jeton expiré…) : nouvel essai seulement après ce délai, pas à chaque tour.
ATTENTE_APRES_ECHEC_RELEVE = timedelta(hours=3)
ERREURS_DEFINITIVES = (PlafondAtteint, CharteErreur, ErreurDefinitive)


class WorkerDejaLance(Exception):
    pass


class Worker:
    def __init__(self, config: Config, agents: list[Agent], *, racine: Path = RACINE,
                 horloge: Horloge = maintenant_utc, attente_secondes: float = 2,
                 commande_claude: Sequence[str] = ("claude",)):
        inconnus = [a.id for a in agents if a.id not in AGENTS]
        if inconnus:
            raise ValueError(f"Agents inconnus de docs/architecture.md : {', '.join(inconnus)}")
        self.config = config
        self.agents = {a.id: a for a in agents}
        self.racine = racine
        self.horloge = horloge
        self.attente = attente_secondes
        self.commande_claude = list(commande_claude)
        self.arret = threading.Event()
        self._fils: list[threading.Thread] = []
        self._pauses: dict[str, float] = {}  # voie → instant (monotone) de reprise
        self._prochain_releve = None  # après un échec de relevé des statistiques
        self._verrou = None

    # --- Démarrage et arrêt ---

    def demarrer(self) -> None:
        self._verrouiller()
        lire(self.racine)  # refuse de démarrer si la charte n'est pas prête
        nettoyes = nettoyer_travail(self.config.dossier_temp, self._caches_a_garder())
        if nettoyes:
            journal.info("%s fichier(s) de travail orphelin(s) supprimé(s)", nettoyes)
        conn = connecter(self.config.chemin_base)
        repris = File(conn, self.horloge).reprendre_bloquees()
        conn.close()
        if repris:
            journal.warning("Tâches interrompues remises dans la file : %s", repris)

        for voie in ("normale", "rendu"):
            agents = [a for a in self.agents.values() if a.voie == voie]
            if agents:
                fil = threading.Thread(target=self._boucle, args=(voie, agents), name=f"voie-{voie}", daemon=True)
                fil.start()
                self._fils.append(fil)
        journal.info("Worker démarré : %s", ", ".join(sorted(self.agents)))

    def _caches_a_garder(self) -> set[str]:
        """Étapes déjà payées des reels à reprendre (agents/video.py) : gardées au redémarrage."""
        conn = connecter(self.config.chemin_base)
        try:
            return {f"video-{l['id']}" for l in conn.execute(
                "SELECT id FROM taches WHERE type = 'brief_video' AND statut IN ('à_faire', 'en_cours', 'échec')")}
        finally:
            conn.close()

    def arreter(self, delai: float | None = None) -> None:
        """Demande l'arrêt : chaque voie termine sa tâche en cours puis s'arrête."""
        self.arret.set()
        for fil in self._fils:
            fil.join(delai)
        if self._verrou:
            self._verrou.close()
            self._verrou = None

    def _verrouiller(self) -> None:
        chemin = self.config.chemin_base.with_name(self.config.chemin_base.name + ".worker.lock")
        chemin.parent.mkdir(parents=True, exist_ok=True)
        verrou = chemin.open("w")
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            verrou.close()
            raise WorkerDejaLance("Un worker tourne déjà sur cette base.") from None
        self._verrou = verrou

    # --- Boucle d'une voie ---

    def _boucle(self, voie: str, agents: list[Agent]) -> None:
        conn = connecter(self.config.chemin_base)
        file = File(conn, self.horloge)
        biblio = Bibliotheque.depuis_config(conn, self.config, self.horloge)
        couts = JournalCouts.depuis_config(conn, self.config, self.horloge)
        claude = MoteurClaude(self.config, couts, self.commande_claude)
        planificateur = Planificateur(conn, file, self.horloge) if voie == "normale" else None
        creneaux = Creneaux.depuis_config(self.config, conn, self.horloge)
        pause_humaine = Pause(conn, self.horloge)
        prochaine_planification = 0.0
        try:
            while not self.arret.is_set():
                if planificateur and time.monotonic() >= prochaine_planification:
                    try:
                        for tache_id in planificateur.verifier():
                            journal.info("Planificateur : tâche %s lancée", tache_id)
                        if self.config.publication_automatique:
                            for post in Publications(file, biblio, self.horloge).a_echeance(self.horloge()):
                                file.creer(DemandePublication(post_id=post.id), parent_id=post.id)
                                journal.info("Post %s : horaire atteint, publication demandée", post.id)
                        envoyees = Notifications(self.config, conn, self.horloge).verifier(
                            Purge.depuis_config(biblio, self.config).etat_disque().alerte)
                        for titre in envoyees:
                            journal.info("Notification envoyée : %s", titre)
                        self._relever_statistiques(file, biblio)
                        for tache_id in creneaux.reevaluer(file):
                            journal.info("Tâche %s : ne peut plus attendre la nuit, lancée en journée", tache_id)
                        # La mesure est un appel : la nuit, elle obéit à la règle de la fenêtre comme le reste.
                        if creneaux.autorises() and not pause_humaine.etat() and \
                                Quota(conn, self.horloge).a_actualiser(AGE_MAX_MESURE_QUOTA):
                            with dossier_de_travail(self.config.dossier_temp) as dossier:
                                claude.actualiser_quota(dossier)
                    except Exception:
                        journal.exception("Planificateur, publications ou mesure du quota en erreur")
                    prochaine_planification = time.monotonic() + 60
                pause = self._pauses.get(voie, 0) - time.monotonic()
                if pause > 0:
                    self.arret.wait(min(pause, 60))
                    continue
                try:
                    en_pause = pause_humaine.etat() is not None
                except Exception:
                    journal.exception("Voie %s : lecture de la pause impossible", voie)
                    en_pause = False
                if en_pause:  # pause décidée par l'humain : aucune tâche prise
                    self.arret.wait(min(self.attente * 5, 30))
                    continue
                try:
                    tache = file.prendre([a.id for a in agents], voie, creneaux.autorises())
                except Exception:
                    journal.exception("Voie %s : lecture de la file impossible", voie)
                    tache = None
                if tache is None:
                    self.arret.wait(self.attente)
                    continue
                self._traiter(tache, file, biblio, couts, claude)
        finally:
            conn.close()

    def _relever_statistiques(self, file: File, biblio: Bibliotheque) -> None:
        """Toutes les 12 h, si le jeton Instagram est réglé : statistiques des derniers posts (pour l'analyste)."""
        if not (self.config.instagram_jeton and self.config.instagram_compte_id):
            return
        statistiques = Statistiques(file, Publications(file, biblio, self.horloge), self.horloge)
        dernier = statistiques.dernier_releve_api()
        if dernier and self.horloge() - dernier < INTERVALLE_RELEVE_STATISTIQUES:
            return
        if self._prochain_releve and self.horloge() < self._prochain_releve:
            return
        client = ClientInstagram(self.config.instagram_api_url, self.config.instagram_compte_id,
                                 self.config.instagram_jeton)
        try:
            rapport = statistiques.relever_api(client)
        except (ErreurDefinitive, ErreurTemporaire) as erreur:
            self._prochain_releve = self.horloge() + ATTENTE_APRES_ECHEC_RELEVE
            journal.warning("Relevé des statistiques Instagram impossible : %s (nouvel essai dans %s h)", erreur,
                            int(ATTENTE_APRES_ECHEC_RELEVE.total_seconds() // 3600))
            return
        self._prochain_releve = None
        journal.info("Statistiques Instagram relevées : %s posts, %s de l'agence", rapport.lignes,
                     len(rapport.rattachees))

    def _traiter(self, tache: Tache, file: File, biblio: Bibliotheque, couts: JournalCouts,
                 claude: MoteurClaude) -> None:
        agent = self.agents[tache.agent]
        journal.info("Tâche %s (%s) prise par %s, tentative %s", tache.id, tache.type, agent.id, tache.tentatives)
        try:
            with dossier_de_travail(self.config.dossier_temp) as dossier:
                statut = agent.executer(tache, Outils(file, biblio, couts, dossier, claude, self.config))
            file.changer_statut(tache.id, statut)
            journal.info("Tâche %s : %s", tache.id, statut)
        except ErreurTemporaire as erreur:
            file.reporter(tache.id, str(erreur))
            self._pauses[tache.voie] = time.monotonic() + erreur.reessayer_dans.total_seconds()
            journal.warning("Tâche %s reportée, voie %s en pause %s : %s",
                            tache.id, tache.voie, erreur.reessayer_dans, erreur)
        except ERREURS_DEFINITIVES as erreur:
            journal.error("Tâche %s en échec : %s", tache.id, erreur)
            file.changer_statut(tache.id, "échec", erreur=str(erreur))
        except Exception as erreur:
            journal.exception("Tâche %s : erreur", tache.id)
            resultat = file.echouer(tache.id, f"{type(erreur).__name__} : {erreur}")
            journal.info("Tâche %s : %s", tache.id, resultat.statut)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker de l'agence IA")
    parser.add_argument("--agent-factice", action="store_true", help="Circuit de test sans appel d'API")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    if args.agent_factice:
        from agents.factice import AgentFactice

        agents: list[Agent] = [AgentFactice()]
    else:
        from agents import AGENTS_ACTIFS

        agents = [classe() for classe in AGENTS_ACTIFS]

    worker = Worker(charger(), agents)
    for signe in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signe, lambda *_: worker.arret.set())
    worker.demarrer()
    worker.arret.wait()
    journal.info("Arrêt demandé : fin des tâches en cours")
    worker.arreter()


if __name__ == "__main__":
    main()
