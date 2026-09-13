from enum import StrEnum


class IssueStatus(StrEnum):
    TODO = "TODO"
    BLOCKED = "BLOCKED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
