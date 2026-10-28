# Hitman: Codename 47 — Port New 3DS

Portage non officiel du moteur de **Hitman: Codename 47** (2000, IO Interactive) pour **Nintendo 3DS / New 3DS**.

---

## ⚠️ Avertissement légal / Legal Disclaimer

* Ce dépôt contient **uniquement du code source open-source et des outils de conversion**.
* **Aucun fichier ni ressource sous copyright n'est inclus** (pas de textures, modèles 3D, sons, musiques ou scripts du jeu).
* Vous **devez posséder une copie légale** de *Hitman: Codename 47* sur PC (version GOG, Steam ou CD originale) pour extraire et convertir vous-même vos données.
* **Ne partagez jamais et ne publiez jamais le fichier généré `data.c47p`** (sur GitHub ou ailleurs) : il contient l'intégralité des ressources propriétaires du jeu original.

---

## 📌 État actuel du projet

Le projet est actuellement un **moteur d'exploration 3D de scènes** (visualiseur de niveaux complets) :
* ✅ Rendu 3D natif accéléré par le GPU PICA200 via **Citro3D** avec shaders personnalisés.
* ✅ Support des **13 missions principales** du jeu d'origine (Hong Kong, Colombie, Budapest, Rotterdam, Asile...).
* ✅ **Barre de chargement** au lancement de chaque niveau avec pré-chargement complet de la carte en mémoire.
* ✅ **Caméra Libre** fluide pour survoler et inspecter l'intégralité des décors du jeu sans contrainte.
* ✅ Mode caméra 3ᵉ personne avec déplacement de l'Agent 47, collisions et détection du sol.
* ✅ Gestion intelligente du cache de textures LRU (élimination des clignotements).
* ⏳ *En cours de développement* : moteur d'animations squelettiques (.ANM / .MOR), ragdoll physique, IA des PNJ, logique de jeu et lecture audio PCM.

---

## 🛠️ Comment convertir vos fichiers de jeu

### Prérequis sur votre PC
1. **Python 3.10+** installé.
2. La bibliothèque **Pillow** :
   ```bash
   pip install pillow
   ```
3. L'outil **tex3ds** (inclus avec devkitPro dans `C:\devkitPro\tools\bin\tex3ds.exe`).
4. Votre dossier de jeu PC (ex : `C:\GOG Games\Hitman Codename 47`).

---

### Méthode 1 : Avec l'outil graphique (Recommandé)

Lancez l'interface tout-en-un à la racine du projet :
```bash
python c47tool.py
```

1. **Onglet 1 · Convertir** :
   * Cliquez sur **« Parcourir… »** à côté de *Dossier du jeu* et sélectionnez votre dossier d'installation (*Hitman Codename 47*).
   * Choisissez un dossier de sortie (par exemple `convertion`).
   * Cliquez sur **« ▶ Lancer la conversion »**.
   * Les 13 missions, modèles 3D et textures sont convertis au format optimisé 3DS.

2. **Onglet 2 · Empaqueter** :
   * Cliquez sur **« ▶ Créer data.c47p »**.
   * Cela fusionne tous les fichiers convertis dans une seule archive ultra-rapide sans compression `data.c47p` (~280 Mo).
   * Cliquez sur **« Copier sur la carte SD… »** et sélectionnez la racine de votre carte SD.

---

### Méthode 2 : En ligne de commande

```bash
# 1. Conversion des données
python tools/c47conv.py "C:\chemin\vers\Hitman Codename 47" build/phase2 --all

# 2. Création de l'archive pour la 3DS
python tools/c47pack.py build/phase2 data.c47p
```

---

## 💾 Installation sur la console 3DS

Copiez les fichiers sur votre carte SD selon l'arborescence suivante :

```text
SD:/
└── 3ds/
    └── hitman47/
        ├── hitman47.3dsx    <-- L'application homebrew
        ├── hitman47.smdh    <-- Icône et métadonnées
        └── data.c47p        <-- Le paquet de données converti
```

Lancez ensuite **Hitman 47** depuis le **Homebrew Launcher**.

---

## 🎮 Contrôles en jeu

### Menu principal
| Bouton | Action |
|---|---|
| **Croix Haut / Bas** | Sélectionner une mission |
| **Croix Gauche / Droite** | Sauter de 10 missions |
| **A** | Lancer la mission sélectionnée |
| **START** | Quitter l'application |

### Écran de chargement
Au lancement de la mission, l'écran du bas affiche une **barre de chargement en temps réel** (`[####----] XX%`) pendant que l'ensemble des modèles de la carte sont pré-chargés en VRAM pour garantir un affichage fluide sans saccades.

---

### En jeu : Mode Caméra Libre (Mode Exploration)
Ce mode permet de visiter librement l'ensemble des décors et pièces de chaque mission :

| Bouton | Action |
|---|---|
| **Circle Pad** | Déplacement horizontal (Avant / Arrière / Pas chassés) |
| **C-Stick** ou **Croix directionnelle** | Orientation de la vue (Haut / Bas / Gauche / Droite) |
| **B (maintenu)** | Vitesse rapide (Turbo) |
| **X** ou **ZR** | Prendre de la hauteur (monter) |
| **Y** ou **ZL** | Descendre |
| **L / R** | Réduire / Augmenter la distance d'affichage (brouillard dynamique) |
| **A** | Activer / Désactiver le brouillard |
| **SELECT** | Basculer en mode 3ᵉ personne (Agent 47) |
| **START** | Retourner au menu de sélection des missions |

---

### En jeu : Mode 3ᵉ personne (Agent 47)
| Bouton | Action |
|---|---|
| **Circle Pad** | Déplacer l'Agent 47 (avec gestion de collision et hauteur du sol) |
| **B (maintenu)** | Courir |
| **C-Stick** ou **Croix directionnelle** | Faire pivoter la caméra en orbite autour de 47 |
| **SELECT** | Revenir en Caméra Libre |
| **START** | Quitter vers le menu |

---

## 🏗️ Compilation depuis les sources

Pour recompiler le projet vous-même :
* Installez **devkitPro** avec les paquets 3DS (`devkitARM`, `libctru`, `citro3d`, `picasso`, `tex3ds`).
* Ouvrez un terminal devkitPro / MSYS2 à la racine du projet et tapez :
  ```bash
  make
  ```
* Cela génère `hitman47.3dsx` et `hitman47.elf`.

---

## 📜 Licence & Crédits
* *Hitman: Codename 47* est une marque déposée et propriété intellectuelle de **IO Interactive**.
* Le code du moteur de portage et des outils est distribué à des fins éducatives et de recherche.
