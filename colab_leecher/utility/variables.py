from time import time
from datetime import datetime
from pyrogram.types import Message


class BOT:
    SOURCE = []
    TASK = None
    class Setting:
        stream_upload = "Media"
        auto_forward = "Off"
        convert_video = "Yes"
        convert_quality = "Low"
        caption = "Monospace"
        split_video = "Split Videos"
        cc_engine_mode = "Balanced CPU"
        cc_quality_profile = "Balanced"
        cc_resize = "720p"
        cc_target_size = "100 MB"
        prefix = ""
        suffix = ""
        thumbnail = False

    class Options:
        stream_upload = True
        auto_forward = False
        convert_video = True
        convert_quality = False
        is_split = True
        caption = "code"
        video_out = "mp4"
        cc_engine_mode = "balanced"
        cc_quality_profile = "balanced"
        cc_resize = 720
        cc_target_size_mb = 100
        custom_name = ""
        zip_pswd = ""
        unzip_pswd = ""
        dump_ids = []  # liste de chat_id (int) — canaux dump, gérés via /add et /dumps
        cc_api_keys = []  # liste de clés CloudConvert, gérées via /addcc et /apikeys
        fc_api_keys = []  # liste de clés FreeConvert, gérées via /addfc et /apikeys
        allowed_users = []  # liste de user_id (int) autorisés à utiliser le bot, gérée via /adduser et /users

    class Mode:
        mode = "leech"
        type = "normal"
        ytdl = False

    class State:
        started = False
        task_going = False
        prefix = False
        suffix = False


class YTDL:
    header = ""
    speed = ""
    percentage = 0.0
    eta = ""
    done = ""
    left = ""


class Transfer:
    down_bytes = [0, 0]
    up_bytes = [0, 0]
    total_down_size = 0
    sent_file = []
    sent_file_names = []


class TaskError:
    state = False
    text = ""


class BotTimes:
    current_time = time()
    start_time = datetime.now()
    task_start = datetime.now()


class Paths:
    BASE_DIR = "/content/zilong"
    DATA_DIR = f"{BASE_DIR}/data"
    LOG_PATH = f"{DATA_DIR}/zilong.log"
    WORK_PATH = f"{BASE_DIR}/BOT_WORK"
    THMB_PATH = f"{BASE_DIR}/colab_leecher/Thumbnail.jpg"
    VIDEO_FRAME = f"{WORK_PATH}/video_frame.jpg"
    HERO_IMAGE = f"{WORK_PATH}/Hero.jpg"
    DEFAULT_HERO = f"{BASE_DIR}/custom_thmb.jpg"
    MOUNTED_DRIVE = "/content/drive"

    down_path = f"{WORK_PATH}/Downloads"
    temp_dirleech_path = f"{WORK_PATH}/dir_leech_temp"
    mirror_dir = f"{MOUNTED_DRIVE}/MyDrive/Colab Leecher Uploads"
    temp_zpath = f"{WORK_PATH}/Leeched_Files"
    temp_unzip_path = f"{WORK_PATH}/Unzipped_Files"
    temp_cc_path = f"{WORK_PATH}/CloudConvert_Files"
    temp_files_dir = f"{WORK_PATH}/leech_temp"
    thumbnail_ytdl = f"{WORK_PATH}/ytdl_thumbnails"
    access_token = "/content/token.pickle"


class Messages:
    caution_msg = "\n\n<i>💖 When I'm Doin This, Do Something Else ! <b>Because, Time Is Precious ✨</b></i>"
    download_name = ""
    task_msg = ""
    status_head = f"<b>📥 DOWNLOADING » </b>\n"
    dump_task = ""
    src_link = ""
    link_p = ""


class MSG:
    sent_msg = Message(id=1)
    status_msg = Message(id=2)


class Aria2c:
    link_info = False
    pic_dwn_url = "https://picsum.photos/900/600"


class Gdrive:
    service = None


# ═════════════════════════════════════════════════════════════
# ProcessTracker — tracks ALL subprocesses so /cancel kills them
# ═════════════════════════════════════════════════════════════

class ProcessTracker:
    """
    Global registry of running subprocess PIDs.
    When cancelTask() fires, it kills EVERY tracked process — not just
    the asyncio task. This is why the old cancel was broken: BOT.TASK.cancel()
    only cancelled the Python coroutine, but aria2c/ffmpeg/yt-dlp kept running
    as orphan processes eating CPU and disk.

    Usage:
        proc = subprocess.Popen(...)
        ProcessTracker.register(proc.pid, "aria2c")
        ...
        ProcessTracker.kill_all()  # called by cancelTask()
    """
    _pids: dict = {}  # pid → label

    @classmethod
    def register(cls, pid: int, label: str = "") -> None:
        cls._pids[pid] = label

    @classmethod
    def unregister(cls, pid: int) -> None:
        cls._pids.pop(pid, None)

    @classmethod
    def kill_all(cls) -> int:
        """Kill every tracked process. Returns count killed."""
        import os, signal, logging
        killed = 0
        for pid, label in list(cls._pids.items()):
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
                logging.info(f"[ProcessTracker] Killed PID {pid} ({label})")
            except ProcessLookupError:
                pass
            except Exception as e:
                logging.warning(f"[ProcessTracker] Kill PID {pid}: {e}")
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
                except Exception:
                    pass
        cls._pids.clear()
        return killed

    @classmethod
    def active(cls) -> list:
        """Return list of (pid, label) for alive processes."""
        import os
        alive = []
        dead = []
        for pid, label in cls._pids.items():
            try:
                os.kill(pid, 0)  # check if alive
                alive.append((pid, label))
            except ProcessLookupError:
                dead.append(pid)
        for p in dead:
            cls._pids.pop(p, None)
        return alive

    @classmethod
    def count(cls) -> int:
        return len(cls.active())


# ═════════════════════════════════════════════════════════════
# TaskInfo — live task state for /status panel
# ═════════════════════════════════════════════════════════════

class TaskInfo:
    """Structured live task state — updated by download/upload code."""
    phase:      str   = "idle"       # idle | download | upload | process | zip | extract
    engine:     str   = ""           # aria2c | yt-dlp | gdrive | telegram | ffmpeg
    filename:   str   = ""
    done_bytes: int   = 0
    total_bytes:int   = 0
    speed:      str   = ""
    eta:        str   = ""
    percentage: float = 0.0
    started_at: float = 0.0

    @classmethod
    def reset(cls):
        cls.phase = "idle"
        cls.engine = ""
        cls.filename = ""
        cls.done_bytes = 0
        cls.total_bytes = 0
        cls.speed = ""
        cls.eta = ""
        cls.percentage = 0.0
        cls.started_at = 0.0

    @classmethod
    def set(cls, **kw):
        for k, v in kw.items():
            if hasattr(cls, k):
                setattr(cls, k, v)


class ActiveJobs:
    """Registre des jobs FC hardsub / FFmpeg local en cours, tenus dans un
    dict {job_id: asyncio.Task} pour permettre une annulation individuelle
    (plusieurs jobs peuvent tourner en parallèle via le Semaphore(3) FC,
    donc pas question d'utiliser BOT.TASK/cancelTask() qui ne gère qu'une
    seule tâche à la fois)."""
    tasks: dict = {}

    @classmethod
    def register(cls, job_id: str, task) -> None:
        cls.tasks[job_id] = task

    @classmethod
    def unregister(cls, job_id: str) -> None:
        cls.tasks.pop(job_id, None)

    @classmethod
    def cancel(cls, job_id: str) -> bool:
        task = cls.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            return True
        return False
