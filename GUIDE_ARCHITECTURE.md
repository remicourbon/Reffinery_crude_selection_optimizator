# Guide d'architecture — crude-delivery-optimizer

Ce document explique le code de A à Z : comment les fichiers s'articulent, le
flux de données d'une requête, puis chaque module en détail. Objectif : que tu
puisses modifier le projet seul et défendre chaque choix en entretien.

---

## 1. Vue d'ensemble en une phrase

> L'utilisateur choisit une raffinerie, une date d'arrivée et un volume.
> Le code price chaque brut candidat à son propre tenor de départ, calcule son
> coût rendu (CIF), puis un solveur linéaire (LP) choisit le mélange de bruts
> qui maximise la marge de raffinage. L'app Streamlit n'affiche que des
> résultats — toute la logique vit dans `core/`.

---

## 2. Le principe d'architecture le plus important

**La logique métier (`core/`) ne connaît pas l'interface (`app/`).**

`core/` est du Python pur : on peut l'utiliser sans Streamlit, et c'est ce que
font les tests. `app/app.py` ne fait qu'appeler `core/` et dessiner le
résultat. Cette séparation est *la* raison pour laquelle 81 tests unitaires
couvrent tout ce qui compte : les chiffres affichés sont calculés par des
fonctions testées, pas par du code d'affichage.

```
  ┌─────────────────────────────────────────────────────┐
  │                    app/app.py                        │   ← INTERFACE
  │   (Streamlit : widgets, tableaux, graphiques)        │     (non testé)
  │   N'IMPORTE QUE depuis core/, ne calcule rien        │
  └───────────────────────┬─────────────────────────────┘
                          │ appelle
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │                      core/                           │   ← LOGIQUE MÉTIER
  │   Python pur, sans Streamlit, entièrement testé      │     (81 tests)
  └─────────────────────────────────────────────────────┘
                          │ lit
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │                      data/                           │   ← DONNÉES
  │   6 fichiers YAML (assays, raffineries, routes...)   │     (statiques)
  └─────────────────────────────────────────────────────┘
```

**Règle d'or pour modifier :** si tu changes un *calcul*, tu touches `core/` et
tu mets à jour son test. Si tu changes un *affichage*, tu touches `app/app.py`
seulement. Si tu changes une *hypothèse chiffrée*, tu touches `data/`.

---

## 3. Carte des fichiers

```
crude-delivery-optimizer/
│
├── run.py                  Lanceur : règle sys.path + lance Streamlit
├── requirements.txt        Dépendances
│
├── data/                   ← LES HYPOTHÈSES (modifiables sans coder)
│   ├── crudes.yaml             7 bruts : assays, benchmark, différentiel
│   ├── refineries.yaml         4 raffineries : ports, configs, capacités
│   ├── routes.yaml             28 routes : distances + Worldscale flat
│   ├── vessels.yaml            3 bateaux : taille, vitesse, WS typique
│   ├── conversion_units.yaml   4 unités : FCC, HCU, coker, reformer
│   └── product_markets.yaml    4 marchés : cracks produits régionaux
│
├── core/                   ← LA LOGIQUE (Python pur, testé)
│   ├── data_models.py          Charge + valide les YAML → dataclasses
│   ├── market.py               Courbes forward, prix à n'importe quel tenor
│   ├── freight.py              Coût de fret, durée de voyage, choix du bateau
│   ├── refinery.py             Bilan matière, GPW, upgrading des unités
│   ├── lp_model.py             LE solveur d'optimisation (PuLP)
│   └── decision.py             L'ORCHESTRATEUR : assemble tout
│
├── app/
│   └── app.py                  Interface Streamlit, 5 pages
│
└── tests/                  ← 81 tests unitaires, un fichier par module core
```

---

## 4. Le graphe de dépendances (qui appelle qui)

Lis ce schéma de bas en haut : les modules du bas ne dépendent de rien, ceux
du haut orchestrent.

```
                          app/app.py
                              │
                              │ appelle surtout evaluate()
                              ▼
                       core/decision.py          ← L'ORCHESTRATEUR
                    (assemble tous les autres)
                       │     │      │     │
          ┌────────────┘     │      │     └──────────────┐
          ▼                  ▼      ▼                     ▼
   core/freight.py   core/market.py  core/refinery.py   core/lp_model.py
          │                  │              │                   │
          │                  │              │ utilise           │ utilise
          │                  │              ▼                   ▼
          │                  │        (gpw, uplift)       (gpw, uplift de
          │                  │                             refinery.py)
          └──────────────────┴──────────────┴───────────────────┘
                              │
                              │ tous lisent les dataclasses de
                              ▼
                       core/data_models.py        ← LA FONDATION
                    (Crude, Refinery, Route...)
                              │
                              │ charge
                              ▼
                          data/*.yaml
```

Points clés à retenir :
- **`data_models.py` ne dépend de personne** (sauf PyYAML). C'est la fondation.
- **`lp_model.py` et `refinery.py` ne s'importent pas l'un l'autre dans le
  mauvais sens** : `lp_model` importe `gpw` et `uplift` depuis `refinery`,
  jamais l'inverse.
- **`decision.py` est le seul qui connaît tout le monde.** C'est volontaire :
  c'est le chef d'orchestre, les autres sont des musiciens qui s'ignorent.
- **`app.py` n'appelle presque que `decision.evaluate()`** plus quelques
  fonctions d'affichage de `refinery`/`market`.

---

## 5. Le flux de données d'une requête (le cœur du projet)

Voici ce qui se passe quand tu cliques dans l'app. Suis les numéros.

```
UTILISATEUR : "Livre 650 kbbl à Dangote le 1er septembre"
     │
     ▼
[1] app.py lit les widgets → refinery_key="dangote", arrival=1er sept,
    volume=650, et construit les courbes forward (market.benchmark_curves)
     │
     ▼
[2] app.py appelle  decision.evaluate(ds, "dangote", ..., curves)
     │
     ▼  ┌──────────────────── DANS evaluate() ────────────────────────┐
     │  │                                                              │
     │  │  [3] POUR CHAQUE BRUT, price_option() :                      │
     │  │      ┌─ freight.best_quote() : quel bateau ? quelle durée ?  │
     │  │      │   → 26 jours de mer pour le WTI                        │
     │  │      ├─ date de départ = arrivée − 26 jours = 6 août         │
     │  │      ├─ market.crude_fob() : prix FOB AU 6 AOÛT              │
     │  │      │   (← la "structure" entre ici : on price au départ)   │
     │  │      ├─ freight.financing_usd_bbl() : intérêts sur 26 jours  │
     │  │      └─ CIF = FOB + fret + financement + assurance + pertes  │
     │  │                                                              │
     │  │  [4] market.product_prices() : prix des produits À L'ARRIVÉE │
     │  │                                                              │
     │  │  [5] lp_model.optimise_basket() : avec tous les CIF et les   │
     │  │      prix produits, quel MÉLANGE de bruts maximise la marge ?│
     │  │      → résout le LP sous contraintes (CDU, unités, soufre)   │
     │  │                                                              │
     │  └──────────────────────────────────────────────────────────────┘
     │
     ▼
[6] evaluate() renvoie un objet Decision (panier optimal, options,
    shadow prices, config scalée...)
     │
     ▼
[7] app.py affiche : tableau des options, panier optimal, waterfall,
    shadow prices. AUCUN calcul ici, juste de l'affichage.
```

**La phrase à retenir pour l'entretien :** *« chaque brut est pricé à sa propre
date de départ — donc à son propre point sur la courbe forward — pour que la
comparaison soit honnête à date d'arrivée égale. La structure de la courbe
entre par là. »*

---

## 6. Les modules un par un

### 6.1 `core/data_models.py` — la fondation

**Rôle :** transformer les YAML en objets Python validés et immuables.

**Concepts clés :**

- **Les dataclasses `frozen=True`** : `Crude`, `Refinery`, `RefineryConfig`,
  `Route`, `Vessel`, `ConversionUnit`, `ProductMarket`. "Frozen" = immuable :
  une fois créé, on ne peut plus modifier un `Crude`. Ça empêche un bug où le
  LP corromprait silencieusement les données.

- **La validation dans `__post_init__`** : chaque dataclass se vérifie à la
  construction. Exemple dans `Crude` : les rendements doivent sommer à 1.0,
  l'API doit être entre 10 et 60, etc. Si un YAML est cassé, l'erreur dit
  *exactement* quel fichier et quelle clé.

- **`Dataset`** : le conteneur qui regroupe tout, plus
  `validate_referential_integrity()` qui vérifie les liens entre fichiers
  (ex : chaque brut a-t-il une route vers chaque raffinerie ?).

- **`load_dataset(data_dir)`** : LE point d'entrée. Charge les 6 YAML, construit
  les dataclasses, valide tout. C'est la seule fonction que le reste du code
  appelle.

**Schéma de la donnée la plus importante — le `Crude` :**

```
Crude (bonny_light)
├── name, api, sulfur_pct
├── benchmark = "brent"      ← sur quelle courbe il se price
├── diff_usd_bbl = 0.50      ← son différentiel vs le benchmark
├── fob_port = "bonny"       ← d'où il part (→ clé vers routes.yaml)
├── yields = {lpg: 0.03, naphtha: 0.22, gasoline: 0.0, ...}  ← somme = 1.0
└── diesel_sulfur_pct = 0.08 ← le seul soufre que le LP contraint
```

**Pour modifier :** ajouter un brut = ajouter un bloc dans `crudes.yaml` + les
routes correspondantes dans `routes.yaml`. La validation te dira si tu oublies
une route. **Aucun code Python à toucher.**

---

### 6.2 `core/market.py` — le temps et les prix

**Rôle :** donner le prix de n'importe quel brut ou produit à n'importe quelle
date. C'est le module qui porte toute la dimension temporelle.

**Concepts clés :**

- **`Curve`** : une courbe forward = des piliers (date, prix) avec
  interpolation linéaire entre eux. Sa méthode `.price(date)` donne le prix à
  une date donnée ; `.front` donne le prix spot (tenor 0).

- **`parametric_curve(spot, slope)`** : construit une courbe simple = spot +
  pente constante en $/mois. C'est le *fallback* quand yfinance n'est pas
  disponible. Pente > 0 = contango, pente < 0 = backwardation.

- **`fetch_benchmark_curve()`** : va chercher les vrais contrats futures via
  yfinance. Si ça échoue (pas de réseau), lève une erreur → le fallback prend
  le relais.

- **`benchmark_curves()`** : orchestre les deux — tente le live, retombe sur le
  paramétrique. C'est ce que l'app appelle.

- **`crude_fob(crude, curves, date)`** = prix du benchmark à `date` +
  différentiel du brut.

- **`product_prices(market, curves, date)`** = prix du benchmark à `date` +
  crack de chaque coupe. Renvoie `{cut: prix}`, exactement ce que `refinery.py`
  attend.

**Schéma :**

```
                    benchmark_curves(anchor, spots, slopes, use_live)
                              │
                live? ───────┼─────── pas de live / échec
                    │                        │
                    ▼                        ▼
          fetch_benchmark_curve()    parametric_curve()
          (yfinance, vrais futures)  (spot + pente $/mois)
                    │                        │
                    └────────────┬───────────┘
                                 ▼
                          {"brent": Curve, "wti": Curve}
                                 │
                  ┌──────────────┴───────────────┐
                  ▼                               ▼
          crude_fob(crude, ., date)    product_prices(market, ., date)
          = benchmark(date) + diff     = benchmark(date) + crack par coupe
```

**Pour modifier :** changer le mapping yfinance (ticker Brent/WTI) ou la
convention de fallback se fait ici. Ajouter un 3e benchmark (ex. Dubai) =
ajouter au dict `YF_TICKERS` et à `BENCHMARKS` dans `data_models.py`.

---

### 6.3 `core/freight.py` — le transport

**Rôle :** combien coûte d'acheminer un baril, combien de temps ça prend, quel
bateau utiliser.

**Concepts clés :**

- **`tonnes_per_bbl(api)`** : convertit via la densité. Important : le
  Worldscale est en $/tonne, mais on raisonne en $/baril, et un baril de brut
  lourd pèse plus → coûte plus cher au baril.

- **`voyage_days(route, vessel)`** = distance / vitesse + jours de port.

- **`financing_usd_bbl(fob, days)`** = intérêts ACT/360 sur le capital
  immobilisé pendant le voyage.

- **`quote(route, vessel, volume, api, ws%)`** : le coût de fret d'un bateau
  donné. Gère le *dead freight* : un bateau à moitié vide coûte quand même son
  prix entier.

- **`best_quote(...)`** : LA règle déterministe — choisit le bateau le moins
  cher au $/baril parmi ceux qui (a) sont acceptés au port et (b) peuvent
  porter le volume. **Ce choix se fait AVANT le LP**, pour garder le problème
  linéaire (sinon ce serait un MILP, deux crans plus complexe).

**Schéma de `best_quote` :**

```
best_quote(route, volume=650, port accepte max VLCC)
     │
     ▼
  candidats = bateaux qui (cargo ≥ 650) ET (cargo ≤ taille max du port)
     │
     ▼
  pour chaque candidat : quote() → $/bbl (dead freight inclus)
     │
     ▼
  garder le MOINS CHER au $/bbl
```

**Pour modifier :** changer le taux de financement par défaut, l'assurance, les
pertes = constantes en haut du fichier (`DEFAULT_FINANCING_RATE`, etc.).
Ajouter une classe de bateau = un bloc dans `vessels.yaml`.

---

### 6.4 `core/refinery.py` — la transformation physique

**Rôle :** que devient un panier de bruts une fois raffiné, et combien ça vaut.

**Concepts clés :**

- **`gpw(crude, prices)`** : Gross Product Worth = somme (rendement × prix) sur
  les coupes. C'est la valeur "brute" d'un baril, sans upgrading.

- **`material_balance(basket, crudes)`** : volumes de chaque coupe produits par
  un panier. Invariant testé : la masse se conserve.

- **`blend_diesel_sulfur(basket, crudes)`** : soufre moyen pondéré du pool
  diesel. C'est ce que le LP contraint.

- **`uplift(unit, prices)`** : valeur créée en upgradant 1 baril dans une unité
  = valeur du slate de sortie − prix du feed. Peut être négatif (alors l'unité
  reste à l'arrêt).

- **`apply_upgrading(basket, crudes, config, units, prices)`** : déroule la
  raffinerie complète. Ordre : distillation (CDU) → conversion (FCC/HCU sur le
  VGO) → coker (sur le résidu) → reformer (sur le naphtha). Renvoie le bilan
  matière final + la valeur totale.

- **`utilisation(...)`** : taux de charge de chaque unité (pour les jauges).

**Le schéma de l'upgrading — à connaître par cœur :**

```
  PANIER DE BRUTS
       │
       ▼  material_balance()
  ┌─────────────────────────────────────────────────┐
  │  CDU (distillation) produit les coupes :          │
  │  lpg | naphtha | kero | diesel | vgo | residue    │
  └───────┬──────────────────────┬─────────┬─────────┘
          │                      │         │
          │ naphtha              │ vgo     │ residue
          ▼                      ▼         ▼
     ┌─────────┐          ┌──────────┐  ┌───────┐
     │ REFORMER│          │ FCC / HCU│  │ COKER │
     │ →gasoline│         │ →gasoline│  │→diesel│
     │          │         │  +diesel │  │ +vgo  │
     └─────────┘          └──────────┘  └───────┘
          │                      │         │
          └──────────┬───────────┴─────────┘
                     ▼
            BILAN MATIÈRE FINAL + valeur totale
            (chaque unité tourne SI son uplift > 0)
```

**Le point subtil :** chaque unité ne tourne que si son uplift est positif (le
modèle ne détruit jamais de valeur). Et l'upgrading est *séquentiel et
acyclique* : le reformer traite le naphtha du CDU + celui produit par FCC/coker,
mais rien ne reboucle.

**Pour modifier :** changer les rendements d'une unité = `conversion_units.yaml`.
Ajouter une nouvelle voie d'upgrading (ex. alkylation) = un bloc YAML + une
étape dans `apply_upgrading` + une variable dans le LP (voir 6.5).

---

### 6.5 `core/lp_model.py` — le solveur (LE morceau d'entretien)

**Rôle :** étant donné les CIF de chaque brut et les prix produits, trouver le
MÉLANGE de bruts qui maximise la marge, sous contraintes.

**La formulation mathématique (à savoir réécrire au tableau) :**

```
VARIABLES DE DÉCISION
  x_i      ≥ 0   volume acheté du brut i        (kb/j)
  u_conv   ≥ 0   volume upgradé en conversion   (kb/j)
  u_coker  ≥ 0   volume upgradé au coker
  u_reformer ≥ 0 volume upgradé au reformer

OBJECTIF (maximiser, en k$/jour)
  max  Σ x_i × (GPW_i − CIF_i)              ← marge straight-run par brut
       + u_conv     × uplift_conv           ← + gains d'upgrading
       + u_coker    × uplift_coker
       + u_reformer × uplift_reformer

CONTRAINTES
  Σ x_i ≤ CAP_CDU                            "cdu"
  Σ x_i = V_cible          (optionnel)       "total_volume"
  u_conv     ≤ feed VGO disponible           "conv_feed"
  u_conv     ≤ CAP_conv                      "conv_capacity"
  u_coker    ≤ feed résidu disponible        "coker_feed"
  u_coker    ≤ CAP_coker                     "coker_capacity"
  u_reformer ≤ feed naphtha disponible       "reformer_feed"
  u_reformer ≤ CAP_reformer                  "reformer_capacity"
  Σ x_i × y_diesel_i × (S_i − S_max) ≤ 0     "sulfur"  ← linéarisée !
```

**Les 3 idées à retenir :**

1. **Sans contraintes, le LP achèterait 100% du meilleur brut.** Toute
   l'intelligence est dans les contraintes. C'est la réponse à "pourquoi un LP
   et pas un simple classement ?"

2. **La contrainte soufre est linéarisée.** La vraie contrainte est un ratio
   (soufre moyen du pool ≤ spec), non-linéaire. On multiplie les deux côtés par
   le dénominateur (positif) pour obtenir
   `Σ x_i × y_diesel × (S_i − S_max) ≤ 0`. Chaque brut doux apporte du "crédit
   soufre", chaque brut soufré en consomme. **C'est LA question piège des LP de
   blend en entretien.**

3. **Les shadow prices (duaux).** Après résolution, chaque contrainte a un prix
   dual = combien la marge gagnerait si on relâchait la contrainte d'une unité.
   Le dual soufre = la prime sweet/sour de cette raffinerie. Le dual
   `conv_capacity` = la valeur d'un baril de conversion en plus.

**La traduction en PuLP (le code suit la maths ligne à ligne) :**

```python
prob = pulp.LpProblem("crude_basket", pulp.LpMaximize)
x = {k: pulp.LpVariable(f"x_{k}", lowBound=0) for k in candidates}
objective = pulp.lpSum(x[k] * (gpw(c, prices) - cif[k]) ...)
# ... + variables d'upgrading
prob += objective
prob += (pulp.lpSum(x.values()) <= config.cdu_capacity_kbd, "cdu")
# ... les autres contraintes
prob.solve(pulp.PULP_CBC_CMD(msg=0))
```

**Pour modifier :** ajouter une contrainte (ex. plafond sur le naphtha vendu) =
ajouter une ligne `prob += (..., "nom")`. Ajouter une unité d'upgrading = une
variable `u_xxx` + ses deux contraintes (feed + capacité) + son terme dans
l'objectif.

---

### 6.6 `core/decision.py` — l'orchestrateur

**Rôle :** assembler freight + market + lp_model en une seule fonction qui
répond à la question complète.

**Concepts clés :**

- **`price_option(ds, crude, refinery, arrival, volume, curves)`** : price UN
  brut livré à UNE raffinerie à UNE date. C'est ici que se fait le voyage dans
  le temps : départ = arrivée − durée, FOB au départ, financement sur la durée,
  CIF total. Renvoie un `CrudeOption`.

- **`_scaled_config(config, volume)`** : convertit les capacités kb/j en
  capacités sur la fenêtre de traitement de la cargaison. C'est la subtilité
  "kbbl (stock) vs kb/j (débit)" : une cargaison de 650 kbbl chez une raffinerie
  de 200 kb/j = ~3 jours de traitement, donc on scale les capacités à 650.

- **`evaluate(...)`** : LE point d'entrée de tout le projet. Boucle sur tous les
  bruts (price_option), récupère les prix produits à l'arrivée, lance le LP, et
  renvoie un `Decision` complet. Accepte un `config_override` pour le bac à
  sable Marseille (config construite en live au lieu du YAML).

**Schéma de `evaluate` :**

```
evaluate(ds, "dangote", config, arrival, volume, curves)
     │
     ├─ POUR CHAQUE BRUT : price_option() → CrudeOption (CIF)
     │      (bruts non livrables = exclus avec raison)
     │
     ├─ product_prices() à l'arrivée
     │
     ├─ _scaled_config() : capacités kb/j → fenêtre cargaison
     │
     ├─ optimise_basket() : le LP tourne
     │
     └─ renvoie Decision { options, panier optimal, shadow prices,
                           scaled_config, prix produits, exclusions }
```

**Pour modifier :** ajouter une composante de coût au CIF (ex. taxe portuaire)
se fait dans `price_option`. Changer la règle de scaling se fait dans
`_scaled_config`.

---

### 6.7 `app/app.py` — l'interface

**Rôle :** dessiner. Lit les widgets de la sidebar, appelle `evaluate()` une
fois, et affiche le résultat sur 5 pages.

**Structure du fichier :**

```
[en-tête]   bootstrap sys.path, imports, load_dataset (caché par @st.cache)
[sidebar]   widgets : raffinerie, config (ou sliders Marseille), date,
            volume, WS%, courbes
[appel]     decision = evaluate(...)   ← une seule fois, partagé par les pages
[helpers]   margin_table(), stacked_cif_chart(), cif_waterfall(),
            freight_frame()  + palettes de couleurs
[pages]     if page == "Markets": ...
            elif page == "Freight": ...
            elif page == "Refinery": ...
            elif page == "Reference data": ...
            else: (Simulator)
```

**Les 5 pages :**

| Page | Ce qu'elle montre | Appelle surtout |
|------|-------------------|-----------------|
| Simulator | Options triées par marge, panier optimal, shadow prices | `margin_table()`, `decision` |
| Markets | Courbes forward, FOB par tenor, prix produits | `market`, `decision.options` |
| Freight | Waterfall CIF, structure, comparaison | `freight_frame()` |
| Refinery | Bilan matière, caractéristiques des unités | `apply_upgrading`, `utilisation` |
| Reference data | Toutes les données brutes (6 onglets) | lit `ds` directement |

**Le point d'architecture clé de l'app :** `evaluate()` est appelé **une seule
fois** en haut, et toutes les pages réutilisent le même objet `decision`. Si
l'évaluation échoue (ex. cargaison trop grosse pour le port), `decision` vaut
`None` et chaque page affiche l'erreur proprement.

**Pour modifier :** ajouter une page = un nouveau `elif page == "...":` + le nom
dans la liste `st.sidebar.radio(...)`. Changer un graphique = toucher le helper
correspondant. **Ne jamais mettre de calcul ici** — si tu te surprends à
calculer une marge dans `app.py`, c'est que ça devrait être dans `core/`.

---

## 7. Recettes pratiques de modification

### Ajouter un brut
1. Bloc dans `crudes.yaml` (assay, benchmark, diff, port FOB).
2. Routes vers les 4 raffineries dans `routes.yaml` (la validation liste les
   manquantes).
3. Rien d'autre. Lance `pytest` pour vérifier l'intégrité.

### Ajouter une raffinerie
1. Bloc dans `refineries.yaml` (port, max vessel, marché produit, config(s)).
2. Routes depuis tous les bruts vers son port.
3. Si nouveau marché produit : bloc dans `product_markets.yaml`.

### Ajouter une unité d'upgrading
1. Bloc dans `conversion_units.yaml` (feed + slate).
2. Champ capacité dans `RefineryConfig` (`data_models.py`).
3. Étape dans `apply_upgrading` (`refinery.py`).
4. Variable + contraintes dans `optimise_basket` (`lp_model.py`).
5. Scaling dans `_scaled_config` (`decision.py`).
6. Tests.

### Changer une hypothèse de prix/coût
- Cracks produits → `product_markets.yaml`
- Financement / assurance / pertes → constantes en haut de `freight.py`
- Différentiels de bruts → `crudes.yaml`

### Ajouter une contrainte au LP
- Une ligne `prob += (expression <= limite, "nom")` dans `optimise_basket`.
- Le dual sera automatiquement exposé dans `shadow_prices`.

---

## 8. Comment tester tes modifications

```bash
python -m pytest -q          # les 81 tests (doit rester vert)
python run.py                # lance l'app pour vérifier visuellement
```

**Règle :** toute modif d'un calcul dans `core/` doit s'accompagner d'une mise à
jour (ou d'un ajout) de test. Les tests contiennent des valeurs calculées à la
main dans les commentaires — c'est ta documentation vivante de "ce que le code
devrait produire".

---

## 9. Le chemin de lecture conseillé (si tu reprends le code à froid)

1. `data/crudes.yaml` — comprends la donnée d'entrée.
2. `core/data_models.py` — comment elle devient un objet Python.
3. `core/refinery.py` (`gpw`, `apply_upgrading`) — la transformation physique.
4. `core/lp_model.py` — l'optimisation (le cœur intellectuel).
5. `core/decision.py` (`evaluate`) — comment tout s'assemble.
6. `app/app.py` — comment c'est affiché.

C'est l'ordre du plus simple au plus orchestré, et c'est aussi l'ordre dans
lequel tu peux le raconter en entretien.
```
