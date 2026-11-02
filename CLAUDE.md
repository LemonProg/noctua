# CLAUDE.md — Règles du projet

Consignes pour Claude Code quand il travaille sur ce dépôt.

## Avant chaque session

Lis `README.md` puis `docs/architecture.md`.

## Méthode de travail

- **Plan d'abord** pour tout nouveau chantier ; une étape à la fois, testée avant la suivante.
- **Commit git** à la fin de chaque étape qui fonctionne, avec un message clair.
- **En cas de doute, demande** plutôt que de deviner, surtout pour les API des réseaux sociaux et les coûts.

## Règles techniques

- Le projet vise un VPS de **2 cœurs et 4 Go de RAM**. Choisis toujours la solution la plus légère. Justifie toute nouvelle dépendance.
- **Un seul rendu (image ou vidéo) à la fois**, via la file de tâches.
- Les rushs et contenus sont stockés sur le disque du serveur. Passe toujours par le module `stockage`, jamais par des chemins en dur.
- **Conservation** : fichiers de travail supprimés après traitement ; contenus publiés supprimés 7 jours après publication ; rushs et contenus refusés ou « à ne pas utiliser » en corbeille, supprimés sous 24 h.
- **Choix du modèle** : chaque tâche confiée par l'orchestrateur précise `modele` et `effort` ; la génération de contenu se fait toujours avec Opus 5.5.
- **Claude passe par l'abonnement**, jamais par une clé API Anthropic : n'ajoute pas `ANTHROPIC_API_KEY` et retire-la de l'environnement de la commande `claude`.
- **Secrets uniquement dans `.env`**, jamais dans le code ni dans git. Maintiens `.env.example` à jour.
- Les identifiants des agents et des messages sont ceux de `docs/architecture.md`. Ne les renomme pas.
- Chaque agent lit `charte.md` et `lecons.md` avant d'agir.
- Tout contenu rejeté est analysé par `agent_erreurs` avant suppression. Les leçons n'entrent dans `lecons.md` qu'après validation humaine.

## Ce qui est interdit

- Publier via un robot navigateur (Playwright, sessions persistantes, identifiants stockés).
- Publier ou envoyer quoi que ce soit sans validation humaine.

## Langue

Documentation, interface et messages destinés à l'utilisateur en français.
