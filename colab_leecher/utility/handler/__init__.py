"""
colab_leecher/utility/handler/__init__.py

Réexporte toute la surface publique qui vivait auparavant directement dans
handler.py, désormais éclatée en shared.py / task_control.py / leech.py /
cloudconvert_tasks.py / seedr_tasks.py / direct_hardsub_tasks.py /
local_video_tasks.py / archive_tasks.py — pour que chaque
`from colab_leecher.utility.handler import X` déjà existant ailleurs dans
le bot (services/hardsub_flow.py, __main__.py, claude_agent.py,
video_menu.py...) continue de fonctionner sans aucune modification.
"""

from colab_leecher.utility.handler.archive_tasks import Unzip_Handler, Zip_Handler
from colab_leecher.utility.handler.cloudconvert_tasks import CloudConvert_Handler
from colab_leecher.utility.handler.direct_hardsub_tasks import (
    Direct_CC_Hardsub_Handler,
    Direct_FC_Hardsub_Handler,
)
from colab_leecher.utility.handler.leech import Leech
from colab_leecher.utility.handler.local_video_tasks import (
    Local_Compress_Handler,
    Local_ManualShot_Handler,
    Local_Merge_Handler,
    Local_Metadata_Handler,
    Local_Mute_Handler,
    Local_Rename_Handler,
    Local_Sample_Handler,
    Local_Screenshots_Handler,
    Local_Split_Handler,
    Local_Subs_Handler,
    Local_Thumb_Handler,
    Local_ToAudio_Handler,
    Local_Trim_Handler,
    Local_Video_Convert_Handler,
)
from colab_leecher.utility.handler.seedr_tasks import (
    Seedr_CC_Convert_Handler,
    Seedr_CC_Hardsub_Handler,
    Seedr_FC_Hardsub_Handler,
)
from colab_leecher.utility.handler.task_control import SendLogs, cancelTask

__all__ = [
    "Unzip_Handler", "Zip_Handler",
    "CloudConvert_Handler",
    "Direct_CC_Hardsub_Handler", "Direct_FC_Hardsub_Handler",
    "Leech",
    "Local_Compress_Handler", "Local_ManualShot_Handler", "Local_Merge_Handler",
    "Local_Metadata_Handler", "Local_Mute_Handler", "Local_Rename_Handler",
    "Local_Sample_Handler", "Local_Screenshots_Handler", "Local_Split_Handler",
    "Local_Subs_Handler", "Local_Thumb_Handler", "Local_ToAudio_Handler",
    "Local_Trim_Handler", "Local_Video_Convert_Handler",
    "Seedr_CC_Convert_Handler", "Seedr_CC_Hardsub_Handler", "Seedr_FC_Hardsub_Handler",
    "SendLogs", "cancelTask",
]
