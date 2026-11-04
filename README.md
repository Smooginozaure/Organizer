<img src="assets/icon.svg" width="64" height="64" alt="">

# Trier Mails — archivage et classement des pièces jointes de vos boîtes mail

**Version 1.0** · Windows · Python 3.13 · 👉 **[Guide rapide](GUIDE.md)** (installation et utilisation pas à pas)

Sauvegarde les mails et classe leurs pièces jointes **par type de document**, toutes boîtes confondues,
du plus général au plus précis : `Bureau\Archive mail\<Type>\<Domaine>\<Sous-domaine>\<Année>\`, par exemple
`Factures\Énergie\Électricité - Gaz\2024\` ou `Tickets de caisse\Achats\Courses\2025\`.
Les copies des mails restent par boîte : `_Mails sauvegardés\<Boîte>\<Dossier>\<Année>\`.
Un fichier identique reçu sur plusieurs boîtes n'est conservé qu'une fois.

**Les mails ne sont jamais modifiés, déplacés, marqués comme lus ni supprimés sur les serveurs** :
les dossiers sont ouverts en lecture seule (`EXAMINE`) et l'outil refuse, avant tout envoi,
chaque commande IMAP capable de modifier une boîte (`STORE`, `EXPUNGE`, `MOVE`, `COPY`, `DELETE`,
`CLOSE`, `SELECT`, `FETCH` sans `.PEEK`…). Voir `mailarchive/imap_client.py` et ses tests.

## Utilisation : double-cliquer sur `Trier Mails.bat`

**Installation** (détails dans le [guide](GUIDE.md)) : cloner ce dépôt dans un dossier `Programme`, installer
les dépendances (`py -3 -m pip install -r requirements.txt`), puis copier `Trier Mails.bat` à côté :

```
C:\Outils\Organizer\
├── Trier Mails.bat     ← double-cliquer ici
└── Programme\          ← ce dépôt (script, réglages, règles de tri, tests)
```

Le lanceur fonctionne aussi directement depuis le dossier du dépôt.

**Premier lancement** : l'outil propose de créer l'archive sur le **Bureau** (par défaut) ou dans un autre
emplacement de votre choix (Documents, disque externe, NAS…), modifiable ensuite dans le menu 4.

**Durée de la première récupération** : le téléchargement et l'enregistrement des pièces jointes dépendent à
la fois de la **connexion internet** et du **volume des boîtes mail** ; comptez en général **entre 30 minutes
et 2 heures**. Les récupérations suivantes (nouveaux mails uniquement) ne prennent que quelques minutes.

Une interface s'affiche dans l'invite de commande (tableaux, couleurs, barres de progression) :

1. **Paramétrer les boîtes mail** : ajouter une adresse (messagerie reconnue automatiquement : iCloud,
   Free, Gmail, Orange, La Poste, SFR, Yahoo, GMX ; sinon le serveur est demandé), tester les connexions,
   changer un mot de passe, **choisir les dossiers récupérés** (cases à cocher), retirer une boîte.
   Le message « Paramétrage effectué — connexion réussie » confirme l'enregistrement ; le mot de passe
   n'est conservé (Gestionnaire d'identifiants Windows) que si la connexion réussit.
   - iCloud : **mot de passe pour app** (https://account.apple.com → Connexion et sécurité).
   - Gmail / Yahoo : mot de passe d'application. Outlook/Hotmail : non pris en charge.
2. **Scan à blanc (obligatoire)**, sans rien télécharger, au choix :
   - **rapide** : n'analyse que les mails arrivés depuis le dernier scan (quelques secondes). Le serveur
     numérote les mails de chaque dossier dans l'ordre d'arrivée : seuls les numéros nouveaux sont lus, le
     reste de l'inventaire est repris tel quel ; les mails supprimés depuis sont aussi détectés. Un dossier
     nouveau ou renuméroté par le serveur est relu entièrement, automatiquement ;
   - **complet** : relit tous les mails de tous les dossiers (2 à 5 minutes), conseillé une fois par mois.
   Résumé à l'écran (nouveaux mails, reste à récupérer dans chaque mode, espace disque) et rapport HTML.
3. **Récupération** (verrouillée tant qu'il n'y a pas de scan) : **1** mail complet (.eml) + pièces jointes,
   ou **2** pièces jointes uniquement ; période au choix (tout, 12 derniers mois, depuis une date).
   Vérifie l'espace disque avant de commencer. Passer plus tard de 2 à 1 ne retélécharge que les mails.
4. **Tri et rangement** : emplacement de l'archive (Bureau, Documents, disque externe : déplacement
   sécurisé, index mis à jour), règles de tri (affichage, modification dans le Bloc-notes), aperçu puis
   application du rangement, tri assisté par Claude (export filtré) et annulation.
5. **Veille automatique** : scan + récupération programmés (toutes les heures, toutes les 6 h, chaque jour,
   chaque semaine) via le Planificateur de tâches Windows, sans fenêtre ; scan rapide à chaque passage et
   scan complet automatique si le dernier date de plus de 30 jours ; fonctionne sur batterie, rattrape
   un passage manqué, jamais deux opérations en même temps ; journal dans `_rapports\veille.log`.
6. **Rechercher un document** : par nom, objet du mail, correspondant, type, domaine ou année ; ouvre
   l'Explorateur sur le fichier.

## Réglages (`config.json`)

- `dossier_archive` : `{BUREAU}` = votre Bureau.
- `recuperation.roles` : dossiers récupérés (`réception`, `envoyés`, `brouillons`, `autre`, `corbeille`, `spam`).
- `recuperation.dossiers_inclus` / `dossiers_exclus` : ajouter ou retirer un dossier par son nom (ex. `"Commercial"`).
- `recuperation.sauvegarder_mails` : `false` pour ne garder que les pièces jointes.

## Classement (`regles_classement.json`)

Au premier lancement, `regles_classement.json` est créé à partir de `regles_classement.example.json` ;
il devient ensuite **le vôtre** (non publié, comme `config.json`) : vos expéditeurs et mots-clés personnels y restent.

Deux listes de règles : `types` (facture, ticket de caisse, contrat, attestation, relevé…) donne le
premier niveau ; `regles` (énergie, santé, logement, loisirs…) donne les niveaux suivants.
`python -m mailarchive reorganiser [--simulation]` re-range toute l'archive avec les règles à jour
(les décisions validées à la main sont conservées ; journal dans `_rapports`).

100 % local : mots-clés cherchés dans le nom du fichier, l'objet du mail, l'expéditeur et le texte des
PDF / Word / Excel. Sans règle reconnue : `Photos et vidéos`, `Agenda et contacts` ou `À classer`.
Les exécutables vont dans `_Quarantaine (ne pas ouvrir)` avec l'extension `.quarantaine`.
Chaque fichier identique (empreinte SHA-256) n'est stocké qu'une fois par compte.

## Mots à exclure du tri (menu 4 → 4)

Trois listes, modifiables depuis le menu (enregistrées dans `config.json`) :

| Liste | Effet |
|---|---|
| Mots ignorés | ne comptent jamais pour les règles (signature, pied de page, mot trompeur) |
| À trier soi-même | le document va dans `À trier soi-même\<Année>` : ni règles, ni IA |
| Confidentiels | s'ajoutent à la liste intégrée : jamais envoyé à une IA en ligne |

Mots reconnus sans tenir compte des majuscules ni des accents, en mot entier. « Tester » montre le rangement obtenu
pour un nom de fichier ou un objet ; « Appliquer » revérifie la confidentialité, montre l'aperçu puis re-range.
Les tris validés à la main (ou par IA) sont conservés ; un document marqué confidentiel le reste.

## Classer les documents restants avec une IA (menu 4 → 7)

Le scan et la récupération n'utilisent aucune IA. Pour les documents que les règles n'ont pas su classer,
deux options sont proposées, avec pour chacune la durée estimée et la mémoire de carte graphique nécessaire :

| | 1 · Agent IA en ligne | 2 · IA locale, hors ligne |
|---|---|---|
| Exemples | Claude Code, Gemini CLI, Codex (ChatGPT) | Ollama + un modèle (Gemma 4, Qwen…) |
| Prérequis | agent **déjà installé** + abonnement | Ollama + un modèle **déjà installés** |
| Internet | oui | **non**, seul `127.0.0.1` est contacté |
| Documents | ni confidentiels, ni échanges entre particuliers, ni scans | **tous**, confidentiels compris ; scans si le modèle lit vraiment les images (testé) |
| Carte graphique | aucune | petit modèle 4–6 Go · moyen 10–12 Go · grand 20–24 Go |
| Durée | quelques minutes / 100 documents | rapide ≈ 1 s/document · précis ≈ 10 s/document (gemma4, carte 12 Go) |

L'outil n'installe ni agent ni modèle ; il donne seulement des liens pour se renseigner :
[Claude Code](https://code.claude.com/docs/en/setup) · [Gemini CLI](https://github.com/google-gemini/gemini-cli) ·
[Codex](https://github.com/openai/codex) · [Ollama](https://ollama.com/download) · [modèles Ollama](https://ollama.com/library).
Dans les deux cas : aperçu, validation, journal et annulation (menu 4 → 8). L'IA locale peut aussi classer
automatiquement les nouveaux documents à chaque passage de la veille.

## Documents confidentiels (gendarmerie, diplomatie) — règle impérative

Tout document lié à la gendarmerie ou à la diplomatie est **trié uniquement en local** et n'est
**jamais transmis à un service externe** (Claude/Anthropic compris). Le filtre (`mailarchive/confidentiel.py`)
s'applique sur l'ordinateur, au mail entier : adresses de tous les participants (expéditeur,
destinataires, copies), objet, noms des pièces jointes et texte des documents. Il est volontairement
large ; `config.json → "confidentiel"` peut ajouter des domaines ou des mots, jamais en retirer.
Les confidentiels non reconnus vont dans `À classer - confidentiel`. Avant chaque export pour le tri
assisté, le filtre est réappliqué à toute l'archive, et tout document échangé **uniquement entre
particuliers** (Gmail, iCloud, Free…, vos adresses comprises) est exclu par prudence.

## Tri assisté du dossier « À classer » (dans une conversation avec Claude)

1. `python -m mailarchive tri-export` : crée `Archive mail\_tri_assiste\a_trier.json`, le **seul** fichier
   que Claude lit. En sont exclus les documents confidentiels (et leurs doublons) et ceux dont le texte
   n'est pas lisible localement (scans, images, mails joints), dont le contenu ne peut pas être vérifié.
2. Claude propose un thème par document (`decisions.json`), que vous validez.
3. `python -m mailarchive tri-appliquer decisions.json` : déplace les fichiers (refuse tout confidentiel),
   journal dans `_tri_assiste\journal_*.csv` ; `tri-annuler <journal>` pour revenir en arrière.

## Technique

Python 3.13 (`keyring`, `pypdf`, `rich`). Index local : `Archive mail\_index\archive.sqlite`.
Ligne de commande : `python -m mailarchive [menu|compte <email>|scan|recuperer|reorganiser|veille|tri-export|tri-appliquer|tri-annuler]`.
Tests : `python -m unittest discover -s tests -t .`
