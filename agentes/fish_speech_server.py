#!/home/hproano/asistente_env/bin/python
"""
Fish Speech TTS Server — Voz clonada de JARVIS.
Usa el modelo Fish Speech 1.5 con la referencia jarvis_referencia.wav.
Endpoint: POST /synthesize {text: "..."} → audio WAV
Health: GET /health
Puerto: 8080
"""

import os
import sys
import io
import time
import logging
import queue
import threading

import numpy as np
import torch
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel
import uvicorn

# ── Paths ──
PROYECTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FISH_REPO = "/home/hproano/fish-speech"
MODEL_PATH = os.path.join(PROYECTO, "modelos", "fish-speech-1.5")
REFERENCIA = os.path.join(PROYECTO, "audios", "jarvis_referencia.wav")

# Add fish-speech repo to path for its internal imports
sys.path.insert(0, FISH_REPO)

logging.basicConfig(format="%(asctime)s [%(name)s] %(message)s", level=logging.INFO)
log = logging.getLogger("fish-tts")

app = FastAPI(title="JARVIS Fish Speech TTS")


# ── Model loading ──

_engine = None
_ready = threading.Event()


class SynthRequest(BaseModel):
    text: str
    temperature: float = 0.7
    top_p: float = 0.8


def _load_models():
    """Load Fish Speech 1.5 models (llama + vqgan decoder)."""
    global _engine

    from tools.llama.generate import launch_thread_safe_queue
    from tools.vqgan.inference import load_model as load_decoder
    from tools.inference_engine import TTSInferenceEngine

    precision = torch.bfloat16

    log.info(f"Loading llama from {MODEL_PATH}...")
    llama_queue = launch_thread_safe_queue(
        checkpoint_path=MODEL_PATH,
        device="cuda",
        precision=precision,
        compile=False,
    )

    decoder_path = os.path.join(MODEL_PATH, "firefly-gan-vq-fsq-8x1024-21hz-generator.pth")
    log.info(f"Loading decoder from {decoder_path}...")
    decoder = load_decoder(
        config_name="firefly_gan_vq",
        checkpoint_path=decoder_path,
        device="cuda",
    )

    _engine = TTSInferenceEngine(
        llama_queue=llama_queue,
        decoder_model=decoder,
        precision=precision,
        compile=False,
    )

    _ready.set()
    log.info("Fish Speech TTS ready.")


# ── Endpoints ──

@app.on_event("startup")
async def startup():
    threading.Thread(target=_load_models, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ready" if _ready.is_set() else "loading",
            "model": "fish-speech-1.5", "reference": os.path.exists(REFERENCIA)}


@app.post("/synthesize")
def synthesize(req: SynthRequest):
    if not _ready.is_set():
        return Response(content="Model loading...", status_code=503)

    from tools.schema import ServeTTSRequest, ServeReferenceAudio

    # Build request with reference
    references = []
    if os.path.exists(REFERENCIA):
        ref_bytes = open(REFERENCIA, "rb").read()
        references = [ServeReferenceAudio(audio=ref_bytes, text="")]

    tts_req = ServeTTSRequest(
        text=req.text,
        references=references,
        format="wav",
        chunk_length=200,
        temperature=req.temperature,
        top_p=req.top_p,
        repetition_penalty=1.1,
        max_new_tokens=1024,
    )

    # Generate
    audio_chunks = []
    sample_rate = 44100

    for result in _engine.inference(tts_req):
        if result.code == "header":
            if isinstance(result.audio, tuple):
                sample_rate = result.audio[0]
        elif result.code in ("segment", "final"):
            if isinstance(result.audio, tuple):
                audio_chunks.append(result.audio[1])

    if not audio_chunks:
        return Response(content="No audio generated", status_code=500)

    audio = np.concatenate(audio_chunks)

    # Encode to WAV in memory
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    buf.seek(0)

    return Response(content=buf.read(), media_type="audio/wav",
                    headers={"X-Sample-Rate": str(sample_rate)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
