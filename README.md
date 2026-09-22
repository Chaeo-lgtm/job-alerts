# Bot d'alerte emploi — Paris (Indeed, LinkedIn, HelloWork)

Surveille en continu les nouvelles offres à Paris sur Indeed, LinkedIn et
HelloWork, en priorité sur le domaine culturel/créatif (médiation
culturelle, communication, événementiel, audiovisuel/photo/graphisme),
avec vente/accueil/service client/restauration en filet de sécurité. Envoie
une notification Discord dès qu'une offre inédite apparaît. Tourne
gratuitement sur GitHub Actions, sans jamais avoir besoin de laisser un
ordinateur allumé, et sans se connecter à aucun compte perso (recherche sur
les pages publiques uniquement).

## Ce qu'il fait

- Interroge 8 familles de recherche à Paris (modifiable en tête du fichier
  `job_bot.py`, variable `CATEGORIES`) :
  - **Priorité (⭐, domaine culturel/CV)** : médiation culturelle,
    communication/réseaux sociaux, événementiel, audiovisuel/photo/graphisme
  - **Filet de sécurité** : vente, accueil/réception, service client,
    restauration
- Tourne toutes les 15 minutes, 24h/24, via GitHub Actions.
- Ne notifie que les offres jamais vues auparavant (le premier lancement
  sert juste à établir une base de référence, sans spam).
- Envoie chaque nouvelle offre par message Discord (titre, entreprise,
  catégorie, lien direct) — les offres du domaine culturel ressortent en
  doré avec une étoile ⭐, pour les repérer d'un coup d'œil.
- S'auto-signale si un site casse (structure de page changée) au lieu de
  tomber en silence sans prévenir.

## Mise en place (une seule fois)

### 1. Créer le webhook Discord

1. Sur Discord, dans le serveur (ou crée-en un juste pour toi, en quelques
   secondes) où tu veux recevoir les alertes : ouvre les **Paramètres du
   salon** (clic droit sur le nom du salon, ou l'icône ⚙️) → **Intégrations**
   → **Webhooks** → **Nouveau webhook**.
2. Donne-lui un nom (ex. "Alertes emploi"), puis clique sur **Copier l'URL
   du webhook**. Cette URL ressemble à
   `https://discord.com/api/webhooks/123456789/abcDEF...` — c'est ton
   `DISCORD_WEBHOOK_URL`. Personne d'autre n'en a besoin, garde-la privée
   (elle permet d'écrire dans ce salon).

Si tu n'as pas de serveur Discord perso : **Ajouter un serveur** → **Créer
le mien** → un modèle "Pour moi et mes amis" suffit, ça prend 30 secondes et
tu peux le laisser vide à part ce salon d'alertes.

### 2. Créer le repo GitHub

1. Sur [github.com/new](https://github.com/new), crée un nouveau repo vide
   (public ou privé, peu importe), par exemple `job-alerts-bot`.
2. Depuis ce dossier, en local :

   ```bash
   git init
   git add .
   git commit -m "Initial commit — bot d'alerte emploi"
   git branch -M main
   git remote add origin https://github.com/<ton-compte>/job-alerts-bot.git
   git push -u origin main
   ```

### 3. Ajouter le secret Discord au repo

Sur GitHub : **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, et ajoute :

- `DISCORD_WEBHOOK_URL` → l'URL copiée à l'étape 1

### 4. Autoriser le bot à committer ses données

Le workflow a besoin d'écrire dans le repo (fichier `data/seen_jobs.json`,
qui garde la mémoire des offres déjà vues). Sur GitHub : **Settings** →
**Actions** → **General** → section **Workflow permissions**, sélectionne
**"Read and write permissions"**, puis **Save**.

### 5. Premier lancement

Va dans l'onglet **Actions** du repo, clique sur le workflow **"Job
alerts"**, puis **"Run workflow"** pour le lancer manuellement une première
fois (au lieu d'attendre le prochain créneau de 15 min). Tu dois recevoir un
message Discord "Bot d'alerte emploi initialisé" avec le nombre d'offres
enregistrées. À partir de là, seules les offres nouvelles seront notifiées,
automatiquement, toutes les 15 minutes.

## Ajuster les critères plus tard

Tout se règle en tête de `job_bot.py` :

- `CATEGORIES` — ajoute/retire des familles ou des mots-clés
- `PRIORITY_CATEGORIES` — quelles familles ressortent en doré ⭐
- `HOURS_OLD` — n'examine que les offres postées dans les X dernières heures
- `LOCATION` / `HELLOWORK_LOCATION` — élargir au-delà de Paris si besoin

Après une modif, il suffit de `git commit` + `git push` : le prochain run
utilisera automatiquement la nouvelle config.

## Point de vigilance connu

- **Indeed et LinkedIn** passent par la librairie
  [`python-jobspy`](https://github.com/speedyapply/JobSpy), activement
  maintenue — la partie la plus fiable du bot.
- **HelloWork** n'a pas de librairie équivalente : le scraper lit en
  priorité les données structurées (JSON-LD) que le site expose pour son
  référencement Google, ce qui est plus robuste qu'un scraping HTML brut —
  mais si HelloWork change son site en profondeur, ce module pourra avoir
  besoin d'un ajustement. Le bot t'enverra alors un message Discord
  d'avertissement automatique plutôt que de s'arrêter silencieusement.
- **LinkedIn** reste, par nature, le site qui bloque le plus agressivement
  les requêtes automatisées (même sur des pages publiques). Il est possible
  qu'il tombe en échec de temps en temps sans que ce soit un vrai problème —
  Indeed et HelloWork continueront de fonctionner en parallèle.
