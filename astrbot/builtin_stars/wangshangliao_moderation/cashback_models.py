"""Strict request models shared by the integrated settlement page."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=300)


class Selection(Body):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    version: StrictInt = Field(ge=1)


class PriceItem(Selection):
    original_amount: StrictInt = Field(ge=0)
    target_amount: StrictInt = Field(ge=0)
    original_category: Literal["ordinary", "agent"]
    price_rule_version: StrictInt = Field(ge=1)


class PricePreview(Body):
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    items: list[Selection] = Field(min_length=1, max_length=100)
    category: Literal["ordinary", "agent"]


class PriceConfirmation(Body):
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    items: list[PriceItem] = Field(min_length=1, max_length=100)
    category: Literal["ordinary", "agent"]
    rule_version: StrictInt = Field(ge=1)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Payment(Body):
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    operation: Literal["pay", "correct", "exception", "reprice", "assign"]
    items: list[Selection] = Field(min_length=1, max_length=100)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{16,80}$")
    reason: str = ""
    category: Literal["ordinary", "agent"] | None = None
    inviter: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    price_confirmation: PriceConfirmation | None = None
    actual_payment_confirmed: StrictBool = False


class GroupRule(Body):
    id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    bot_id: str = Field(min_length=1, max_length=100)
    contact: str = Field(min_length=1, max_length=200)
    rate: StrictInt = Field(ge=1, le=99999999)
    agent_rate: StrictInt | None = Field(default=None, ge=1, le=99999999)
    qualification_seconds: Literal[10800] = 10800
    require_speech: StrictBool = False
    agent_only: StrictBool = False
    enabled: StrictBool
    version: StrictInt = Field(ge=0)

    @field_validator("contact")
    @classmethod
    def validate_contact(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("Contact must be nonempty single-line text")
        return value


class RiskRelease(Body):
    group: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    inviter: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    reason: str = Field(min_length=2, max_length=300)


class ClientError(Body):
    kind: Literal["script_error", "unhandled_rejection", "network_error"]
