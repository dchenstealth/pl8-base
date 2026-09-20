# pl8-base

`pl8-base` is the data layer for PL8, a lightweight issue tracker backed by
DynamoDB. It's a Python library, not a service: it defines PL8's entities
(`Issue`, `Space`, `IssueBlocker`), its events, and the `BasePL8` manager
that reads and writes them against a DynamoDB table. It's the source of
truth for PL8's data model, consumed by the Lambda functions that actually
run PL8 in AWS.

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
)

issue = pl8.create_issue(
    space_id="eng",
    title="Fix login bug",
    description="Users can't log in on Safari",
    status=IssueStatus.TODO,
)

pl8.transition_issue(space_id="eng", issue_id=issue.issue_id,
                     status=IssueStatus.IN_PROGRESS)
```

## Deploying

`pl8-base` only talks to a DynamoDB table you already have; it doesn't
provision or run anything in AWS itself. Work is underway to create other
repos to scaffold this.
