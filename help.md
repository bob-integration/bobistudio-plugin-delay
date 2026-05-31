# Délai vidéo

Introduit un délai fixe sur un shm vidéo (buffer de frames en mémoire). Utile pour synchroniser des sources de latences différentes.

## Configurer

Au déploiement : choisir le délai en frames ou millisecondes. Le délai est **fixe** (pas de changement à chaud sans redéploiement).

## Câblage

Entrée : 1 shm vidéo. Sortie : 1 shm vidéo retardé. Câbler depuis la page **Câbles**.

## Notes

- Le délai maximum dépend de la RAM disponible (ring-buffer en mémoire)
- La résolution de sortie est identique à l'entrée
