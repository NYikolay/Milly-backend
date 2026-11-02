from fastapi import APIRouter

from backend.api.routes import tasks

api_router = APIRouter()
api_router.include_router(tasks.router)
