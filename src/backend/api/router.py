from backend.api.errors.openapi import StandardizedAPIRouter
from backend.api.routes import auth

api_router = StandardizedAPIRouter()
api_router.include_router(auth.router)
