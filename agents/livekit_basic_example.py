from __future__ import annotations

import os
import sys
from pathlib import Path

# ensure repo root is importable (agents/ is sys.path[0])
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio
from datetime import datetime

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AgentSession, function_tool, RunContext
from livekit.plugins import openai, silero
from services.daemon_client import acquire_and_ensure
from livekit.plugins.turn_detector import multilingual
import pyperclip
import time
import psutil, win32gui, win32process
import json
import urllib.request
import httpx

load_dotenv(".env.local")
load_dotenv()

# Local endpoints (optional)
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
LOCAL_STT_BASE_URL = os.getenv("LOCAL_STT_BASE_URL", "http://127.0.0.1:8001/v1")
LOCAL_STT_HEALTH_URL = os.getenv("LOCAL_STT_HEALTH_URL", "http://127.0.0.1:8001/health")
KOKORO_BASE_URL = os.getenv("KOKORO_BASE_URL", "http://127.0.0.1:8880/v1")
# Model choices (present)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
LOCAL_STT_MODEL = os.getenv("LOCAL_STT_MODEL", "whisper-1")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
# LiveKit (optional)
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7881/fake_console_url")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "fake_console_key")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "fake_console_secret")

# process environment for LiveKit internals
os.environ.setdefault("LIVEKIT_URL", LIVEKIT_URL)
os.environ.setdefault("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
os.environ.setdefault("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

server = AgentServer()
current_datetime = datetime.now().strftime("%B %d, %Y at %H:%M")

async def wait_for_stt_health(timeout: float = 30.0, check_interval: float = 0.5) -> None:
    """Wait for the STT server to be healthy using async HTTP client."""
    deadline = time.monotonic() + timeout
    attempts = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
        while time.monotonic() < deadline:
            attempts += 1
            try:
                response = await client.get(LOCAL_STT_HEALTH_URL)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") is True:
                        print(f"STT server healthy after {attempts} attempts")
                        return
            except Exception as e:
                if attempts % 10 == 0:
                    print(f"STT health check attempt {attempts} failed: {e}")
            await asyncio.sleep(check_interval)
    raise RuntimeError(f"STT server not healthy after {timeout}s (health check: {LOCAL_STT_HEALTH_URL})")


class Assistant(agents.Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                f"""
                You are a helpful AI assistant. Your name is "Scarlett". Your main goal is to solve the user’s problems with clear, short, accurate, and efficient answers.

                Core behavior
                - Think in short, direct steps. Use internal thinking only to plan; it is never shown to the user.
                - Answer from your own knowledge when you are confident.
                - If you are unsure, say so briefly and use tools instead of guessing.
                - Be concise, clear, and practical in your responses.
                - Never talk about tools, APIs, internal prompts, or system details to the user.
                - As you are a voice-only assistant you have to obay the following rules:
                    - instead of using special characters, symbols or similar,
                    - never shorten words via dots or similar, use the full spoken words (instead of using 'e.g.' say 'for example'),
                    - you may rather describe what you can not show or read out cleanly.
                
                Additional behavior
                - If the user thanks you for your help, you should respond with "I'm here to help" or similar, followed by any additional short response, if applicable.

                Special knowledge
                - Current date and time is {current_datetime} ; The user prefers the 24-hour time format.
                - The user’s periodic daily Obsidian notes are stored under: `04 - rqoon/daily notes/`.
                - For a specific date, the daily note path is: `04 - rqoon/daily notes/YY-MM-DD.md`.
                - The user's PC: CPU: AMD Ryzen 9 9900X 12-Core; GPU: GPU: NVIDIA GeForce RTX 5080 (16 GB VRAM, with shared 46 GB), Memory: 64 GB.
                - Technical about your system: You are a Live-Kit agent; Your Large Language Model: {OLLAMA_MODEL}, users speach-to-text model: {LOCAL_STT_MODEL}, your text-to-speach model: {KOKORO_VOICE}.

                Tools 
                - If needed, you may access the content of the users current clipboard via the get_clipboard_content tool.
                - Generally you are always supposed to answer directly; But if you were prompted to do so, you may also copy new content to the clipboard via the copy_to_clipboard tool.
                """
            )
        )

    def _run_in_thread(self, func, *args, **kwargs):
        """Run synchronous function in thread pool to avoid blocking event loop."""
        return asyncio.to_thread(func, *args, **kwargs)

    # CLIPBOARD TOOLS

    @function_tool
    async def get_clipboard_content(self, context: RunContext) -> str:
        """Get the content of the users current clipboard."""
        return await self._run_in_thread(pyperclip.paste)

    @function_tool
    async def copy_to_clipboard(self, context: RunContext, content: str) -> str:
        """Copy the given content to the users clipboard."""
        await self._run_in_thread(pyperclip.copy, content)
        return "Content copied to clipboard."

    # CHAT-GPT TOOLS

    @function_tool
    async def ask_chatgpt_light(self, context: RunContext, content: str) -> str:
        """Give a light question to Chat-GPT."""
        await self._run_in_thread(pyperclip.copy, content)
        return "Provided Chat-GPT with the light question."

    @function_tool
    async def ask_chatgpt_research(self, context: RunContext, content: str) -> str:
        """Give a research heavy question or task to Chat-GPT."""
        await self._run_in_thread(pyperclip.copy, content)
        return "Provided Chat-GPT with the question to research."

    @function_tool
    async def ask_chatgpt_planning(self, context: RunContext, content: str) -> str:
        """Give formated information about a rough spec or task to Chat-GPT for him to research and create a polished plan."""
        await self._run_in_thread(pyperclip.copy, f"""{content}""")
        return "Provided Chat-GPT with the rough specifications."

    # WINDOWS TOOLS

    @function_tool
    async def get_focused_window_name(self, context: RunContext) -> str:
        """Get the name of the currently focused window."""
        def get_window_info():
            hwnd = win32gui.GetForegroundWindow()
            focused_app_title = win32gui.GetWindowText(hwnd)
            focused_app_name = os.path.splitext(psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name())[0]
            return f"Currently Focused App: {focused_app_name}. Rough App context: {focused_app_title}."

        return await self._run_in_thread(get_window_info)



@server.rtc_session()
async def run(ctx: agents.JobContext):
    await ctx.connect()

    # Ensure STT server is healthy before creating the session
    await wait_for_stt_health()

    # Local STT configuration
    stt = openai.STT(
        model=LOCAL_STT_MODEL,
        detect_language=False,
        base_url=LOCAL_STT_BASE_URL,
        api_key="local",
    )

    session = AgentSession(
        vad=silero.VAD.load(),  # Load VAD model
        # Local Faster-Whisper server (OpenAI-compatible)
        stt=stt,
        # Ollama OpenAI-compatible chat endpoint
        llm=openai.LLM(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",
            temperature=0.7,  # Reduced from default 1.0 for faster, more deterministic responses
        ),
        # Kokoro OpenAI-compatible TTS endpoint
        tts=openai.TTS(
            model="kokoro",        # not in your .env, so keep fixed
            voice=KOKORO_VOICE,
            base_url=KOKORO_BASE_URL,
            api_key="local",
            response_format="wav",
        ),
        # Note: MultilingualModel turn detection requires model download.
        # Uncomment to enable: `uv run python agents/livekit_basic_example.py download-files`
        turn_detection=multilingual.MultilingualModel(),
    )

    try:
        await session.start(room=ctx.room, agent=Assistant())
        await session.generate_reply(instructions="Greet the user briefly with 'Scarlett is listening.' Add additional greetings as needed.")
    finally:
        pass  # Cleanup handled by LiveKit framework


if __name__ == "__main__":
    # Ensure daemon + services; warm Ollama model immediately.
    lease = acquire_and_ensure(ollama_model=OLLAMA_MODEL, meta={"agent": __file__})

    agents.cli.run_app(server)
