from fastapi import FastAPI

app = FastAPI(title="Smart Business Agent API")


@app.get("/")
async def root():
    return {"status": "ok", "message": "Smart Business Agent API is running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
