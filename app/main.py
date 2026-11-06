from fastapi import FastAPI

app = FastAPI(title="Transfer Core")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}