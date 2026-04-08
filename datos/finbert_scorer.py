#!/home/hproano/asistente_env/bin/python
"""
FinBERT Scorer — Análisis de sentimiento financiero.
Corre en jarvis-power (192.168.208.80) puerto 8002.
Recibe noticias por activo y retorna scores -2 a +2.
"""
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import pipeline
import uvicorn
from typing import List

app = FastAPI()
_pipe = None

def get_pipeline():
    global _pipe
    if _pipe is None:
        _pipe = pipeline("text-classification",
                        model="ProsusAI/finbert",
                        top_k=None)
    return _pipe

class NoticiaRequest(BaseModel):
    simbolo: str
    noticias: List[str]

class ScoreResponse(BaseModel):
    simbolo: str
    score: int  # -2 a +2
    detalle: str

@app.get("/health")
def health():
    return {"status": "ok", "modelo": "ProsusAI/finbert"}

@app.post("/score", response_model=ScoreResponse)
def score_noticias(req: NoticiaRequest):
    if not req.noticias:
        return ScoreResponse(simbolo=req.simbolo, score=0, detalle="sin noticias")
    pipe = get_pipeline()
    scores = []
    for noticia in req.noticias[:5]:  # max 5 noticias
        try:
            resultado = pipe(noticia[:512])[0]
            for r in resultado:
                if r['label'] == 'positive':
                    scores.append(r['score'])
                elif r['label'] == 'negative':
                    scores.append(-r['score'])
        except Exception:
            pass
    if not scores:
        return ScoreResponse(simbolo=req.simbolo, score=0, detalle="error procesando")
    avg = sum(scores) / len(scores)
    if avg > 0.6:
        score = 2
    elif avg > 0.2:
        score = 1
    elif avg < -0.6:
        score = -2
    elif avg < -0.2:
        score = -1
    else:
        score = 0
    return ScoreResponse(
        simbolo=req.simbolo,
        score=score,
        detalle=f"avg={avg:.2f} sobre {len(scores)} noticias"
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
