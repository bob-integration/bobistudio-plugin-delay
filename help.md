# Délai (ligne à retard)

Ligne à retard multi-canaux : X entrées vidéo et/ou audio retardées de D images vers X sorties correspondantes. Utile pour synchroniser des sources de latences différentes, ou rattraper le retard d'une chaîne de traitement.

## Configurer

Au déploiement : nombre de canaux (1 à 16), essence (**vidéo seul**, **vidéo + audio** ou **audio seul**), délai maximum et délai initial. Le délai s'exprime en **images vidéo** (en **millisecondes** pour l'audio seul).

## Régler à chaud

Le retard se règle **en direct** depuis **Traitements → Délai** : délai global ou par canal, avec un mode « canaux liés ». Les réglages faits en live sont **persistés** — ils survivent au redémarrage et au redéploiement du conteneur.

## Câblage

Chaque canal a son entrée et sa sortie propres (V1→sortie V1, A1→sortie A1…). Câbler depuis la page **Câbles**. Le format d'entrée est repris automatiquement.

## Mode tranche (latence réduite)

Le délai peut publier ses sorties vidéo **bande par bande** (mode tranche MXL) : à délai nul il suit la source au fil de son arrivée, et à délai ≥ 1 image il republie les images retardées par bandes — l'aval en mode tranche conserve le bénéfice sous-trame à travers l'étage délai.

- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement historique strictement inchangé).
- Conditions : source progressive ; en entrelacé, le chemin classique s'applique.

## Notes

- Le délai maximum dépend de la RAM disponible (tampon d'images en mémoire)
- La résolution de sortie est identique à l'entrée
- Un délai vidéo de D images est **exact** (ni perte ni duplication)
