"""
Real_ESRGAN.py — upscale des covers AniList (souvent ~360p / 460x650) via Real-ESRGAN
(modèle générique x4, marche bien sur de l'art anime).

Installation requise (dans requirements.txt de myuu) :
    pip install git+https://github.com/ai-forever/Real-ESRGAN.git torch torchvision pillow requests

Sur Colab, le premier appel télécharge les poids du modèle (~65 Mo) dans ./weights/,
puis les réutilise ensuite. Le modèle reste chargé en mémoire entre les appels
(_MODEL global) pour éviter de le recharger à chaque /anime.
"""

import io
import os

import requests
import torch
from PIL import Image
from RealESRGAN import RealESRGAN

_MODEL = None
_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
_SCALE = 4  # x4 : un cover AniList 460x650 -> ~1840x2600, au-dessus de la 4K en hauteur
_WEIGHTS_DIR = "weights"


def _get_model(scale: int = _SCALE) -> RealESRGAN:
    """Charge le modèle une seule fois et le garde en mémoire pour les appels suivants."""
    global _MODEL
    if _MODEL is None or _MODEL.scale != scale:
        os.makedirs(_WEIGHTS_DIR, exist_ok=True)
        _MODEL = RealESRGAN(_DEVICE, scale=scale)
        _MODEL.load_weights(f"{_WEIGHTS_DIR}/RealESRGAN_x{scale}.pth", download=True)
    return _MODEL


def upscale_from_url(image_url: str, output_path: str, scale: int = _SCALE) -> str:
    """
    Télécharge une image (ex: coverImage.extraLarge d'AniList) et l'upscale.
    Retourne output_path une fois le fichier écrit.
    """
    resp = requests.get(image_url, timeout=30)
    resp.raise_for_status()
    image = Image.open(io.BytesIO(resp.content)).convert("RGB")

    model = _get_model(scale)
    sr_image = model.predict(image)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    sr_image.save(output_path, quality=95)
    return output_path


def upscale_from_path(input_path: str, output_path: str, scale: int = _SCALE) -> str:
    """Même chose que upscale_from_url mais depuis un fichier déjà téléchargé en local."""
    image = Image.open(input_path).convert("RGB")

    model = _get_model(scale)
    sr_image = model.predict(image)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    sr_image.save(output_path, quality=95)
    return output_path


# --- Exemple d'intégration dans Aniliste.py (à adapter à ton handler /anime) ---
#
# from Real_ESRGAN import upscale_from_url
#
# cover_url = media["coverImage"]["extraLarge"]
# upscaled_path = f"/tmp/{media['id']}_cover.jpg"
# upscale_from_url(cover_url, upscaled_path)
#
# await message.reply_photo(photo=upscaled_path, caption=caption_text)
