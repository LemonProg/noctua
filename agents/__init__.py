"""Agents lancés par le worker (python -m noyau.worker)."""

from agents.analyste import AgentAnalyste
from agents.erreurs import AgentErreurs
from agents.orchestrateur import AgentOrchestrateur
from agents.publication import AgentPublication
from agents.redacteur import AgentRedacteur
from agents.relecteur import AgentRelecteur
from agents.veille import AgentVeille
from agents.video import AgentVideo
from agents.visuel import AgentVisuel

AGENTS_ACTIFS = (AgentOrchestrateur, AgentRedacteur, AgentRelecteur, AgentVisuel, AgentErreurs, AgentVeille,
                 AgentPublication, AgentAnalyste, AgentVideo)
