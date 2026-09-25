# pl8-base

`pl8-base` is the data layer for [PL8](https://github.com/dchenstealth/pl8-docs),
a lightweight issue tracker for AI agents and the people working alongside
them, backed by DynamoDB. It's a Python library, not a service: it defines
PL8's entities (`Issue`, `Space`, `IssueBlocker`, `IssueComment`), its
events, and the `BasePL8` manager that reads and writes them against a
DynamoDB table. It's the source of truth for PL8's data model, consumed by
the Lambda functions that actually run PL8 in AWS.

**To use PL8, you don't need this library.** Deploy PL8 to your own AWS
account and use it through [`pl8-cli`](https://github.com/dchenstealth/pl8-cli);
see the [setup](https://github.com/dchenstealth/pl8-docs/blob/main/user_docs/setup.md)
and [usage](https://github.com/dchenstealth/pl8-docs/blob/main/user_docs/usage.md)
guides. `pl8-base` is for working on PL8 itself.

> **Status:** alpha (0.0.x). Interfaces may change between releases.

## Getting started

### Install

```bash
pip install pl8-base
```

or with [uv](https://docs.astral.sh/uv/):

```bash
uv add pl8-base
```

### Basic usage

`BasePL8` wraps a `boto3` DynamoDB client and a structured logger. Error
logging passes arbitrary keyword args through to be merged into the log
record (e.g. `self.logger.error(msg, item=item)`), which a stdlib
`logging.Logger` rejects — pass an
[`aws-lambda-powertools`](https://docs.powertools.aws.dev/lambda/python/latest/)
`Logger` instead (`pip install aws-lambda-powertools` — it's not a dependency
of this package, since only your logger instance needs it, not `pl8-base`
itself). It also expects a table already provisioned with `PK`/`SK` and a
`GSI1` global secondary index (`GSI1PK`/`GSI1SK`) — see
[Deploying](#deploying) below.

```python
import boto3
from aws_lambda_powertools import Logger

from pl8_base.manager import BasePL8
from pl8_base.types import IssueStatus

pl8 = BasePL8(
    dynamodb_client=boto3.client("dynamodb"),
    table_name="pl8",
    logger=Logger(),
)

space = pl8.create_space(
    space_id="eng",
    name="Engineering",
    description="Issues for the engineering team",
    creator="alice",
)

issue = pl8.create_issue(
    space_id="eng",
    title="Fix login bug",
    description="Users can't log in on Safari",
    status=IssueStatus.TODO,
    creator="alice",
)

pl8.transition_issue(space_id="eng", issue_id=issue.issue_id,
                     status=IssueStatus.IN_PROGRESS)

pl8.create_issue_comment(
    space_id="eng",
    issue_id=issue.issue_id,
    body="Reproduced on Safari 17. Looks like the cookie SameSite attr.",
    creator="alice",
)
```

### Rules

`BasePL8` enforces PL8's entity rules and raises an error from
`pl8_base.errors` when a call breaks one:

| Rule | Error |
| --- | --- |
| Create a Space before creating Issues in it | `DDBMissingError` |
| Delete a Space's Issues before deleting the Space | `DDBSpaceNotEmptyError` |
| `DONE` is final: an Issue can't move from `DONE` to any other status | `DDBTerminalStatusError` |
| An Issue with unfinished blockers can't leave `BLOCKED` | `DDBStillBlockedError` |
| A `DONE` Issue can't be added as a blocker | `DDBBlockingIssueDoneError` |
| A write passed `version=` fails if the item has changed since | `DDBVersionConflictError` |

Retrying one of these unchanged won't help. `DDBTransactionConflictError` is
different: it means contention that `BasePL8` already retried, nothing was
applied, and the same call can be retried. The full rules are in
[Entities](https://github.com/dchenstealth/pl8-docs/blob/main/architecture/entities.md).

Some updates happen in the background rather than in the call that caused
them, such as moving a blocked Issue back to `TODO` when its last blocker
finishes. The `handle_*` methods apply them, driven by the DynamoDB stream
and events that pl8-services wires up; see
[Events](https://github.com/dchenstealth/pl8-docs/blob/main/architecture/backend/events.md).

One thing `BasePL8` deliberately does not enforce is `creator`. It is a label
the caller supplies, recorded as given: `pl8-base` has no user model, so it
never checks one against the invoking IAM principal and never uses one to
allow or refuse an operation.

## Deploying

`pl8-base` only talks to a DynamoDB table you already have; it doesn't
provision or run anything in AWS itself.
[pl8-services](https://github.com/dchenstealth/pl8-services) provisions the
table, event bus and queues, and runs the Lambdas that use this library. See
the [setup guide](https://github.com/dchenstealth/pl8-docs/blob/main/user_docs/setup.md)
to deploy it.

## Repositories

| Repo | What it is |
| --- | --- |
| [pl8-docs](https://github.com/dchenstealth/pl8-docs) | User docs and architecture docs |
| [pl8-services](https://github.com/dchenstealth/pl8-services) | OpenTofu infrastructure and Lambda code you deploy |
| [pl8-cli](https://github.com/dchenstealth/pl8-cli) | The `pl8` command line ([PyPI](https://pypi.org/project/pl8-cli/)) |
| [pl8-base](https://github.com/dchenstealth/pl8-base) | This repo: Python data layer and source of truth for the data model ([PyPI](https://pypi.org/project/pl8-base/)) |

## Contributing

See [CONTRIBUTING.md](https://github.com/dchenstealth/pl8-base/blob/main/CONTRIBUTING.md).

## License

[MIT](https://github.com/dchenstealth/pl8-base/blob/main/LICENSE)
