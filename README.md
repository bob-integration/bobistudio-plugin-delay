# Délai — ligne à retard N→N de Bobi.Studio

*[English version](README.en.md)*

Retarde X entrées vidéo et/ou audio vers X sorties correspondantes. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `plugins/delay/`.

## Ce que ça fait

X entrées retardées vers X sorties correspondantes, de 1 à 16 canaux. Chaque canal est
indépendant : entrée V1 → sortie V1, A1 → sortie A1. Trois essences au choix au déploiement —
vidéo seul (défaut), vidéo + audio, ou audio seul.

Sert à recaler des sources de latences différentes avant un mélange, ou à rattraper le retard
cumulé d'une chaîne avant un point de comparaison.

Le format d'entrée est repris tel quel : le délai ne convertit rien, et un retard de D images est
**exact** — ni perte ni duplication.

## Couplé ou découplé

Couplé (défaut), une seule consigne règle l'image et le son : le son suit l'image, converti par la
période vidéo mesurée. Découplé, deux consignes indépendantes — l'image en images, le son en
millisecondes.

Une conséquence à connaître, voulue par le découplage : un enregistreur placé derrière ce délai
sort décalé de la **différence** des deux consignes, puisqu'il apparie l'image *k* avec le son du
même instant. L'ISO en portera la trace.

## Régler à chaud

Le délai se règle sans redéployer : un curseur global qui pousse la même valeur partout, ou un
curseur par canal. Le bouton **canaux liés** aligne tous les canaux sur la même valeur, pour garder
un groupe synchrone. Les valeurs réglées en direct sont **persistées** : elles survivent au
redémarrage comme au redéploiement du conteneur.

Changer l'essence ou le nombre de canaux, en revanche, est structurel et redéploie.

## Consigne et délai mesuré

`GET :8082/state` et `/channels` publient, par canal, le délai **réglé** (`delay_frames`) et le
délai **réellement mesuré** (`measured_ms`, `realized_frames`), plus le fps du canal. Comparer les
deux désigne un canal qui décroche, ou qui n'a pas la source attendue ; sans cette paire, le
délai appliqué n'est pas vérifiable.

Une perte de source déclenche la réouverture automatique du flux MXL ; aucune action opérateur.

## Mode tranche

À délai nul, le plugin suit la source bande par bande au fil de son arrivée : aucune latence
propre ajoutée. À délai d'une image ou plus, il republie l'image retardée **re-tranchée** en
bandes — l'aval garde le bénéfice sous-trame à travers l'étage, même si retarder impose bien
d'attendre l'image entière.

Conditions : source progressive, et une hauteur de bande qui divise la hauteur d'image. Sinon le
plugin le dit dans son journal et retombe sur l'image entière. Au choix, désactivé par défaut.

## Coût en mémoire

Chaque canal tamponne `max_delay_frames` images, ou l'équivalent en audio, **en mémoire**. Un
délai maximum élevé sur beaucoup de canaux en haute résolution consomme de la RAM en proportion,
et il la consomme même si le délai courant est nul : le tampon est dimensionné par la borne, non
par la consigne.

L'audio circule par paquets dont la taille vient du réglage du cœur (Réglages → MXL, 1 ms par
défaut) ; le plugin la lit, il ne la décide pas.

## Macros et contrôle externe

Le retard global, le retard d'un canal, le retard audio découplé et le couplage image/son sont
publiés aux macros et aux déclencheurs, avec un `param_tree` pour les consignes continues. Le
pilotage depuis un pupitre passe par là.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
