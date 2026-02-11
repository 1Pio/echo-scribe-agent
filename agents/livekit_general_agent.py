# Run with: uv run python agents/livekit_general_agent.py console

from __future__ import annotations

import os
import sys
from pathlib import Path

# ensure repo root is importable (agents/ is sys.path[0])
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AgentSession, function_tool, RunContext
from livekit.plugins import openai, silero
from services.daemon_client import acquire_and_ensure

from datetime import datetime
import pyperclip
import time
import psutil, win32gui, win32process
import json
import urllib.request
import httpx


from livekit.agents.llm import mcp

DOCKER_MCP_ARGS = [
    "mcp", "gateway", "run",
    "--servers=duckduckgo",
    "--tools=search",          # only expose the tool(s) you want
    # "--transport=stdio",     # optional; stdio is the default in most setups
]

MCP_DOCKER_GATEWAY = mcp.MCPServerStdio(
    command="docker",
    args=DOCKER_MCP_ARGS,
    # Windows note: if your agent runs in a stripped env, forward these so Docker can find its config.
    # (Some clients require LOCALAPPDATA and ProgramFiles on Windows.) :contentReference[oaicite:4]{index=4}
    env={
        **os.environ,
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "ProgramFiles": os.environ.get("ProgramFiles", ""),
    },
)


import asyncio

_mcp_tool_map = None
_mcp_lock = asyncio.Lock()

def _tool_name(tool) -> str:
    # Compatible across LiveKit versions by probing common metadata fields
    for attr in ("__livekit_raw_tool_info", "__livekit_tool_info"):
        info = getattr(tool, attr, None)
        name = getattr(info, "name", None) if info else None
        if name:
            return name
    return getattr(tool, "name", None) or getattr(tool, "__name__", "unknown")

def _mcp_result_to_text(result) -> str:
    # MCP tool results often return { "content": [ {"type":"text","text":"..."} ] }
    if isinstance(result, dict) and "content" in result and isinstance(result["content"], list):
        texts = []
        for part in result["content"]:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text", ""))
        joined = "\n".join(t for t in texts if t).strip()
        if joined:
            return joined
    return json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (dict, list)) else str(result)

async def _get_mcp_tool(tool_name: str):
    global _mcp_tool_map
    async with _mcp_lock:
        if _mcp_tool_map is None:
            await MCP_DOCKER_GATEWAY.initialize()
            tools = await MCP_DOCKER_GATEWAY.list_tools()
            _mcp_tool_map = {_tool_name(t): t for t in tools}
        return _mcp_tool_map[tool_name]






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
# Obsidian Configuration (optional)
OBSIDIAN_JOURNAL_FOLDER = os.getenv("OBSIDIAN_JOURNAL_FOLDER",r"C:\Users\aaron\iCloudDrive\iCloud~md~obsidian\OBSIDIAN-vault\Daily")

# process environment for LiveKit internals
os.environ.setdefault("LIVEKIT_URL", LIVEKIT_URL)
os.environ.setdefault("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
os.environ.setdefault("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)

current_datetime = datetime.now().strftime("%B %d, %Y at %H:%M")
server = AgentServer()


def wait_for_stt_health(timeout: float = 30.0, check_interval: float = 0.5) -> None:
    """Wait for the STT server to be healthy before proceeding."""
    deadline = time.monotonic() + timeout
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            req = urllib.request.Request(LOCAL_STT_HEALTH_URL)
            with urllib.request.urlopen(req, timeout=2.0) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    if data.get("ok") is True:
                        # Verify it's actually working by checking twice with a small delay
                        time.sleep(0.1)
                        try:
                            with urllib.request.urlopen(req, timeout=2.0) as response2:
                                if response2.status == 200:
                                    data2 = json.loads(response2.read().decode("utf-8"))
                                    if data2.get("ok") is True:
                                        print(f"STT server verified healthy after {attempts} attempts")
                                        return
                        except Exception:
                            pass  # Continue waiting if second check fails
        except Exception as e:
            if attempts % 10 == 0:  # Log every 10th attempt
                print(f"STT health check attempt {attempts} failed: {e}")
        time.sleep(check_interval)
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
                - The user writes both journaling and and todo's to his personal daily note.
                - The user's PC: CPU: AMD Ryzen 9 9900X 12-Core; GPU: GPU: NVIDIA GeForce RTX 5080 (16 GB VRAM, with shared 46 GB), Memory: 64 GB.
                - Technical about your system: You are a Live-Kit agent; Your Large Language Model: {OLLAMA_MODEL}, users speach-to-text model: {LOCAL_STT_MODEL}, your text-to-speach model: {KOKORO_VOICE}.

                Tools 
                - If needed, you may access the content of the users current clipboard via the get_clipboard_content tool.
                - Generally you are always supposed to answer directly; But if you were prompted to do so, you may also copy new content to the clipboard via the copy_to_clipboard tool.
                
                Daily Note Editing Rules (ENFORCED)
                - When editing the daily note, you MUST follow this exact workflow:
                  1. First call read_obsidian_daily_note to get the current content
                  2. Make any needed edits (add, remove, or modify sections) using your internal reasoning (icnlude any necessery context and write in a highly readable way)
                  3. Finally call write_to_obsidian_daily_note with the complete modified full_content
                - The write_to_obsidian_daily_note tool overwrites the entire file with the content you provide.
                - Always include all existing content when writing, not just new additions.
                - Use proper Markdown formatting: headings with #, subheadings with ##, task lists with - [ ] for incomplete and - [x] for completed.
                - Never delete anything unless really necessary to add new content, Also never delete Tasks, just mark them complete or not complete.
                
                """
            )
        )



    # DUCK-DUCK-GO (MCP TOOLKIT)

    @function_tool(
    name="search",
    description="Web search via Docker MCP Gateway. Returns concise, readable results."
    )
    async def search_tool(_: RunContext, query: str) -> str:
        mcp_search = await _get_mcp_tool("search")
        # Pass only args your MCP tool supports. Keep it minimal and reliable:
        result = await mcp_search({"query": query})
        return _mcp_result_to_text(result)


    # CLIPBOARD TOOLS

    @function_tool
    async def get_clipboard_content(self, context: RunContext) -> str:
        """Get the content of the users current clipboard."""
        return pyperclip.paste()

    @function_tool
    async def copy_to_clipboard(self, context: RunContext, content: str) -> str:
        """Copy the given content to the users clipboard."""
        pyperclip.copy(content)
        return "Content copied to clipboard."

    # CHAT-GPT TOOLS

    @function_tool
    async def ask_chatgpt_light(self, context: RunContext, content: str) -> str:
        """Give a light question to Chat-GPT."""
        pyperclip.copy(content)
        return "Provided Chat-GPT with the light question."

    @function_tool
    async def ask_chatgpt_research(self, context: RunContext, content: str) -> str:
        """Give a research heavy question or task to Chat-GPT."""
        pyperclip.copy(content)
        return "Provided Chat-GPT with the question to research."

    @function_tool
    async def ask_chatgpt_planning(self, context: RunContext, content: str) -> str:
        """Give formated information about a rough spec or task to Chat-GPT for him to research and create a polished plan."""
        pyperclip.copy(f"""{content}""")
        return "Provided Chat-GPT with the rough specifications."
    
    # WINDOWS TOOLS

    @function_tool
    async def get_focused_window_name(self, context: RunContext) -> str:
        """Get the name of the currently focused window."""
        hwnd = win32gui.GetForegroundWindow()
        focused_app_title = win32gui.GetWindowText(hwnd)
        focused_app_name = os.path.splitext(psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name())[0]
        return f"Currently Focused App: {focused_app_name}. Rough App context: {focused_app_title}."
    
    # OBSIDIAN TOOLS

    @function_tool
    async def read_obsidian_daily_note(self, context: RunContext) -> str:
        """Read the content of today's Obsidian daily note."""
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_daily_note_path = f"{OBSIDIAN_JOURNAL_FOLDER}/{current_date}.md"
        try:
            if Path(current_daily_note_path).exists():
                with open(current_daily_note_path, "r", encoding="utf-8") as f:
                    content = f.read()
                return f"Daily note content for {current_date}:\n{content}"
            else:
                return f"No daily note found for today ({current_date})."
        except Exception as e:
            return f"Error reading daily note: {str(e)}"

    @function_tool
    async def write_to_obsidian_daily_note(self, context: RunContext, full_content: str) -> str:
        """Write the complete content to today's Obsidian daily note, overwriting the entire file."""
        current_date = datetime.now().strftime("%Y-%m-%d")
        current_daily_note_path = f"{OBSIDIAN_JOURNAL_FOLDER}/{current_date}.md"
        try:
            Path(OBSIDIAN_JOURNAL_FOLDER).mkdir(parents=True, exist_ok=True)
            with open(current_daily_note_path, "w", encoding="utf-8") as f:
                f.write(full_content)
            return f"Daily note for {current_date} has been updated with the new content."
        except Exception as e:
            return f"Error writing to daily note: {str(e)}"



@server.rtc_session()
async def run(ctx: agents.JobContext):
    await ctx.connect()

    # Ensure STT server is healthy before creating the session
    wait_for_stt_health()

    # Create HTTP client with longer timeouts for local STT server
    # Local STT processing can take longer than default OpenAI timeouts
    stt_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,  # Connection timeout
            read=120.0,     # Read timeout (2 minutes for transcription)
            write=10.0,     # Write timeout
            pool=10.0,      # Pool timeout
        ),
        limits=httpx.Limits(
            max_keepalive_connections=5,
            max_connections=10,
        ),
    )
    
    # Try to configure STT with custom HTTP client for longer timeouts
    # If http_client parameter is not supported, fall back to default configuration
    stt_http_client_used = False
    try:
        stt = openai.STT(
            model=LOCAL_STT_MODEL,
            detect_language=False,
            base_url=LOCAL_STT_BASE_URL,
            api_key="local",
            http_client=stt_http_client,
        )
        stt_http_client_used = True
        # STT client now owns the HTTP client, clear our reference
        stt_http_client = None
    except TypeError:
        # http_client parameter not supported, use default configuration
        print("Warning: http_client parameter not supported, using default timeouts")
        stt = openai.STT(
            model=LOCAL_STT_MODEL,
            detect_language=False,
            base_url=LOCAL_STT_BASE_URL,
            api_key="local",
        )
    
    session = AgentSession(
        vad=silero.VAD.load(),
        # Local Faster-Whisper server (OpenAI-compatible)
        stt=stt,
        # Ollama OpenAI-compatible chat endpoint
        llm=openai.LLM(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",
        ),
        # Kokoro OpenAI-compatible TTS endpoint
        tts=openai.TTS(
            model="kokoro",        # not in your .env, so keep fixed
            voice=KOKORO_VOICE,
            base_url=KOKORO_BASE_URL,
            api_key="local",
            response_format="wav",
        ),
        # mcp_servers=[MCP_DOCKER_GATEWAY],
    )

    try:
        await session.start(room=ctx.room, agent=Assistant())
        await session.generate_reply(instructions="Greet the user briefly with 'Scarlett is listening.' Add additional greetings as needed.")
    finally:
        # Clean up HTTP client if it was created but not used by STT
        if not stt_http_client_used and stt_http_client is not None:
            try:
                await stt_http_client.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    # Ensure daemon + services; warm Ollama model immediately.
    lease = acquire_and_ensure(ollama_model=OLLAMA_MODEL, meta={"agent": __file__})

    agents.cli.run_app(server)
