"""Contract tests for the application-layer ports."""

from __future__ import annotations

import inspect

from backend.application.ports import auth as auth_ports
from backend.application.ports import collaborators
from backend.application.ports import task_management as ports
from backend.application.ports.unit_of_work import UnitOfWork


def _methods(protocol: type) -> set[str]:
    """Return the public method names declared by a protocol, including inherited ones."""
    return {name for name in dir(protocol) if not name.startswith("_")}


ALL_AGGREGATE_METHODS = frozenset(
    {
        "list_palette_colors",
        "list_active_palette_colors",
        "get_palette_color",
        "get_palette_colors_by_ids",
        "get_default_palette_color",
        "count_active_palette_colors",
        "palette_color_is_used",
        "list_projects",
        "get_project",
        "project_name_exists",
        "add_project",
        "count_projects",
        "project_progress",
        "project_progress_for_projects",
        "today_progress",
        "soft_delete_project",
        "list_labels",
        "get_label",
        "get_labels_by_ids",
        "label_name_exists",
        "add_label",
        "soft_delete_label",
        "planned_tasks_count",
        "get_user_timezone",
        "get_telegram_id",
        "list_tasks",
        "list_task_labels_for_tasks",
        "list_task_reminders_for_tasks",
        "get_projects_by_ids",
        "get_task",
        "add_task",
        "add_tasks",
        "soft_delete_task",
        "hard_delete_tasks",
        "add_task_labels",
        "add_task_label_rows",
        "replace_task_labels",
        "list_task_labels",
        "list_task_reminders",
        "list_series_reminders",
        "add_reminder",
        "add_reminders",
        "delete_task_reminders",
        "delete_series_reminders",
        "cancel_task_reminders",
        "cancel_series_reminders",
        "pause_project_reminders",
        "resume_project_reminders",
        "project_is_active",
        "list_series",
        "get_series",
        "get_series_by_ids",
        "add_series",
        "soft_delete_series",
        "add_series_labels",
        "replace_series_labels",
        "list_series_labels",
        "list_series_for_generation",
        "claim_series_for_generation",
        "get_series_tasks_for_update",
        "series_due_slot_exists",
        "list_future_series_tasks",
        "list_series_due_times",
        "list_due_reminders",
        "claim_due_reminder",
        "update_reminder_next_run_at",
        "get_delivery",
        "add_delivery",
        "mark_delivery_sending",
        "mark_delivery_sent",
        "mark_delivery_pending",
        "mark_delivery_failed",
        "cancel_delivery",
        "reactivate_delivery",
        "commit",
        "rollback",
        "flush",
    }
)

SAVE_METHODS = frozenset(
    {
        "save_project",
        "save_label",
        "save_task",
        "save_series",
        "save_reminder",
        "save_delivery",
    }
)

AGGREGATE_PROTOCOLS = {
    "PaletteRepository": ports.PaletteRepository,
    "AccountRepository": ports.AccountRepository,
    "ProjectRepository": ports.ProjectRepository,
    "LabelRepository": ports.LabelRepository,
    "TaskRepository": ports.TaskRepository,
    "SeriesRepository": ports.SeriesRepository,
    "ReminderRepository": ports.ReminderRepository,
}

AGGREGATE_METHODS = {
    "PaletteRepository": {
        "list_palette_colors",
        "list_active_palette_colors",
        "get_palette_color",
        "get_palette_colors_by_ids",
        "get_default_palette_color",
        "count_active_palette_colors",
        "palette_color_is_used",
    },
    "AccountRepository": {
        "get_user_timezone",
        "get_telegram_id",
    },
    "ProjectRepository": {
        "list_projects",
        "get_project",
        "get_projects_by_ids",
        "project_name_exists",
        "add_project",
        "save_project",
        "count_projects",
        "project_progress",
        "project_progress_for_projects",
        "today_progress",
        "planned_tasks_count",
        "project_is_active",
        "soft_delete_project",
    },
    "LabelRepository": {
        "list_labels",
        "get_label",
        "get_labels_by_ids",
        "label_name_exists",
        "add_label",
        "save_label",
        "soft_delete_label",
    },
    "TaskRepository": {
        "list_tasks",
        "get_task",
        "add_task",
        "add_tasks",
        "save_task",
        "soft_delete_task",
        "hard_delete_tasks",
        "list_task_labels_for_tasks",
        "list_task_reminders_for_tasks",
        "add_task_labels",
        "add_task_label_rows",
        "replace_task_labels",
        "list_task_labels",
        "list_future_series_tasks",
    },
    "SeriesRepository": {
        "list_series",
        "get_series",
        "get_series_by_ids",
        "add_series",
        "save_series",
        "soft_delete_series",
        "add_series_labels",
        "replace_series_labels",
        "list_series_labels",
        "list_series_for_generation",
        "claim_series_for_generation",
        "get_series_tasks_for_update",
        "series_due_slot_exists",
        "list_series_due_times",
    },
    "ReminderRepository": {
        "list_task_reminders",
        "list_series_reminders",
        "add_reminder",
        "add_reminders",
        "save_reminder",
        "delete_task_reminders",
        "delete_series_reminders",
        "cancel_task_reminders",
        "cancel_series_reminders",
        "pause_project_reminders",
        "resume_project_reminders",
        "list_due_reminders",
        "claim_due_reminder",
        "update_reminder_next_run_at",
        "get_delivery",
        "add_delivery",
        "save_delivery",
        "mark_delivery_sending",
        "mark_delivery_sent",
        "mark_delivery_pending",
        "mark_delivery_failed",
        "cancel_delivery",
        "reactivate_delivery",
    },
}

COLLABORATOR_METHODS = {
    "ProjectReader": {
        "get_project",
        "project_is_active",
        "get_projects_by_ids",
        "today_progress",
    },
    "LabelReader": {"get_label", "get_labels_by_ids"},
    "TaskReader": {"get_task"},
    "SeriesReader": {"get_series"},
    "SeriesOperations": {
        "get",
        "get_detail",
        "create_from_task",
        "delete",
        "update",
        "detach_task",
    },
    "PalettePolicy": {
        "get_color",
        "get_colors",
        "resolve_for_write",
        "ensure_active",
        "ensure_can_deactivate",
        "ensure_can_delete",
    },
    "ReminderSender": {"send"},
}

AUTH_REPOSITORY_METHODS = {
    "acquire_telegram_lock",
    "get_identity_by_telegram_id",
    "create_user_with_profile",
    "update_profile",
    "revoke_user_sessions",
    "create_session",
    "get_telegram_id_for_session",
    "get_session_for_update",
    "revoke_session",
    "revoke_family",
    "get_user_with_profile",
    "update_user_timezone",
}


def test_aggregate_protocols_expose_expected_methods() -> None:
    """The fat repository is split into aggregate-specific contracts."""
    assert "list_palette_colors" in _methods(ports.PaletteRepository)
    assert "save_project" in _methods(ports.ProjectRepository)
    assert "save_label" in _methods(ports.LabelRepository)
    assert "save_task" in _methods(ports.TaskRepository)
    assert "claim_series_for_generation" in _methods(ports.SeriesRepository)
    assert "claim_due_reminder" in _methods(ports.ReminderRepository)
    assert _methods(UnitOfWork) >= {"flush", "commit", "rollback"}


def test_aggregate_protocols_declare_exactly_the_spec_methods() -> None:
    """Each aggregate protocol owns exactly its documented method set."""
    for name, expected in AGGREGATE_METHODS.items():
        assert _methods(AGGREGATE_PROTOCOLS[name]) == expected


def test_aggregate_protocols_partition_the_legacy_contract_without_duplicates() -> None:
    """Every legacy fat-port method belongs to exactly one aggregate protocol."""
    declared = {name: _methods(protocol) for name, protocol in AGGREGATE_PROTOCOLS.items()}
    for left_name, left_methods in declared.items():
        for right_name, right_methods in declared.items():
            if left_name >= right_name:
                continue
            assert left_methods.isdisjoint(right_methods), (
                f"{left_name} and {right_name} share {sorted(left_methods & right_methods)}"
            )

    aggregate_union = set().union(*declared.values())
    assert aggregate_union | _methods(UnitOfWork) == (ALL_AGGREGATE_METHODS | SAVE_METHODS)


def test_unit_of_work_is_reexported_by_task_management_and_auth_ports() -> None:
    """Both port modules expose the shared transaction boundary contract."""
    assert ports.UnitOfWork is UnitOfWork
    assert auth_ports.UnitOfWork is UnitOfWork


def test_collaborator_protocols_expose_expected_methods() -> None:
    """Collaborator ports stay narrow and replace concrete service dependencies."""
    for name, expected in COLLABORATOR_METHODS.items():
        assert _methods(getattr(collaborators, name)) == expected


def test_auth_repository_uses_the_final_identity_contract() -> None:
    """Auth persistence exposes identity operations and no commit-on-error helpers."""
    methods = _methods(auth_ports.AuthRepository)
    assert methods == AUTH_REPOSITORY_METHODS
    assert "revoke_session_and_commit" not in methods
    assert "revoke_family_and_commit" not in methods
    assert "get_profile_by_telegram_id" not in methods


def test_auth_collaborator_protocols_expose_expected_methods() -> None:
    """Auth validators and token issuers are explicit narrow ports."""
    assert _methods(auth_ports.InitDataValidator) == {"validate"}
    assert _methods(auth_ports.TokenIssuer) == {"issue_access_token"}


def test_every_port_method_documents_its_contract() -> None:
    """Every protocol method carries an English contract docstring."""
    protocols = [
        *AGGREGATE_PROTOCOLS.values(),
        UnitOfWork,
        collaborators.ProjectReader,
        collaborators.LabelReader,
        collaborators.TaskReader,
        collaborators.SeriesReader,
        collaborators.SeriesOperations,
        collaborators.PalettePolicy,
        collaborators.ReminderSender,
        auth_ports.AuthRepository,
        auth_ports.InitDataValidator,
        auth_ports.TokenIssuer,
    ]
    for protocol in protocols:
        for name in _methods(protocol):
            method = getattr(protocol, name)
            assert inspect.getdoc(method), f"{protocol.__name__}.{name} lacks a docstring"
