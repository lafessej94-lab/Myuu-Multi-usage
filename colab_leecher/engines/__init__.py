"""
colab_leecher/engines/

Moteurs de traitement autonomes (aucun handler Pyrogram, aucune notion de
callback bot) : conversion/hardsub CloudConvert et FreeConvert, FFmpeg
local (conversion + boîte à outils vidéo), Seedr, et l'upscale d'image
Real-ESRGAN. Utilisés par colab_leecher/utility/handler/ (la couche
d'orchestration des tâches) et par services/Aniliste.py.

house_style.py et smart_rename.py restent à la racine de colab_leecher/
(config partagée entre moteurs, pas des moteurs en soi).
"""
