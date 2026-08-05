# Délai (ligne à retard)

Ligne à retard multi-canaux : X entrées vidéo et/ou audio retardées de D images vers X sorties
correspondantes. Utile pour synchroniser des sources de latences différentes (ex. recaler une
source rapide sur la plus lente d'un mixeur), ou rattraper le retard cumulé d'une chaîne de
traitement (encodeur, réseau, étage supplémentaire) avant un point de comparaison ou de mixage.

## Configurer au déploiement

| Réglage | Description |
|---|---|
| Nombre de canaux | 1 à 16 |
| Essence | **Vidéo seul** (défaut), **Vidéo + audio**, ou **Audio seul** — fixe si chaque canal a une entrée/sortie vidéo, audio, ou les deux |
| Délai max | Borne haute réglable en direct ensuite. En **images vidéo** pour les modes avec vidéo, en **millisecondes** en audio seul. Dimensionne le tampon mémoire (voir Notes) |
| Délai initial | Valeur de départ, dans la même unité |

Changer l'essence ou le nombre de canaux redéploie le conteneur (structurel) ; le délai lui-même
se règle ensuite à chaud sans redéploiement.

## Régler à chaud

Depuis **Traitements → Délai** :

- **Délai global** : un seul curseur pousse la même valeur sur tous les canaux.
- **Délai par canal** : chaque canal a son propre curseur.
- **Canaux liés** (bouton bascule) : quand actif, régler un canal (ou le délai global) aligne
  **tous** les canaux sur la même valeur — pratique pour garder un groupe synchrone. Désactivé,
  chaque canal garde son délai indépendant.
- Les réglages faits en live sont **persistés** : ils survivent au redémarrage et au redéploiement
  du conteneur (le délai courant n'est pas perdu à un redémarrage du conteneur).

## Câblage

Chaque canal a son entrée et sa sortie propres (V1 → sortie V1, A1 → sortie A1…), indépendantes
des autres canaux. Câbler depuis la page **Câbles**. Le format d'entrée (résolution, cadence,
chroma) est repris automatiquement — pas de format à configurer.

## Mode tranche (latence réduite)

Le délai peut publier ses sorties vidéo **bande par bande** (mode tranche MXL) :

- **À délai nul** : il suit la source au fil de son arrivée (bande par bande dès qu'elle arrive) —
  aucune latence propre ajoutée.
- **À délai ≥ 1 image** : il republie l'image déjà complète et retardée, **re-tranchée** en
  bandes — l'aval en mode tranche conserve le bénéfice sous-trame à travers l'étage délai, même si
  le délai lui-même impose forcément d'attendre l'image entière avant de la retarder.
- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement
  historique strictement inchangé).
- Conditions : source progressive ; en entrelacé, le chemin classique s'applique. Le plugin
  signale dans le journal quand une sortie n'est pas éligible (résolution sans diviseur de bande
  raisonnable) et retombe sur le chemin image entière.

## Diagnostic

- `GET :8082/state` (et `/channels`) publient, par canal : le délai réglé (`delay_frames`), le
  délai **réellement mesuré** (`measured_ms`, `realized_frames`) et le fps du canal — comparer
  réglé vs mesuré permet de détecter un canal qui décroche ou qui n'a pas la source attendue.
- Une perte de source déclenche une réouverture automatique du flux MXL (SIGBUS intercepté) —
  aucune action opérateur nécessaire.
- Le niveau de log se règle à chaud (`POST :8082/log_level`) sans redéployer.

## Notes

- Le délai maximum dépend de la RAM disponible : chaque canal tamponne `max_delay_frames` images
  (ou ms d'audio) en mémoire — un délai max élevé sur beaucoup de canaux en haute résolution
  consomme de la RAM en proportion. Le réglage n'est pas gratuit même si le délai courant est nul.
- La résolution/le format de sortie sont identiques à l'entrée (le délai ne convertit rien).
- Un délai vidéo de D images est **exact** (ni perte ni duplication d'image).
