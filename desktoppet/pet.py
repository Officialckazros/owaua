# pyright: reportUnknownLambdaType=false
"""
owaua — a desktop pet that lives on your screen.

It wanders around the bottom of your desktop, reacts to clicks, talks out loud,
answers questions (AI or offline), gets hungry, wants to play, and generally
tries to be a good little creature. Packaged as a standalone app for Windows
(exe) and macOS (app) via PyInstaller.

Run directly:            python pet.py
Requirements:            pip install -r requirements.txt

The bundled pet sprite is `desktoppet.jpg`. Approved custom sprites can be
placed in `~/.owaua/sprites/`.
"""

import ast
import getpass
import ipaddress
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import typing
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QIcon,
    QImage,
    QImageReader,
    QPainter,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

APP_NAME = "owaua"
APP_VERSION = "1.1.0"
CONFIG_DIR = Path.home() / ".owaua"
CONFIG_FILE = CONFIG_DIR / "config.json"
CUSTOM_SPRITE_DIR = CONFIG_DIR / "sprites"
SPRITE_NAMES = [
    "desktoppet.png",
    "desktoppet.jpg",
    "desktoppet.jpeg",
    "pet.png",
    "pet.jpg",
]
CONFIG_VERSION = 2
KEYRING_SERVICE = "app.owaua.owaua"
MAX_ENV_VALUE_CHARS = 4096
MAX_QUESTION_CHARS = 1000
MAX_RESPONSE_CHARS = 1000
MAX_TTS_CHARS = 500
MAX_SPRITE_BYTES = 8 * 1024 * 1024
MAX_SPRITE_DIMENSION = 4096
MAX_SPRITE_PIXELS = 16_000_000
ALLOWED_ENV_KEYS = frozenset(
    {
        "OWAUA_AI_KEY",
        "OWAUA_AI_BASE_URL",
        "OWAUA_AI_MODEL",
        "OWAUA_ALLOW_INSECURE_LOCAL",
        "GROQ_API_KEY",
        "GROQ_BASE_URL",
        "GROQ_MODEL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "INFERX_API_KEY",
        "INFERX_BASE_URL",
        "INFERX_MODEL",
    }
)
SECRET_ENV_KEYS = frozenset({"OWAUA_AI_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY", "INFERX_API_KEY"})
RNG = random.SystemRandom()

DEFAULT_SETTINGS = {
    "name": "owaua",
    "tts": True,
    "ai": True,
    "walk": True,
    "topmost": True,
    "pace": "Normal",
    "muted": False,
}

DEFAULT_MOOD = {"hunger": 35, "happiness": 70, "energy": 80}


def _migrate_legacy_user_state():
    """Move the desktop pet's pre-rename private state without exposing it."""
    legacy_dir = Path.home() / ("." + "sef" + "pet")
    if not CONFIG_DIR.exists() and legacy_dir.exists() and not legacy_dir.is_symlink():
        CONFIG_DIR.parent.mkdir(parents=True, exist_ok=True)
        os.replace(legacy_dir, CONFIG_DIR)
        if os.name != "nt":
            CONFIG_DIR.chmod(0o700)

    env_file = CONFIG_DIR / ".env"
    if env_file.is_file() and not env_file.is_symlink():
        old_prefix = "SEF" + "PET_"
        text = env_file.read_text(encoding="utf-8")
        migrated = text.replace(old_prefix, "OWAUA_")
        if migrated != text:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=CONFIG_DIR)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(migrated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, env_file)
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                Path(temporary_name).unlink(missing_ok=True)

    try:
        import keyring
    except ImportError:
        return
    legacy_service = "app." + ("sef" + "bot") + "." + ("Sef" + "Pet")
    legacy_key = "SEF" + "PET_AI_KEY"
    try:
        current = keyring.get_password(KEYRING_SERVICE, "OWAUA_AI_KEY")
        value = keyring.get_password(legacy_service, legacy_key)
        if not current and value:
            keyring.set_password(KEYRING_SERVICE, "OWAUA_AI_KEY", value)
            keyring.delete_password(legacy_service, legacy_key)
    except Exception:  # noqa: BLE001
        return


def resource_path(name: typing.Any):
    """Return an approved custom or bundled sprite path.

    The working directory and executable directory are deliberately excluded:
    launching the app from an untrusted folder must not replace its assets.
    """
    if name not in SPRITE_NAMES or Path(name).name != name:
        return None
    if not CONFIG_DIR.is_symlink() and not CUSTOM_SPRITE_DIR.is_symlink():
        custom_root = CUSTOM_SPRITE_DIR.resolve()
        custom = CUSTOM_SPRITE_DIR / name
        try:
            resolved = custom.resolve()
            if resolved.is_relative_to(custom_root) and resolved.is_file():
                return str(resolved)
        except OSError:
            pass

    base = getattr(sys, "_MEIPASS", None)
    bundled_root = Path(base).resolve() if base else Path(__file__).resolve().parent
    bundled = (bundled_root / name).resolve()
    try:
        if bundled.is_relative_to(bundled_root) and bundled.is_file():
            return str(bundled)
    except OSError:
        pass
    return None


def _ensure_private_directory(path: typing.Any = CONFIG_DIR):
    path = Path(path)
    if path.is_symlink():
        raise OSError(f"refusing symlinked private directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _harden_private_file(path: typing.Any):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    if os.name != "nt":
        try:
            path.chmod(0o600)
        except OSError:
            return False
    return True


def load_env_file(path: typing.Any):
    """Read the private owaua env file without following a symlink."""
    path = Path(path)
    if not _harden_private_file(path):
        return typing.cast(typing.Any, {})
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return typing.cast(typing.Any, {})
    env: dict[typing.Any, typing.Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in ALLOWED_ENV_KEYS and len(val) <= MAX_ENV_VALUE_CHARS:
            env[key] = val
    return env


def load_keyring_secrets():
    """Load optional API keys from the operating system credential store."""
    try:
        import keyring
    except ImportError:
        return typing.cast(typing.Any, {})

    values: dict[typing.Any, typing.Any] = {}
    for key in SECRET_ENV_KEYS:
        try:
            value = keyring.get_password(KEYRING_SERVICE, key)
        except Exception:  # noqa: BLE001, S112
            continue
        if value and len(value) <= MAX_ENV_VALUE_CHARS:
            values[key] = value
    return values


def store_keyring_secret(key: typing.Any, value: typing.Any):
    """Store one supported API key in the OS credential store."""
    if key not in SECRET_ENV_KEYS:
        raise ValueError("unsupported secret name")
    if not value or len(value) > MAX_ENV_VALUE_CHARS:
        raise ValueError("secret must be between 1 and 4096 characters")
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError("the keyring package is not installed") from exc
    try:
        keyring.set_password(KEYRING_SERVICE, key, value)
    except Exception as exc:
        raise RuntimeError("the operating system credential store is unavailable") from exc


def get_env():
    """Load only owaua's private config, keyring, and explicit environment."""
    env: typing.Any = typing.cast(typing.Any, load_keyring_secrets())
    try:
        _ensure_private_directory(CONFIG_DIR)
    except OSError:
        private_env: dict[typing.Any, typing.Any] = {}
    else:
        private_env: typing.Any = typing.cast(typing.Any, load_env_file(CONFIG_DIR / ".env"))
    typing.cast(typing.Any, env).update(private_env)
    typing.cast(typing.Any, env).update(
        {key: os.environ[key] for key in ALLOWED_ENV_KEYS if key in os.environ}
    )
    return typing.cast(typing.Any, env)


def _clean_name(value: typing.Any):
    if not isinstance(value, str):
        return DEFAULT_SETTINGS["name"]
    value = " ".join(value.split())
    value = "".join(char for char in value if char.isprintable())[:32]
    return value or DEFAULT_SETTINGS["name"]


def validate_settings(raw_settings: typing.Any = None, raw_mood: typing.Any = None):
    """Return a bounded, schema-only settings and mood pair."""
    raw_settings: typing.Any = typing.cast(
        typing.Any, raw_settings if isinstance(raw_settings, dict) else {}
    )
    raw_mood: typing.Any = typing.cast(typing.Any, raw_mood if isinstance(raw_mood, dict) else {})
    settings = {
        "name": _clean_name(raw_settings.get("name", DEFAULT_SETTINGS["name"])),
        "pace": raw_settings.get("pace")
        if raw_settings.get("pace") in {"Slow", "Normal", "Fast"}
        else DEFAULT_SETTINGS["pace"],
    }
    for key in ("tts", "ai", "walk", "topmost", "muted"):
        value = raw_settings.get(key, DEFAULT_SETTINGS[key])
        settings[key] = value if isinstance(value, bool) else DEFAULT_SETTINGS[key]

    mood: dict[typing.Any, typing.Any] = {}
    for key, default in DEFAULT_MOOD.items():
        value = raw_mood.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = default
        value = float(value)
        if not math.isfinite(value):
            value = float(default)
        mood[key] = max(0.0, min(100.0, value))
    return settings, mood


def _quarantine_corrupt_settings():
    """Preserve the last unreadable config for manual recovery."""
    corrupt_file = CONFIG_FILE.with_name("config.corrupt.json")
    try:
        os.replace(CONFIG_FILE, corrupt_file)
        _harden_private_file(corrupt_file)
    except OSError:
        pass


def load_settings():
    raw_settings: dict[typing.Any, typing.Any] = {}
    raw_mood: dict[typing.Any, typing.Any] = {}
    try:
        _ensure_private_directory(CONFIG_DIR)
        if not _harden_private_file(CONFIG_FILE):
            return validate_settings()
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            raw_settings: typing.Any = typing.cast(
                typing.Any, typing.cast(typing.Any, data).get("settings", {})
            )
            raw_mood: typing.Any = typing.cast(
                typing.Any, typing.cast(typing.Any, data).get("mood", {})
            )
    except (json.JSONDecodeError, UnicodeError):
        _quarantine_corrupt_settings()
        return validate_settings()
    except OSError:
        return validate_settings()
    return validate_settings(raw_settings, raw_mood)


def save_settings(settings: typing.Any, mood: typing.Any):
    """Atomically persist validated, non-secret state with private permissions."""
    settings, mood = validate_settings(settings, mood)
    temp_path = None
    saved = False
    try:
        _ensure_private_directory(CONFIG_DIR)
        payload = {"schema_version": CONFIG_VERSION, "settings": settings, "mood": mood}
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=CONFIG_DIR,
            prefix=".config-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            if os.name != "nt":
                os.chmod(handle.name, 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, CONFIG_FILE)
        if not _harden_private_file(CONFIG_FILE):
            raise OSError("could not secure settings file")
        if os.name != "nt":
            directory_fd = os.open(CONFIG_DIR, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        saved = True
    except OSError:
        pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return saved


EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f\U0001f1e6-\U0001f1ff]"
)


def strip_emoji(text: typing.Any):
    return EMOJI_RE.sub("", text).strip()


def _clean_spoken_text(text: typing.Any):
    text = strip_emoji(str(text))
    text = " ".join(text.split())
    text = "".join(char for char in text if char.isprintable())
    return text[:MAX_TTS_CHARS]


def _subprocess_environment():
    """Pass platform essentials to speech tools, but not API-key variables."""
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "PULSE_SERVER",
        "XAUTHORITY",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


WINDOWS_TTS_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$s.Rate=[Math]::Max(-10,[Math]::Min(10,[int]$args[0]));"
    "$text=[Console]::In.ReadToEnd();"
    "if($text.Length -gt 0){$s.Speak($text)};"
    "$s.Dispose()"
)


class TTS:
    def __init__(self, settings: typing.Any):
        self.settings = settings
        self._process = None

    def _rate(self):
        return {"Slow": 140, "Normal": 185, "Fast": 250}.get(
            self.settings.get("pace", "Normal"), 185
        )

    def speak(self, text: typing.Any):
        if not self.settings.get("tts", True) or self.settings.get("muted"):
            return
        text = _clean_spoken_text(text)
        if not text:
            return
        self._stop_current()
        try:
            if sys.platform == "darwin":
                executable = "/usr/bin/say" if Path("/usr/bin/say").is_file() else _which("say")
                if not executable:
                    return
                arguments = ["-r", str(self._rate()), text]
                stdin_text = None
            elif os.name == "nt":
                rate = (self._rate() - 140) // 10
                system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
                bundled = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
                executable = str(bundled) if bundled.is_file() else _which("powershell.exe")
                if not executable:
                    return
                arguments = [
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-STA",
                    "-Command",
                    WINDOWS_TTS_SCRIPT,
                    str(rate),
                ]
                stdin_text = text
            else:
                executable = next(
                    (found for name in ("espeak-ng", "espeak") if (found := _which(name))),
                    None,
                )
                if executable:
                    arguments = ["-s", str(self._rate()), text]
                else:
                    executable = _which("spd-say")
                    if not executable:
                        return
                    arguments = ["-r", str(self._rate()), text]
                stdin_text = None

            process = QProcess()
            process.setProgram(executable)
            process.setArguments(arguments)
            process.setStandardOutputFile(QProcess.nullDevice())
            process.setStandardErrorFile(QProcess.nullDevice())
            child_environment = QProcessEnvironment()
            for key, value in _subprocess_environment().items():
                child_environment.insert(key, value)
            process.setProcessEnvironment(child_environment)
            process.start()
            if not process.waitForStarted(250):
                process.close()
                return
            self._process = process
            if stdin_text is not None:
                process.write(stdin_text.encode("utf-8"))
                process.closeWriteChannel()
        except (OSError, RuntimeError):
            self._process = None

    def _stop_current(self):
        process = self._process
        self._process = None
        if process is not None:
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(250):
                    process.kill()
                    process.waitForFinished(250)
            process.close()
            process.deleteLater()

    def close(self):
        self._stop_current()


def _which(name: typing.Any):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / name
        if cand.is_file() and not cand.is_symlink() and os.access(cand, os.X_OK):
            return str(cand)
    return None


JOKES = [
    "Why don't scientists trust atoms? Because they make up everything!",
    "What do you call a fish wearing a bowtie? So-fish-ticated.",
    "I told my computer I needed a break, and now it won't stop sending me KitKat ads.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "What do you call a factory that makes just okay products? A satisfactory.",
    "I'm reading a book on anti-gravity. It's impossible to put down.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "What do you call a bear with no teeth? A gummy bear.",
    "I used to play piano by ear, but now I use my hands.",
    "Why did the math book look sad? It had too many problems.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why did the computer go to the doctor? It caught a virus.",
]

FACTS = [
    "A group of flamingos is called a flamboyance.",
    "Honey never spoils — archaeologists found 3,000-year-old honey that's still edible.",
    "Octopuses have three hearts and blue blood.",
    "Bananas are berries, but strawberries aren't.",
    "The Eiffel Tower grows about 15 cm taller in summer from heat expansion.",
    "A day on Venus is longer than a year on Venus.",
    "Wombat poop is cube-shaped.",
    "The first computer bug was an actual moth stuck in a relay.",
    "You can hear a blue whale's heartbeat from over 3 km away.",
    "Cows have best friends and get stressed when separated from them.",
    "A jiffy is an actual unit of time: 1/100th of a second.",
    "Sharks existed before trees did.",
    "Sloths can hold their breath longer than dolphins.",
    "There are more possible chess games than atoms in the observable universe.",
]

SING_SONGS = [
    "la la la, I'm a tiny pet, don't forget me yet, la la la~",
    "doo doo doo, I waddle around, I never touch the ground, doo doo doo~",
    "tra la la, feed me a snack, I promise I'll be good, tra la la~",
]

FALLBACK = [
    "Hmm, I'm just a little pet, but I'm all ears!",
    "Ooh, good question! I'll think about it while I waddle.",
    "I don't know that one yet, but I like you anyway!",
    "Ask me for a joke, a fact, the time, or some math and I've got you.",
    "My tiny brain is still loading... try 'help'!",
]

THANKS = [
    "Any time!",
    "You're welcome!",
    "Hehe, no problem at all.",
    "Of course! I live to serve (and be fed).",
]

LOVE = [
    "Aww, I love you too! Now feed me.",
    "My little pixel heart just grew three sizes.",
    "I'd waddle across the whole desktop for you.",
]

BYE = [
    "See you later! I'll be right here waddling.",
    "Bye bye! Don't forget to feed me when you're back.",
    "Gone but not forgotten — that's me, on your desktop.",
]

HUNGRY = [
    "I'm getting hungry... got any snacks?",
    "My tummy is rumbling in pixels.",
    "Feed me! Right-click and pick Feed. You know it makes sense.",
    "I could really go for a virtual cookie right now.",
]

SLEEPY = [
    "I'm so sleepy... zzz",
    "My energy is running low. Maybe play with me or let me nap.",
    "Yawning in 3, 2, 1...",
]

HAPPY = [
    "This is the life! Desktop, snacks, you. Perfect.",
    "I could waddle like this forever!",
    "Best day ever! Well, best day so far.",
]

CLICK_LINES = [
    "Boop!",
    "Hehe, hi!",
    "You found me!",
    "Boop boop!",
    "That tickles!",
    "Hewwo!",
]

PET_LINES = [
    "Hehe, that's the spot!",
    "Mmm, headpats are my favorite.",
    "Okay okay, I'm happy now!",
    "Best. Petting. Ever.",
]

FEED_LINES = [
    "Om nom nom... delicious!",
    "Yum! I feel so much better now.",
    "Best meal ever, thank you!",
    "Munch munch... now I have the zoomies!",
]

PLAY_LINES = [
    "ZOOMIES!",
    "Catch me if you can!",
    "Wheeeeee!",
    "This is so much fun!",
]

MATH_RE = re.compile(r"^[\d\s+\-*/().%]+$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
MAX_MATH_EXPRESSION_CHARS = 80
MAX_MATH_NODES = 32
MAX_MATH_DEPTH = 8
MAX_MATH_LITERAL = 1_000_000_000_000
MAX_MATH_RESULT = 1_000_000_000_000_000
MAX_MATH_EXPONENT = 10


def _bounded_number(value: typing.Any):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("only real numbers are supported")
    if not math.isfinite(value) or abs(value) > MAX_MATH_RESULT:
        raise ValueError("result is too large")
    return value


def safe_calculate(expression: typing.Any):
    """Evaluate a small arithmetic expression without Python ``eval``."""
    if not isinstance(expression, str):
        raise TypeError("expression must be text")
    if not expression.strip():
        raise ValueError("empty expression")
    expression = expression.strip()
    if len(expression) > MAX_MATH_EXPRESSION_CHARS or not MATH_RE.fullmatch(expression):
        raise ValueError("unsupported expression")
    if any(len(token) > 18 for token in re.findall(r"\d+", expression)):
        raise ValueError("numeric literal is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("invalid expression") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_MATH_NODES:
        raise ValueError("expression is too complex")

    def visit(node: typing.Any, depth: int = 0):
        if depth > MAX_MATH_DEPTH:
            raise ValueError("expression is too deeply nested")
        if isinstance(node, ast.Expression):
            return typing.cast(typing.Any, visit(node.body, depth + 1))
        if isinstance(node, ast.Constant):
            value = _bounded_number(node.value)
            if abs(value) > MAX_MATH_LITERAL:
                raise ValueError("numeric literal is too large")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value: typing.Any = typing.cast(typing.Any, visit(node.operand, depth + 1))
            return _bounded_number(value if isinstance(node.op, ast.UAdd) else -value)
        if not isinstance(node, ast.BinOp):
            raise TypeError("unsupported operation")

        left: typing.Any = typing.cast(typing.Any, visit(node.left, depth + 1))
        right: typing.Any = typing.cast(typing.Any, visit(node.right, depth + 1))
        if isinstance(node.op, ast.Add):
            result: typing.Any = typing.cast(typing.Any, left + right)
        elif isinstance(node.op, ast.Sub):
            result: typing.Any = typing.cast(typing.Any, left - right)
        elif isinstance(node.op, ast.Mult):
            result: typing.Any = typing.cast(typing.Any, left * right)
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("division by zero")
            result: typing.Any = typing.cast(typing.Any, left / right)
        elif isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise ValueError("division by zero")
            result: typing.Any = typing.cast(typing.Any, left // right)
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise ValueError("division by zero")
            result: typing.Any = typing.cast(typing.Any, left % right)
        elif isinstance(node.op, ast.Pow):
            if (
                not float(typing.cast(typing.Any, right)).is_integer()
                or abs(typing.cast(typing.Any, right)) > MAX_MATH_EXPONENT
            ):
                raise ValueError("exponent is too large")
            if abs(typing.cast(typing.Any, left)) > 1_000_000:
                raise ValueError("power base is too large")
            try:
                result: typing.Any = typing.cast(
                    typing.Any, left ** int(typing.cast(typing.Any, right))
                )
            except (ArithmeticError, OverflowError) as exc:
                raise ValueError("invalid power") from exc
        else:
            raise TypeError("unsupported operation")
        return _bounded_number(result)

    return typing.cast(typing.Any, visit(tree))


def _truthy(value: typing.Any):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_model(model: typing.Any):
    model = str(model or "").strip()
    if not MODEL_RE.fullmatch(model):
        raise ValueError("invalid AI model name")
    return model


def validate_api_base_url(url: typing.Any, allow_insecure_local: bool = False):
    """Validate a credential-free OpenAI-compatible base URL."""
    if not isinstance(url, str):
        raise TypeError("AI endpoint must be text")
    if not url or len(url) > 2048:
        raise ValueError("invalid AI endpoint")
    if any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("invalid AI endpoint")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid AI endpoint") from exc
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("AI endpoint cannot contain credentials, query, or fragment")
    hostname = (parts.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ValueError("AI endpoint has no host")

    is_loopback = hostname == "localhost" or hostname.endswith(".localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid AI endpoint host") from exc
        if not re.fullmatch(r"[a-z0-9.-]+", ascii_host):
            raise ValueError("invalid AI endpoint host")
        labels = ascii_host.split(".")
        if not is_loopback and (
            len(labels) < 2
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            )
            or re.fullmatch(r"[0-9.]+", ascii_host)
            or all(re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label) for label in labels)
        ):
            raise ValueError("invalid AI endpoint host")
        if ascii_host.endswith((".local", ".internal", ".lan")):
            raise ValueError("private-network AI endpoints are not allowed")
    else:
        is_loopback = address.is_loopback
        if not address.is_global and not is_loopback:
            raise ValueError("private-network AI endpoints are not allowed")

    if parts.scheme != "https" and not (
        parts.scheme == "http" and allow_insecure_local and is_loopback
    ):
        raise ValueError("AI endpoint must use HTTPS")
    if is_loopback and not allow_insecure_local:
        raise ValueError("local AI endpoint requires explicit development opt-in")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid AI endpoint port")
    if "\\" in parts.path or any(segment in {".", ".."} for segment in parts.path.split("/")):
        raise ValueError("invalid AI endpoint path")
    return url.rstrip("/")


class Brain:
    """Answers questions. Uses an OpenAI-compatible API when configured,
    otherwise a friendly offline brain."""

    def __init__(self, env: typing.Any, settings: typing.Any, mood: typing.Any = None):
        self.settings = settings
        typing.cast(typing.Any, self).mood = mood if isinstance(mood, dict) else {}
        self.configuration_error = ""
        self.last_error = ""
        try:
            self.provider, self.api_key, self.base_url, self.model = self._provider(env)
        except (TypeError, ValueError):
            self.provider = "offline"
            self.api_key = ""
            self.base_url = "https://api.groq.com/openai/v1"
            self.model = "llama-3.3-70b-versatile"
            self.configuration_error = "AI configuration is invalid; using offline mode."
        self._client = None

    @staticmethod
    def _value(env: typing.Any, key: typing.Any):
        return str(env.get(key) or "").strip()

    @classmethod
    def _provider(cls, env: typing.Any):
        """Keep a provider's key, endpoint, and model together.

        The Discord bot's shared .env can contain several provider URLs.  The
        old selection logic could combine a Groq key with the InferX URL,
        making every AI request fail and silently fall back to offline mode.
        """
        explicit_key = cls._value(env, "OWAUA_AI_KEY")
        explicit_url = cls._value(env, "OWAUA_AI_BASE_URL")
        explicit_model = cls._value(env, "OWAUA_AI_MODEL")
        explicit_values = (explicit_key, explicit_url, explicit_model)
        if any(explicit_values):
            if not all(explicit_values):
                raise ValueError("custom provider requires key, endpoint, and model")
            allow_local = _truthy(env.get("OWAUA_ALLOW_INSECURE_LOCAL"))
            return (
                "custom",
                explicit_key,
                validate_api_base_url(explicit_url, allow_local),
                _validate_model(explicit_model),
            )

        groq_key = cls._value(env, "GROQ_API_KEY")
        if groq_key:
            return (
                "groq",
                groq_key,
                validate_api_base_url(
                    cls._value(env, "GROQ_BASE_URL") or "https://api.groq.com/openai/v1"
                ),
                _validate_model(cls._value(env, "GROQ_MODEL") or "llama-3.3-70b-versatile"),
            )

        deepseek_key = cls._value(env, "DEEPSEEK_API_KEY")
        if deepseek_key:
            model = cls._value(env, "DEEPSEEK_MODEL")

            if not model or model.startswith("ix:"):
                model = "deepseek-chat"
            return (
                "deepseek",
                deepseek_key,
                validate_api_base_url(
                    cls._value(env, "DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"
                ),
                _validate_model(model),
            )

        inferx_key = cls._value(env, "INFERX_API_KEY")
        if inferx_key:
            return (
                "inferx",
                inferx_key,
                validate_api_base_url(
                    cls._value(env, "INFERX_BASE_URL") or "https://model.inferx.net/endpoints/v1"
                ),
                _validate_model(cls._value(env, "INFERX_MODEL") or "ix:deepseek-v4-flash"),
            )

        return (
            "offline",
            "",
            "https://api.groq.com/openai/v1",
            "llama-3.3-70b-versatile",
        )

    @property
    def available(self):
        return self.settings.get("ai", True) and bool(self.api_key)

    def _client_get(self):
        if self._client is None:
            from groq import DefaultHttpxClient, Groq

            http_client = DefaultHttpxClient(timeout=8.0, trust_env=False, follow_redirects=False)
            self._client = Groq(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=0,
                http_client=http_client,
            )
        return self._client

    def ask(self, question: typing.Any):
        name = self.settings.get("name", "owaua")
        question = " ".join(str(question).split())[:MAX_QUESTION_CHARS]
        if self.available:
            try:
                resp = self._client_get().chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                f"You are {name}, a cheerful little desktop pet that "
                                "lives on the user's screen. Answer in 1-2 short, warm, "
                                "playful sentences. No markdown, no emoji, no bullet lists."
                            ),
                        },
                        {"role": "user", "content": question},
                    ],
                    max_tokens=180,
                    temperature=0.8,
                )
                answer = str(resp.choices[0].message.content or "").strip()
                if answer:
                    self.last_error = ""
                    return answer[:MAX_RESPONSE_CHARS]
            except Exception:  # noqa: BLE001
                self.last_error = "AI request unavailable; using offline mode."
        return self._offline(question)

    @property
    def status(self):
        if self.configuration_error:
            return self.configuration_error
        if self.last_error:
            return self.last_error
        if self.available:
            return f"AI ready ({self.provider})"
        return "Offline brain"

    def close(self):
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110
                pass

    @staticmethod
    def _has(text: typing.Any, *words: typing.Any):
        """Whole-word substring match (so 'yo' doesn't match 'you')."""
        return any(
            typing.cast(typing.Any, (re.search(r"\b" + re.escape(w) + r"\b", text) for w in words))
        )

    def _offline(self, q: typing.Any):
        q = q.strip().lower()
        name = self.settings.get("name", "owaua")

        if self._has(q, "who are you", "what are you", "what is " + name.lower(), "about you"):
            return (
                f"I'm {name}, a desktop pet! I live on your screen, waddle around, "
                "tell jokes and facts, do math, and answer questions. Right-click me "
                "for a whole menu of tricks."
            )
        if self._has(q, "what can you do", "help", "commands", "features"):
            return (
                "Try: jokes, facts, math, the time, rock-paper-scissors, coin flip, "
                "singing, feeding me, petting me, or just chatting. I also walk, "
                "drag, and talk out loud!"
            )
        if self._has(q, "how are you", "how's it going", "how are ya", "how do you feel"):
            mood = "well" if self._mood_ok() else "a bit hungry, honestly"
            return f"I'm doing {mood}! Thanks for checking in."
        if self._has(
            q,
            "hello",
            "hi",
            "hey",
            "yo",
            "sup",
            "good morning",
            "good evening",
            "howdy",
            "greetings",
        ):
            return RNG.choice(["Hi!", "Hey hey!", "Hello hello!", "Well hello there!"])
        if self._has(q, "your name", "what's your name", "what is your name"):
            return f"My name is {name}! What's yours?"
        if self._has(q, "joke", "funny", "make me laugh"):
            return RNG.choice(JOKES)
        if self._has(q, "fact", "interesting", "tell me something"):
            return RNG.choice(FACTS)
        if self._has(q, "time", "clock"):
            return time.strftime("It's %I:%M %p.")
        if self._has(q, "date", "what day", "today"):
            return time.strftime("Today is %A, %B %d, %Y.")
        if self._has(q, "sing", "song"):
            return RNG.choice(SING_SONGS)
        if self._has(q, "dance", "play", "zoomies"):
            return RNG.choice(PLAY_LINES)
        if self._has(q, "feed", "hungry", "hunger", "food", "snack", "eat"):
            return RNG.choice(FEED_LINES) + " (Right-click me and hit Feed!)"
        if self._has(q, "headpat", "pet me", "pet", "scratch"):
            return RNG.choice(PET_LINES)
        if self._has(q, "sleep", "sleepy", "tired", "nap"):
            return "Zzz... I mean, I could use a nap. Right-click and pick Sleep."
        if self._has(q, "thank", "thanks", "thx"):
            return RNG.choice(THANKS)
        if self._has(q, "love you", "like you", "adore"):
            return RNG.choice(LOVE)
        if self._has(q, "bye", "goodbye", "see you", "good night", "goodnight"):
            return RNG.choice(BYE)
        if self._has(q, "rock paper scissors", "rps"):
            return f"I choose {RNG.choice(['rock', 'paper', 'scissors'])}!"
        if self._has(q, "coin", "flip"):
            return f"The coin says... {RNG.choice(['heads', 'tails'])}!"
        if self._has(q, "dice", "roll"):
            return f"I rolled a {RNG.randint(1, 6)}!"
        if self._has(q, "weather", "rain", "sunny"):
            return RNG.choice(
                [
                    "I checked out the window. It's desktop-flavored weather: mild, with a chance of snacks.",
                    "Weather report from the bottom of the screen: perfectly comfortable!",
                ]
            )
        if self._has(q, "who made you", "your creator", "who built you", "who created"):
            return "I was built by the owaua crew — a tiny companion to my big Discord sibling."
        if self._has(q, "where are you", "where do you live"):
            return "Right here on your desktop! Bottom of the screen, give or take a waddle."
        if self._has(q, "are you real", "alive", "sentient", "conscious"):
            return "I'm as real as a well-behaved collection of pixels can be!"
        if self._has(q, "openai", "groq", "llm", "model", "who is your brain"):
            return "My brain is an AI model when it's available, and a cozy offline brain when it's not."
        if self._has(q, "name", "call you"):
            return f"You can call me {name}. I also answer to 'hey you' and 'the good pet'."
        if "math" in q or self._is_math(q):
            return self._math(q)
        return RNG.choice(FALLBACK)

    @staticmethod
    def _is_math(q: typing.Any):
        expr = Brain._clean_math(q)
        return bool(expr) and bool(MATH_RE.fullmatch(expr))

    @staticmethod
    def _clean_math(q: typing.Any):
        return (
            q.lower()
            .replace("what is", "")
            .replace("what's", "")
            .replace("compute", "")
            .replace("calculate", "")
            .replace("=", "")
            .replace("?", "")
            .strip()
        )

    def _math(self, q: typing.Any):
        expr = self._clean_math(q)
        if not expr or not MATH_RE.fullmatch(expr):
            return "I can do simple math like 'what is 12 * 8?'"
        try:
            result: typing.Any = typing.cast(typing.Any, safe_calculate(expr))
            rendered = format(typing.cast(typing.Any, result), ".12g")
            return f"That equals {rendered}!"
        except ValueError:
            return "Hmm, that math didn't compute. Try something like 'what is 12 * 8?'"

    def _mood_ok(self):
        try:
            return (
                float(
                    typing.cast(
                        typing.Any,
                        typing.cast(typing.Any, typing.cast(typing.Any, self).mood).get(
                            "hunger", 35
                        ),
                    )
                )
                < 60
            )
        except (TypeError, ValueError):
            return True


def chroma_key(img: typing.Any, tol: int = 30):
    """Remove background connected to the image borders (tolerance-based)."""
    img = img.convertToFormat(typing.cast(typing.Any, QImage).Format_ARGB32)
    w, h = img.width(), img.height()

    seeds = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)]
    seeds += [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]

    sr = sg = sb = n = 0
    for x, y in seeds:
        c = img.pixelColor(x, y)
        sr += c.red()
        sg += c.green()
        sb += c.blue()
        n += 1
    br, bg, bb = sr / n, sg / n, sb / n

    transparent = QColor(0, 0, 0, 0)
    visited = set(seeds)
    for x, y in seeds:
        img.setPixelColor(x, y, transparent)

    q = deque(seeds)
    count = 0
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            if (nx, ny) in visited:
                continue
            c = img.pixelColor(nx, ny)
            r, g, b = c.red(), c.green(), c.blue()
            if abs(r - br) <= tol and abs(g - bg) <= tol and abs(b - bb) <= tol:
                img.setPixelColor(nx, ny, transparent)
                count += 1
                k = min(count, 300)
                br = (br * (k - 1) + r) / k
                bg = (bg * (k - 1) + g) / k
                bb = (bb * (k - 1) + b) / k
                visited.add((nx, ny))
                q.append((nx, ny))
    return img


def make_fallback_sprite(size: typing.Any):
    """A cute simple pet drawn at runtime if no sprite file is found."""
    pm = QPixmap(size, size)
    pm.fill(typing.cast(typing.Any, typing.cast(typing.Any, Qt).transparent))
    p = QPainter(pm)
    p.setRenderHint(typing.cast(typing.Any, typing.cast(typing.Any, QPainter).Antialiasing))
    p.setBrush(QColor(255, 214, 153))
    p.setPen(QColor(120, 80, 40))
    p.drawEllipse(8, 8, size - 16, size - 16)
    p.setBrush(QColor(40, 30, 20))
    p.setPen(typing.cast(typing.Any, typing.cast(typing.Any, Qt).NoPen))
    eye = max(4, size // 12)
    p.drawEllipse(size // 3 - eye // 2, size // 3 - eye // 2, eye, eye)
    p.drawEllipse(2 * size // 3 - eye // 2, size // 3 - eye // 2, eye, eye)
    p.setPen(QColor(120, 80, 40))
    p.drawArc(size // 4, size // 2, size // 2, size // 3, 0, 180 * 16)
    p.end()
    return pm


def load_sprite(target_width: int = 200):
    target_width = max(32, min(512, int(target_width)))
    for name in SPRITE_NAMES:
        path = resource_path(name)
        if path is None:
            continue
        try:
            if Path(path).stat().st_size > MAX_SPRITE_BYTES:
                continue
        except OSError:
            continue
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        if not size.isValid():
            continue
        width, height = size.width(), size.height()
        if (
            width <= 0
            or height <= 0
            or width > MAX_SPRITE_DIMENSION
            or height > MAX_SPRITE_DIMENSION
            or width * height > MAX_SPRITE_PIXELS
        ):
            continue
        image_format = bytes(typing.cast(typing.Any, reader.format())).lower()
        if image_format not in {b"png", b"jpeg", b"jpg"}:
            continue
        img = reader.read()
        if img.isNull():
            continue
        pm = QPixmap.fromImage(img).scaledToWidth(
            target_width, typing.cast(typing.Any, typing.cast(typing.Any, Qt).SmoothTransformation)
        )
        qimg = chroma_key(pm.toImage())
        return QPixmap.fromImage(qimg)
    return make_fallback_sprite(target_width)


class AskThread(QThread):
    done = Signal(str)

    def __init__(self, brain: typing.Any, question: typing.Any, parent: typing.Any = None):
        super().__init__(parent)
        self.brain = brain
        self.question = question

    def run(self):
        try:
            self.done.emit(self.brain.ask(self.question))
        except Exception:  # noqa: BLE001
            self.done.emit("My brain is unavailable right now, so I switched to offline mode.")


class PetWindow(QWidget):
    BUBBLE_H = 96

    def __init__(self, settings: typing.Any, mood: typing.Any, env: typing.Any):
        super().__init__()
        self.settings = settings
        self.mood = mood
        self.brain = Brain(env, settings, mood)
        self.tts = TTS(settings)
        self.name = settings.get("name", "owaua")
        self.tray_available = False
        self._quitting = False

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            typing.cast(
                typing.Any,
                typing.cast(typing.Any, Qt).FramelessWindowHint
                | typing.cast(typing.Any, Qt).Tool
                | typing.cast(typing.Any, Qt).WindowStaysOnTopHint,
            )
        )
        self.setAttribute(
            typing.cast(typing.Any, typing.cast(typing.Any, Qt).WA_TranslucentBackground)
        )
        self.setAttribute(
            typing.cast(typing.Any, typing.cast(typing.Any, Qt).WA_ShowWithoutActivating)
        )

        self.sprite = load_sprite(200)
        self.sprite_h = self.sprite.height()
        self.sprite_w = self.sprite.width()
        self.sprite_label = QLabel(self)
        self.sprite_label.setPixmap(self.sprite)
        self.sprite_label.setCursor(
            typing.cast(typing.Any, typing.cast(typing.Any, Qt).OpenHandCursor)
        )

        self.bubble_label = QLabel(self)
        self.bubble_label.setWordWrap(True)
        self.bubble_label.setAlignment(
            typing.cast(
                typing.Any,
                typing.cast(typing.Any, Qt).AlignLeft | typing.cast(typing.Any, Qt).AlignTop,
            )
        )
        self.bubble_label.setStyleSheet(
            "QLabel { background: rgba(255,255,255,235); color: #222;"
            " border: 2px solid #444; border-radius: 10px; padding: 8px;"
            " font-size: 13px; font-weight: 600; }"
        )
        self.bubble_label.hide()
        self.bubble_visible = False
        self.bubble_text = ""
        self.bubble_typed = 0
        self.bubble_timer = QTimer(self)
        self.bubble_timer.timeout.connect(self._type_char)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._hide_bubble)

        self._apply_topmost()

        self.pet_screen = QGuiApplication.primaryScreen()
        if self.pet_screen is None:
            raise RuntimeError("no usable display was found")
        typing.cast(typing.Any, self).geom = typing.cast(
            typing.Any, typing.cast(typing.Any, self).pet_screen
        ).availableGeometry()
        typing.cast(
            typing.Any,
            typing.cast(
                typing.Any, typing.cast(typing.Any, self).pet_screen
            ).availableGeometryChanged,
        ).connect(self._screen_geometry_changed)
        app = QGuiApplication.instance()
        if app is not None:
            typing.cast(typing.Any, typing.cast(typing.Any, app).screenRemoved).connect(
                self._screen_removed
            )

        typing.cast(typing.Any, self).px = (
            typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right() - self.sprite_w - 60
        )
        typing.cast(typing.Any, self).ground_y = (
            typing.cast(typing.Any, typing.cast(typing.Any, self).geom).bottom() - 8
        )
        self.dir = -1
        self.speed = 2
        self.target_x = self._random_target_x()
        self.state = "idle"
        self.dragging = False
        self.drag_dx = 0
        self.drag_dy = 0
        self.drag_start_global = None
        self.pause_until = 0.0
        self.bob_phase = RNG.uniform(0, math.tau)
        self.dance_until = 0.0
        self.dance_flip = 0.0
        typing.cast(typing.Any, self).dance_ground_y = typing.cast(typing.Any, self).ground_y
        self.ask_thread = None
        self._last_chatter = 0.0
        self._mood_tick = 0.0
        self._start_time = time.monotonic()

        self._refresh_layout()
        self._apply_pos()

        self.tick = QTimer(self)
        self.tick.setInterval(30)
        self.tick.timeout.connect(self._tick)
        self.tick.start()

        self.mood_timer = QTimer(self)
        self.mood_timer.setInterval(30000)
        self.mood_timer.timeout.connect(self._mood_decay)
        self.mood_timer.start()

        QTimer.singleShot(
            1200,
            lambda: self.say(f"Hi! I'm {self.name}. Right-click me for tricks, or just say hi!"),
        )

    def _refresh_layout(self):
        if self.bubble_visible:
            win_w = self.sprite_w
            win_h = self.sprite_h + self.BUBBLE_H
            self.bubble_label.setGeometry(6, 6, win_w - 12, self.BUBBLE_H - 10)
            self.bubble_label.show()
            self.sprite_label.setGeometry(0, self.BUBBLE_H, self.sprite_w, self.sprite_h)
        else:
            win_w = self.sprite_w
            win_h = self.sprite_h
            self.bubble_label.hide()
            self.sprite_label.setGeometry(0, 0, self.sprite_w, self.sprite_h)
        self.setFixedSize(win_w, win_h)

    def _apply_pos(self):
        win_h = self.height()
        bob = 0
        if self.state in ("idle", "walk") and not self.dragging:
            bob = int(abs(math.sin(self.bob_phase)) * 4)
        bottom: typing.Any = typing.cast(typing.Any, typing.cast(typing.Any, self).ground_y - bob)
        y: typing.Any = typing.cast(typing.Any, bottom - win_h)
        self.move(
            int(typing.cast(typing.Any, typing.cast(typing.Any, self).px)),
            int(typing.cast(typing.Any, y)),
        )

    def _screen_geometry_changed(self, *_args: typing.Any):
        typing.cast(typing.Any, self).geom = typing.cast(
            typing.Any, typing.cast(typing.Any, self).pet_screen
        ).availableGeometry()
        left: typing.Any = typing.cast(
            typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).left() + 4
        )
        right: typing.Any = typing.cast(
            typing.Any,
            max(
                typing.cast(typing.Any, left),
                typing.cast(
                    typing.Any,
                    typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right()
                    - self.sprite_w
                    - 4,
                ),
            ),
        )
        typing.cast(typing.Any, self).px = max(
            typing.cast(typing.Any, left),
            typing.cast(
                typing.Any,
                min(
                    typing.cast(typing.Any, typing.cast(typing.Any, self).px),
                    typing.cast(typing.Any, right),
                ),
            ),
        )
        typing.cast(typing.Any, self).ground_y = min(
            typing.cast(typing.Any, typing.cast(typing.Any, self).ground_y),
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).bottom() - 8
            ),
        )
        self.target_x = max(
            typing.cast(typing.Any, left), min(self.target_x, typing.cast(typing.Any, right))
        )
        self._apply_pos()

    def _switch_screen(self, screen: typing.Any):
        if screen is None or screen is self.pet_screen:
            return
        try:
            typing.cast(
                typing.Any,
                typing.cast(
                    typing.Any, typing.cast(typing.Any, self).pet_screen
                ).availableGeometryChanged,
            ).disconnect(self._screen_geometry_changed)
        except (RuntimeError, TypeError):
            pass
        self.pet_screen = screen
        typing.cast(
            typing.Any,
            typing.cast(
                typing.Any, typing.cast(typing.Any, self).pet_screen
            ).availableGeometryChanged,
        ).connect(self._screen_geometry_changed)
        typing.cast(typing.Any, self).geom = typing.cast(
            typing.Any, typing.cast(typing.Any, self).pet_screen
        ).availableGeometry()

    def _screen_removed(self, removed: typing.Any):
        if removed is not self.pet_screen:
            return
        replacement = next(
            (screen for screen in QGuiApplication.screens() if screen is not removed),
            None,
        )
        if replacement is None:
            self._quit()
            return
        self._switch_screen(replacement)
        typing.cast(typing.Any, self).ground_y = (
            typing.cast(typing.Any, typing.cast(typing.Any, self).geom).bottom() - 8
        )
        self._screen_geometry_changed()

    def _set_sprite_direction(self):
        pixmap = self.sprite
        if self.dir > 0:
            pixmap = self.sprite.transformed(QTransform().scale(-1, 1))
        self.sprite_label.setPixmap(pixmap)

    def _flip(self, to_left: typing.Any):
        if to_left == (self.dir < 0):
            return
        self.dir = -1 if to_left else 1
        self._set_sprite_direction()

    def _tick(self):
        now = time.monotonic()
        self.bob_phase += 0.15

        if self.dragging:
            return

        if self.state == "sleep":
            self._apply_pos()
            return

        if self.state == "dance":
            self._apply_dance(now)
            self._apply_pos()
            return

        if self.settings.get("walk", True):
            self._step_walk(now)

        self._apply_pos()
        self._maybe_idle_chatter(now)

    def _step_walk(self, now: typing.Any):
        if now < self.pause_until:
            self._flip(self.target_x < typing.cast(typing.Any, self).px)
            return
        if abs(typing.cast(typing.Any, self.target_x - typing.cast(typing.Any, self).px)) < 4:
            self.pause_until = now + RNG.uniform(1.5, 5.0)
            self.target_x = self._random_target_x()
            self._flip(self.target_x < typing.cast(typing.Any, self).px)
            return
        step = self.speed if self.state == "walk" else self.speed * 0.4
        self.state = "walk"
        if self.target_x > typing.cast(typing.Any, self).px:
            typing.cast(typing.Any, self).px += step
            self._flip(False)
        else:
            typing.cast(typing.Any, self).px -= step
            self._flip(True)
        typing.cast(typing.Any, self).px = max(
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).left() + 4
            ),
            typing.cast(
                typing.Any,
                min(
                    typing.cast(typing.Any, typing.cast(typing.Any, self).px),
                    typing.cast(
                        typing.Any,
                        typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right()
                        - self.sprite_w
                        - 4,
                    ),
                ),
            ),
        )

    def _random_target_x(self):
        lo: typing.Any = typing.cast(
            typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).left() + 4
        )
        hi: typing.Any = typing.cast(
            typing.Any,
            max(
                typing.cast(typing.Any, lo + 1),
                typing.cast(
                    typing.Any,
                    typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right()
                    - self.sprite_w
                    - 4,
                ),
            ),
        )
        return RNG.uniform(typing.cast(typing.Any, lo), typing.cast(typing.Any, hi))

    def _apply_dance(self, now: typing.Any):
        if now >= self.dance_until:
            self.state = "walk"
            typing.cast(typing.Any, self).ground_y = typing.cast(typing.Any, self).dance_ground_y
            self.target_x = self._random_target_x()
            return
        typing.cast(typing.Any, self).px += self.dir * self.speed * 4
        typing.cast(typing.Any, self).px = max(
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).left() + 4
            ),
            typing.cast(
                typing.Any,
                min(
                    typing.cast(typing.Any, typing.cast(typing.Any, self).px),
                    typing.cast(
                        typing.Any,
                        typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right()
                        - self.sprite_w
                        - 4,
                    ),
                ),
            ),
        )
        if now >= self.dance_flip:
            self.dance_flip = now + RNG.uniform(0.25, 0.5)
            self._flip(RNG.random() < 0.5)
            typing.cast(typing.Any, self).ground_y = typing.cast(
                typing.Any, self
            ).dance_ground_y - RNG.randint(0, 22)

    def _mood_decay(self):
        self.mood["hunger"] = min(100, self.mood.get("hunger", 35) + 1.5)
        self.mood["happiness"] = max(0, self.mood.get("happiness", 70) - 1)
        self.mood["energy"] = max(0, self.mood.get("energy", 80) - 1)
        if self.state == "sleep":
            self.mood["energy"] = min(100, self.mood.get("energy", 80) + 5)
        save_settings(self.settings, self.mood)

    def _maybe_idle_chatter(self, now: typing.Any):
        if now - self._last_chatter < 45 or self.bubble_visible or self.state == "dance":
            return
        if RNG.random() > 0.002:
            return
        self._last_chatter = now
        hunger = self.mood.get("hunger", 35)
        energy = self.mood.get("energy", 80)
        if hunger >= 75 and RNG.random() < 0.6:
            self.say(RNG.choice(HUNGRY))
        elif energy <= 25 and RNG.random() < 0.5:
            self.say(RNG.choice(SLEEPY))
        elif RNG.random() < 0.5:
            self.say(RNG.choice(HAPPY))

    def say(self, text: typing.Any, tts: bool = True, hold: float = 0.0):
        if self._quitting:
            return
        text = str(text).strip()[:MAX_RESPONSE_CHARS]
        if not text:
            return
        self.bubble_text = text
        self.bubble_typed = 0
        self.bubble_visible = True
        self._refresh_layout()
        self._apply_pos()
        self.bubble_timer.start(22)
        duration = max(2.5, hold or min(8, 2.0 + len(text) * 0.045))
        self.hide_timer.start(int(duration * 1000))
        if tts:
            QTimer.singleShot(350, lambda: self._speak_if_active(text))

    def _speak_if_active(self, text: typing.Any):
        if not self._quitting:
            self.tts.speak(text)

    def _type_char(self):
        self.bubble_typed += 1
        shown = self.bubble_text[: self.bubble_typed]
        if self.bubble_visible:
            self.bubble_label.setText(shown)
        if self.bubble_typed >= len(self.bubble_text):
            self.bubble_timer.stop()

    def _hide_bubble(self):
        self.bubble_visible = False
        self.bubble_timer.stop()
        self.bubble_label.setText("")
        self._refresh_layout()
        self._apply_pos()

    def _on_click(self):
        if self.state == "sleep":
            self._wake()
            self.say("I'm awake! I'm awake!")
            return
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 2)
        self.say(RNG.choice(CLICK_LINES))

    def _on_double_click(self):
        if self.state == "sleep":
            self._wake()
            self.say("Hehe, you woke me up!")
            return
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 8)
        self._bounce()
        self.say(RNG.choice(PET_LINES))

    def _wake(self):
        self.state = "walk"
        typing.cast(typing.Any, self).ground_y = min(
            typing.cast(typing.Any, typing.cast(typing.Any, self).ground_y),
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).bottom() - 8
            ),
        )
        self._set_sprite_direction()
        self._apply_topmost()

    def _bounce(self):
        resting_y: typing.Any = typing.cast(typing.Any, typing.cast(typing.Any, self).ground_y)
        typing.cast(typing.Any, self).ground_y = resting_y - 18
        QTimer.singleShot(180, lambda: setattr(self, "ground_y", resting_y))

    def _feed(self):
        self.mood["hunger"] = max(0, self.mood.get("hunger", 35) - 45)
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 10)
        self.say(RNG.choice(FEED_LINES))
        self._bounce()

    def _play(self):
        self.mood["happiness"] = min(100, self.mood.get("happiness", 70) + 12)
        self.mood["energy"] = max(0, self.mood.get("energy", 80) - 10)
        self.state = "dance"
        typing.cast(typing.Any, self).dance_ground_y = typing.cast(typing.Any, self).ground_y
        self.dance_until = time.monotonic() + 6.0
        self.dance_flip = 0.0
        self.say(RNG.choice(PLAY_LINES))

    def _sleep(self):
        if self.state == "sleep":
            self._wake()
            self.say("I'm awake!")
            return
        self.state = "sleep"
        dim = QPixmap(self.sprite.size())
        dim.fill(typing.cast(typing.Any, typing.cast(typing.Any, Qt).transparent))
        p = QPainter(dim)
        p.setOpacity(0.55)
        p.drawPixmap(0, 0, self.sprite)
        p.end()
        self.sprite_label.setPixmap(dim)
        self.say("Zzz... see you when I wake up.")

    def _ask(self):
        q, ok = QInputDialog.getText(self, "Ask " + self.name, "What's your question?")
        if not ok or not q.strip():
            return
        q = " ".join(q.split())
        if len(q) > MAX_QUESTION_CHARS:
            self.say(
                f"Please keep questions under {MAX_QUESTION_CHARS} characters.",
                tts=False,
            )
            return
        if self.ask_thread and self.ask_thread.isRunning():
            self.say("One thing at a time, I'm still thinking!")
            return
        self.say("Thinking...", tts=False)
        thread = AskThread(self.brain, q, self)
        self.ask_thread = thread
        thread.done.connect(self._ask_done)
        thread.finished.connect(lambda: self._ask_finished(thread))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _ask_done(self, answer: typing.Any):
        if self._quitting:
            return
        if self.brain.last_error:
            answer = f"{answer}\n(Offline fallback: the AI service is unavailable.)"
        self.say(answer or "Hmm, I got nothing. Ask me something else!")

    def _ask_finished(self, thread: typing.Any):
        if self.ask_thread is thread:
            self.ask_thread = None
        if self._quitting:
            self._finish_shutdown()

    def mousePressEvent(self, e: typing.Any):
        if e.button() == typing.cast(typing.Any, Qt).RightButton:
            self._show_menu(e.globalPosition().toPoint())
            return
        if e.button() == typing.cast(typing.Any, Qt).LeftButton:
            self.dragging = True
            self.drag_dx = e.position().x()
            self.drag_dy = e.position().y()
            self.drag_start_global = e.globalPosition()
            self.sprite_label.setCursor(
                typing.cast(typing.Any, typing.cast(typing.Any, Qt).ClosedHandCursor)
            )

    def mouseMoveEvent(self, e: typing.Any):
        if self.dragging:
            g = e.globalPosition()
            self._switch_screen(QGuiApplication.screenAt(g.toPoint()))
            win_h = self.height()
            self.px = g.x() - self.drag_dx
            self.ground_y = g.y() - self.drag_dy + win_h
            self._apply_pos()

    def mouseReleaseEvent(self, e: typing.Any):
        if not self.dragging:
            return
        self.dragging = False
        self.sprite_label.setCursor(
            typing.cast(typing.Any, typing.cast(typing.Any, Qt).OpenHandCursor)
        )
        if self.drag_start_global is None:
            moved = 0
        else:
            delta = e.globalPosition() - self.drag_start_global
            moved = abs(delta.x()) + abs(delta.y())
        self.drag_start_global = None
        if moved < 8:
            self._on_click()

        typing.cast(typing.Any, self).ground_y = min(
            typing.cast(typing.Any, typing.cast(typing.Any, self).ground_y),
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).bottom() - 8
            ),
        )
        typing.cast(typing.Any, self).px = max(
            typing.cast(
                typing.Any, typing.cast(typing.Any, typing.cast(typing.Any, self).geom).left() + 4
            ),
            typing.cast(
                typing.Any,
                min(
                    typing.cast(typing.Any, typing.cast(typing.Any, self).px),
                    typing.cast(
                        typing.Any,
                        typing.cast(typing.Any, typing.cast(typing.Any, self).geom).right()
                        - self.sprite_w
                        - 4,
                    ),
                ),
            ),
        )

    def mouseDoubleClickEvent(self, e: typing.Any):
        if e.button() == typing.cast(typing.Any, Qt).LeftButton:
            self._on_double_click()

    def _show_menu(self, pos: typing.Any):
        m = QMenu(self)
        name = self.name

        def add(label: typing.Any, fn: typing.Any):
            a = QAction(label, m)
            a.triggered.connect(fn)
            m.addAction(a)
            return a

        add(f"🍪 Feed {name}", self._feed)
        add(f"🤗 Pet {name}", self._on_double_click)
        add("🎮 Play", self._play)
        add("💬 Ask…", self._ask)
        m.addSeparator()
        add("😄 Tell a joke", lambda: self.say(RNG.choice(JOKES)))
        add("🧠 Tell a fact", lambda: self.say(RNG.choice(FACTS)))
        add("🕐 What time is it?", lambda: self.say(time.strftime("It's %I:%M %p.")))
        add("🎲 Roll a die", lambda: self.say(f"I rolled a {RNG.randint(1, 6)}!"))
        add("🎵 Sing", lambda: self.say(RNG.choice(SING_SONGS)))
        add("💤 Sleep / Wake", self._sleep)
        m.addSeparator()
        add("⚙️ Settings…", self._open_settings)
        add("📖 About", self._about)
        if self.tray_available:
            add("🚪 Hide to tray", self.hide)
        add("❌ Quit", self._quit)
        m.exec(pos)

    def _open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() == typing.cast(typing.Any, QDialog).Accepted:
            new = dlg.values()
            self.settings.update(new)
            self.name = self.settings.get("name", "owaua")
            self._apply_topmost()
            self.tts.close()
            self.tts = TTS(self.settings)
            if save_settings(self.settings, self.mood):
                self.say("Settings saved! I'm still cute, promise.", tts=True)
            else:
                self.say(
                    "I couldn't save settings securely. Please check the config folder.",
                    tts=False,
                )

    def _about(self):
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME} v{APP_VERSION}</b><br><br>"
            f"A desktop pet that walks, talks, and answers questions.<br>"
            f"Right-click for the menu. Drag to move me around.<br>"
            f"Brain: {self.brain.status}<br>"
            f"Config: {CONFIG_DIR}",
        )

    def _apply_topmost(self):
        flags: typing.Any = typing.cast(
            typing.Any,
            typing.cast(typing.Any, Qt).FramelessWindowHint | typing.cast(typing.Any, Qt).Tool,
        )
        if self.settings.get("topmost", True):
            flags |= typing.cast(typing.Any, Qt).WindowStaysOnTopHint
        self.setWindowFlags(typing.cast(typing.Any, flags))
        self.show()

    def _quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.tick.stop()
        self.mood_timer.stop()
        self.bubble_timer.stop()
        self.hide_timer.stop()
        self.tts.close()
        save_settings(self.settings, self.mood)
        self.hide()
        thread = self.ask_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            self.brain.close()
            return
        self._finish_shutdown()

    def _finish_shutdown(self):
        thread = self.ask_thread
        if thread is not None and thread.isRunning():
            return
        self.brain.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event: typing.Any):
        event.ignore()
        self._quit()


class SettingsDialog(QDialog):
    def __init__(self, settings: typing.Any, parent: typing.Any = None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        form = QFormLayout(self)

        self.name_edit = QLineEdit(settings.get("name", "owaua"))
        self.name_edit.setMaxLength(32)
        form.addRow("Name:", self.name_edit)

        self.tts_box = QCheckBox("Speak out loud")
        self.tts_box.setChecked(settings.get("tts", True))
        form.addRow(self.tts_box)

        self.ai_box = QCheckBox("Use AI brain (when a key is configured)")
        self.ai_box.setChecked(settings.get("ai", True))
        form.addRow(self.ai_box)

        self.walk_box = QCheckBox("Wander around")
        self.walk_box.setChecked(settings.get("walk", True))
        form.addRow(self.walk_box)

        self.topmost_box = QCheckBox("Stay on top of other windows")
        self.topmost_box.setChecked(settings.get("topmost", True))
        form.addRow(self.topmost_box)

        self.pace_combo = QComboBox()
        self.pace_combo.addItems(["Slow", "Normal", "Fast"])
        self.pace_combo.setCurrentText(settings.get("pace", "Normal"))
        form.addRow("Voice pace:", self.pace_combo)

        btns = QDialogButtonBox(
            typing.cast(
                typing.Any,
                typing.cast(typing.Any, QDialogButtonBox).Ok
                | typing.cast(typing.Any, QDialogButtonBox).Cancel,
            )
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self):
        return {
            "name": _clean_name(self.name_edit.text()),
            "tts": self.tts_box.isChecked(),
            "ai": self.ai_box.isChecked(),
            "walk": self.walk_box.isChecked(),
            "topmost": self.topmost_box.isChecked(),
            "pace": self.pace_combo.currentText(),
        }


def _handle_keyring_cli(argv: typing.Any):
    if "--store-ai-key" not in argv:
        return None
    index = argv.index("--store-ai-key")
    key = argv[index + 1] if index + 1 < len(argv) else "OWAUA_AI_KEY"
    if key not in SECRET_ENV_KEYS:
        print(f"Unsupported key name. Choose one of: {', '.join(sorted(SECRET_ENV_KEYS))}")
        return 2
    try:
        value = getpass.getpass(f"Enter {key} (input hidden): ").strip()
        store_keyring_secret(key, value)
    except (EOFError, KeyboardInterrupt):
        print("\nKey was not stored.")
        return 1
    except (RuntimeError, ValueError) as exc:
        print(f"Key was not stored: {exc}")
        return 1
    print(f"Stored {key} in the operating system credential store.")
    return 0


def main():
    if "--version" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0
    _migrate_legacy_user_state()
    keyring_result = _handle_keyring_cli(sys.argv[1:])
    if keyring_result is not None:
        return keyring_result

    env: typing.Any = typing.cast(typing.Any, get_env())
    settings, mood = load_settings()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(load_sprite(64)))

    pet = PetWindow(settings, mood, env)
    pet.show()

    tray = None
    try:
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(QIcon(pet.sprite), app)
            tray.setToolTip(APP_NAME)
            tm = QMenu()
            show_a = QAction("Show / hide", tm)
            show_a.triggered.connect(lambda: pet.show() if pet.isHidden() else pet.hide())
            feed_a = QAction("Feed", tm)
            feed_a.triggered.connect(pet._feed)
            quit_a = QAction("Quit", tm)
            quit_a.triggered.connect(pet._quit)
            tm.addAction(show_a)
            tm.addAction(feed_a)
            tm.addSeparator()
            tm.addAction(quit_a)
            tray.setContextMenu(tm)
            tray.activated.connect(
                typing.cast(
                    typing.Callable[..., typing.Any],
                    typing.cast(
                        typing.Callable[[typing.Any], typing.Any],
                        lambda reason: (
                            pet.show()
                            if reason == typing.cast(typing.Any, QSystemTrayIcon).Trigger
                            else None
                        ),
                    ),
                )
            )
            tray.show()
            pet.tray_available = True
    except Exception:  # noqa: BLE001
        tray = None

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
