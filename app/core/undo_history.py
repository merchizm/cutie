from __future__ import annotations

import copy
from collections.abc import Iterable
from pathlib import Path

from app.core.commands import StateCommand
from app.core.project_state import ProjectState


HistoryEntry = ProjectState | StateCommand


class UndoHistory:
    def __init__(self, limit: int = 50) -> None:
        self.limit = max(limit, 1)
        self._undo_stack: list[HistoryEntry] = []
        self._redo_stack: list[HistoryEntry] = []

    @property
    def undo_stack(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._undo_stack)

    @property
    def redo_stack(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._redo_stack)

    def states(self) -> Iterable[ProjectState]:
        yield from (entry for entry in self._undo_stack if isinstance(entry, ProjectState))
        yield from (entry for entry in self._redo_stack if isinstance(entry, ProjectState))

    def referenced_paths(self) -> set[Path]:
        paths: set[Path] = set()
        for entry in (*self._undo_stack, *self._redo_stack):
            referenced_paths = getattr(entry, "referenced_paths", None)
            if callable(referenced_paths):
                paths.update(referenced_paths())
        return paths

    def push(self, state: ProjectState) -> None:
        self._undo_stack.append(copy.deepcopy(state))
        if len(self._undo_stack) > self.limit:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def execute(self, state: ProjectState, command: StateCommand) -> bool:
        if not command.execute(state):
            return False
        self._undo_stack.append(command)
        if len(self._undo_stack) > self.limit:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        return True

    def discard_latest(self) -> None:
        if self._undo_stack:
            self._undo_stack.pop()

    def undo(self, current: ProjectState) -> ProjectState | None:
        if not self._undo_stack:
            return None
        entry = self._undo_stack.pop()
        if isinstance(entry, ProjectState):
            self._redo_stack.append(copy.deepcopy(current))
            return entry
        entry.undo(current)
        self._redo_stack.append(entry)
        return current

    def redo(self, current: ProjectState) -> ProjectState | None:
        if not self._redo_stack:
            return None
        entry = self._redo_stack.pop()
        if isinstance(entry, ProjectState):
            self._undo_stack.append(copy.deepcopy(current))
            if len(self._undo_stack) > self.limit:
                self._undo_stack.pop(0)
            return entry
        entry.execute(current)
        self._undo_stack.append(entry)
        if len(self._undo_stack) > self.limit:
            self._undo_stack.pop(0)
        return current
