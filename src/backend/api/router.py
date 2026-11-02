from backend.api.errors.openapi import StandardizedAPIRouter
from backend.api.routes import auth, tasks

api_router = StandardizedAPIRouter()
api_router.include_router(auth.router)
api_router.include_router(tasks.router)
