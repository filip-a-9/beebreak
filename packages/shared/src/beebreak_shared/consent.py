"""Sharing consent (spec §4), its own module so api and presence can both use it."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

CONSENT_VERSION = 1


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    share_task_category: bool = False
    share_stack_tags: bool = False
    share_time_bucket: bool = False
