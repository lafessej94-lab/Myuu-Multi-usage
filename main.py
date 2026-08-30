# @title 🖥 Sae Code
API_ID    = 0                              # @param {type: "integer"}
API_HASH  = ""   # @param {type: "string"}
BOT_TOKEN = ""  # @param {type: "string"}
USER_ID   = 0                           # @param {type: "integer"}
DUMP_ID   = 0                                     # @param {type: "integer"} — dump channel/chat ID for autoforward
CC_API_KEY = ""  # @param {type: "string"}
FC_API_KEY = ""  # @param {type: "string"}
SEEDR_USERNAME = ""  # @param {type: "string"}
SEEDR_PASSWORD = ""  # @param {type: "string"}
SEEDR_PROXY = ""  # @param {type: "string"}

MAX_RESTARTS = 50  # @param {type: "integer"} — nombre max de redémarrages auto en cas de crash

import subprocess, time, json, shutil, os, sys, re
from IPython.display import clear_output
from threading import Thread

Working = True
print("💖 Myuu࣪ ☾ Bot — Launcher")
print("─" * 40)


def Loading():
    white = 37
    black = 0
    while Working:
        print("\r" + "░" * white + "▒▒" + "▓" * black + "▒▒" + "░" * white, end="")
        black = (black + 2) % 75
        white = (white - 1) if white != 0 else 37
        time.sleep(2)
    clear_output()


_Thread = Thread(target=Loading, name="Prepare", args=())
_Thread.start()

if os.path.exists("/content/sample_data"):
    shutil.rmtree("/content/sample_data")

# ⚠️ Repo Myuu-Multi-usage (plus le fork zilong, abandonné)
clone_result = subprocess.run(
    "git clone https://github.com/lafessej94-lab/Myuu-Multi-usage.git /content/zilong",
    shell=True,
)
install_result = subprocess.run("apt update -qq && apt install -y -qq ffmpeg aria2", shell=True)
pip_result = subprocess.run(
    "pip3 install -q -r /content/zilong/requirements.txt", shell=True
)

Working = False

# Vérifs post-install : on arrête tout de suite si une étape critique a échoué,
# plutôt que de laisser le bot planter plus loin sans message clair.
if clone_result.returncode != 0:
    print("\n❌ Échec du git clone — vérifie l'URL du repo ou ta connexion réseau.")
    sys.exit(1)
if shutil.which("ffmpeg") is None or shutil.which("aria2c") is None:
    print("\n❌ ffmpeg ou aria2 n'a pas pu s'installer — relance la cellule, ou vérifie les mirrors apt de Colab.")
    sys.exit(1)
if pip_result.returncode != 0:
    print("\n❌ Échec de l'installation des dépendances Python (requirements.txt).")
    sys.exit(1)

credentials = {
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "BOT_TOKEN": BOT_TOKEN,
    "USER_ID": USER_ID,
    "DUMP_ID": DUMP_ID,
    "CC_API_KEY": CC_API_KEY,
    "FC_API_KEY": FC_API_KEY,
    "SEEDR_USERNAME": SEEDR_USERNAME,
    "SEEDR_PASSWORD": SEEDR_PASSWORD,
    "SEEDR_PROXY": SEEDR_PROXY,
}
with open("/content/zilong/credentials.json", "w") as f:
    json.dump(credentials, f)

if os.path.exists("/content/zilong/my_bot.session"):
    os.remove("/content/zilong/my_bot.session")

os.makedirs("/content/zilong/data", exist_ok=True)
log_path = "/content/zilong/data/zilong.log"
print(f"\rLive logs will stream below. File log: {log_path}")

flood_re = re.compile(r"(?:FLOOD_WAIT_SECONDS=(\d+)|A wait of (\d+) seconds is required)")
restart_count = 0

print("\rStarting Bot....")
while restart_count < MAX_RESTARTS:
    start = time.time()
    proc = subprocess.Popen(
        ["python3", "-m", "colab_leecher"],
        cwd="/content/zilong",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    captured = []
    try:
        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                print(line, end="")
                sys.stdout.flush()
                captured.append(line)
    finally:
        if proc.stdout:
            proc.stdout.close()

    return_code = proc.wait()

    if return_code == 0:
        print(f"\n✅ Bot arrêté proprement (code {return_code}).")
        break

    # Reset le compteur si le bot a tourné longtemps avant de crasher
    if time.time() - start > 300:
        restart_count = 0
    restart_count += 1

    # Cherche un FloodWait dans les dernières lignes pour attendre le bon délai
    wait = min(5 * restart_count, 30)
    for line in reversed(captured):
        m = flood_re.search(line)
        if m:
            wait = int(m.group(1) or m.group(2)) + 5
            break

    print(f"\n⚠️ Bot arrêté avec code {return_code}. Redémarrage dans {wait}s [{restart_count}/{MAX_RESTARTS}]")
    time.sleep(wait)
else:
    print("\n❌ Trop de redémarrages, arrêt du script.")
