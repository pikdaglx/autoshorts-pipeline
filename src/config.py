import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

OUTPUT_DIR = (BASE_DIR / "output").resolve()
TEMP_DIR = (BASE_DIR / "temp").resolve()

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Configuracoes de Video Shorts
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))
VIDEO_FPS = int(os.getenv("VIDEO_FPS", "30"))

# Configuracao de IA
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Legendas
SUBTITLE_FONT_SIZE = int(os.getenv("SUBTITLE_FONT_SIZE", "74"))

def get_ffmpeg_path() -> str:
    """Localiza o executavel FFmpeg no sistema ou WinGet."""
    # 1. PATH do sistema
    which = shutil.which("ffmpeg")
    if which:
        return which

    # 2. WinGet Packages
    winget_paths = list(Path(os.environ.get("LOCALAPPDATA", "")).glob("Microsoft/WinGet/Packages/*ffmpeg*/**/bin/ffmpeg.exe"))
    if winget_paths and winget_paths[0].is_file():
        return str(winget_paths[0])

    return "ffmpeg"
