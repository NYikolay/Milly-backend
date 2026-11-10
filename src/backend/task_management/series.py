"""Compatibility re-exports of the series application service and DTOs."""

from backend.application.task_management.series import SeriesService as SeriesService
from backend.domain.task_management.models import UNSET as UNSET
from backend.domain.task_management.models import SeriesDetail as SeriesDetail
from backend.domain.task_management.models import UnsetType as UnsetType

__all__ = ["SeriesDetail", "SeriesService", "UNSET", "UnsetType"]
