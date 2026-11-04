# Guide rapide — Trier Mails v1.0

Trier Mails récupère les pièces jointes de vos boîtes mail et les range automatiquement sur votre
ordinateur, par type de document (factures, contrats, tickets de caisse…), puis par domaine et par année.
**Vos mails ne sont jamais modifiés ni supprimés sur les serveurs** : l'outil ne fait que les lire.

---

## 1. Installer (une seule fois)

1. Installez **Python 3.13** depuis https://www.python.org/downloads/ (cochez « Add Python to PATH »).
2. Créez un dossier, par exemple `C:\Outils\Organizer`, et téléchargez le projet dedans dans un
   sous-dossier nommé **`Programme`** :
   ```
   git clone https://github.com/Smooginozaure/Organizer.git "C:\Outils\Organizer\Programme"
   ```
   (ou bouton **Code → Download ZIP** sur GitHub, puis renommez le dossier extrait en `Programme`).
3. Installez les bibliothèques nécessaires :
   ```
   py -3 -m pip install -r "C:\Outils\Organizer\Programme\requirements.txt"
   ```
4. Copiez `Programme\Trier Mails.bat` un niveau au-dessus. Vous obtenez :
   ```
   C:\Outils\Organizer\
   ├── Trier Mails.bat     ← double-cliquez ici
   └── Programme\          ← tout le reste
   ```
   (Le `.bat` fonctionne aussi depuis le dossier `Programme`.)

## 2. Premier lancement

Double-cliquez sur **Trier Mails.bat**. L'outil vous guide en trois étapes :

| Étape | Ce qui se passe |
|---|---|
| **Emplacement** | L'archive est créée sur le **Bureau** (`Archive mail`) ou à l'endroit de votre choix. |
| **Boîtes mail** | Ajoutez vos adresses. iCloud : créez un **mot de passe pour app** sur https://account.apple.com. Le mot de passe est rangé dans le Gestionnaire d'identifiants Windows, jamais dans un fichier. |
| **Scan à blanc** | Inventaire des mails et pièces jointes, **sans rien télécharger** (2 à 5 minutes). |

> ⏱️ La **première récupération** dépend de votre connexion et du volume de vos boîtes :
> comptez **entre 30 minutes et 2 heures**. Les suivantes ne prennent que quelques minutes.

## 3. Le menu principal

| Touche | Menu | À quoi ça sert |
|---|---|---|
| **1** | Paramétrer les boîtes mail | ajouter, tester, choisir les dossiers récupérés |
| **2** | Scan à blanc | **rapide** (nouveaux mails) ou **complet** (une fois par mois) — obligatoire avant 3 |
| **3** | Récupération | **1** mail complet + pièces jointes, ou **2** pièces jointes uniquement |
| **4** | Tri et rangement | emplacement, règles, mots à exclure, aperçu, rangement, IA |
| **5** | Veille automatique | scan + récupération programmés (toutes les heures, chaque jour…) |
| **6** | Rechercher un document | par nom, objet, correspondant, année… |

Utilisation courante : **2 → rapide**, puis **3**. Ou activez la **veille (5)** et n'y pensez plus.

## 4. Où sont mes fichiers ?

```
Archive mail\
├── Factures\Énergie\Électricité - Gaz\2024\
├── Tickets de caisse\Achats\Courses\2025\
├── Contrats\Logement\Location\2023\
├── À classer\2024\                 ← documents non reconnus
├── À trier soi-même\2024\          ← documents que vous voulez trier vous-même
├── _Mails sauvegardés\<Boîte>\<Dossier>\<Année>\
└── _rapports\                      ← rapports de scan, journaux
```

Un fichier reçu plusieurs fois n'est conservé qu'une fois.

## 5. Améliorer le tri (menu 4)

- **Modifier les règles** (4 → 3) : ajoutez un mot-clé ou un expéditeur dans le Bloc-notes.
- **Mots à exclure du tri** (4 → 4) :
  - *Mots ignorés* : ne comptent plus pour le tri (ex. le nom de votre banque dans une signature) ;
  - *À trier soi-même* : le document est mis de côté, ni règles ni IA ;
  - *Confidentiels* : le document n'est jamais envoyé à une IA en ligne.
  - « Tester » montre où irait un document, sans rien modifier.
- **Aperçu** (4 → 5) puis **Appliquer** (4 → 6) pour re-ranger toute l'archive.
- **IA** (4 → 7) pour les documents restants, au choix :
  - un **agent IA en ligne** déjà installé (Claude Code, Gemini CLI, Codex) ;
  - une **IA locale hors ligne** (Ollama + Gemma, Qwen…) : rien ne sort de l'ordinateur ;
    il faut environ 10 à 12 Go de mémoire de carte graphique pour un modèle moyen.
  - Rien n'est déplacé sans votre validation ; **4 → 8** annule le dernier classement par IA.

## 6. Confidentialité

- Les documents liés à la **gendarmerie** ou à la **diplomatie** (adresses, mots-clés) ne sont
  **jamais** envoyés à un service en ligne ; seule l'IA locale peut les classer.
- Vous pouvez ajouter vos propres mots confidentiels (4 → 4 → 3) ; la liste intégrée ne peut pas être réduite.
- Vos réglages (`config.json`) et vos règles (`regles_classement.json`) restent sur votre ordinateur :
  ils ne sont pas publiés sur GitHub.

## 7. En cas de souci

| Problème | Solution |
|---|---|
| « Connexion refusée » (iCloud) | utilisez un **mot de passe pour app**, pas votre mot de passe Apple |
| Récupération grisée | lancez d'abord un **scan à blanc** (menu 2) |
| Un document est mal rangé | ajoutez un mot-clé (4 → 3) ou un mot ignoré (4 → 4), puis 4 → 6 |
| La fenêtre se ferme aussitôt | vérifiez que Python 3.13 est installé, puis relancez |
| Opération « en cours » | la veille travaille ; réessayez dans quelques minutes |
