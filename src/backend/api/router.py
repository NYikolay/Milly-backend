from backend.api.errors.openapi import StandardizedAPIRouter
from backend.api.routes import auth, color_palette, labels, projects, task_series, tasks

api_router = StandardizedAPIRouter()
api_router.include_router(auth.router)
api_router.include_router(color_palette.router)
api_router.include_router(projects.router)
api_router.include_router(labels.router)
api_router.include_router(tasks.router)
api_router.include_router(task_series.router)
