from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.control import router as control_router
from app.api.destinations import router as destinations_router
from app.api.health import router as health_router
from app.api.music import router as music_router
from app.api.tasks import router as tasks_router
from app.api.voice import router as voice_router
from app.config import settings
from app.repositories.database import Base, engine
from app.services.voice_service import clean_stale_audio_files


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup actions
    settings.init_directories()
    Base.metadata.create_all(bind=engine)
    clean_stale_audio_files()
    yield
    # Shutdown actions
    clean_stale_audio_files()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="MyYaris Navigation Backend - Dispatches destinations to Ottocast P3 Pro",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(destinations_router)
app.include_router(tasks_router)
app.include_router(voice_router)
app.include_router(music_router)
app.include_router(control_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)
