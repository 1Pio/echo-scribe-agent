# services/local_stt_server.py
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# --- MUST run before importing ctranslate2 / faster_whisper on Windows ---
def _add_windows_cuda_dll_dirs() -> None:
    if sys.platform != "win32":
        return

    dirs: list[Path] = []

    # venv site-packages layout used by pip/uv on Windows
    sp = Path(sys.prefix) / "Lib" / "site-packages"
    dirs += [
        sp / "nvidia" / "cudnn" / "bin",
        sp / "nvidia" / "cublas" / "bin",
        sp / "nvidia" / "cuda_runtime" / "bin",
        sp / "nvidia" / "cuda_nvrtc" / "bin",
    ]

    # If user has CUDA Toolkit installed, this often exists too
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        dirs.append(Path(cuda_path) / "bin")

    # Add any existing dirs to the DLL search path AND PATH (belt + suspenders)
    for d in dirs:
        if d.exists():
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

_add_windows_cuda_dll_dirs()

# Optional: make CT2 log more when debugging:
# IMPORTANT: set before importing ctranslate2 (per CT2 docs) :contentReference[oaicite:4]{index=4}
# os.environ.setdefault("CT2_VERBOSE", "1")

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
import ctranslate2 as ct

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
DEVICE = os.getenv("WHISPER_DEVICE", "cuda")  # "cuda" or "cpu"
REQUESTED = os.getenv("WHISPER_COMPUTE", "int8_float16")

CT_DEVICE = "cuda" if DEVICE.startswith("cuda") else "cpu"
supported = ct.get_supported_compute_types(CT_DEVICE)

if CT_DEVICE == "cuda":
    priority = [REQUESTED, "int8_float16", "float16", "bfloat16", "float32"]
else:
    priority = [REQUESTED, "int8_float32", "int8", "float32"]

COMPUTE_TYPE = next((t for t in priority if t in supported), "float32")
print(f"[whisper] device={CT_DEVICE} supported={supported} using compute_type={COMPUTE_TYPE}")

model = WhisperModel(WHISPER_MODEL, device=CT_DEVICE, compute_type=COMPUTE_TYPE)

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True, "device": CT_DEVICE, "compute_type": COMPUTE_TYPE, "model": WHISPER_MODEL}

@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model_name: str = Form("whisper-1"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    temperature: float = Form(0.0),
):
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        segments, _info = model.transcribe(
            tmp_path,
            language=language,
            initial_prompt=prompt,
            temperature=temperature,
            beam_size=1,
            best_of=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments).strip()
        return JSONResponse({"text": text})

    except Exception as e:
        # Don’t hard-crash the server; return a usable error to the client
        msg = str(e)
        return JSONResponse(
            status_code=500,
            content={"error": "stt_failed", "detail": msg},
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
