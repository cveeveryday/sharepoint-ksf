"""
Base connector interface.
Every API connector (Entra ID, Meraki, SendGrid, NinjaOne, …) must subclass BaseConnector.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass
class ConnectorResult:
    """Normalised row returned by any connector. Maps to a SharePoint list item."""

    # The dict that will become the SharePoint list item fields.
    fields: dict[str, Any] = field(default_factory=dict)

    # Optional: a stable unique key used to detect existing items (upsert logic).
    unique_key: str | None = None

    def __post_init__(self):
        if self.unique_key and "UniqueKey" not in self.fields:
            self.fields["UniqueKey"] = self.unique_key


@dataclass
class ConnectorSchema:
    """
    Describes the SharePoint columns that this connector produces.
    field_name → SharePoint field type string (e.g. "Text", "Number", "DateTime").
    """

    list_name: str
    fields: dict[str, str]  # {column_name: sp_type}


class BaseConnector(ABC):
    """
    Abstract base for all data-source connectors.

    Subclasses must implement:
        - schema()      → ConnectorSchema
        - fetch()       → Iterator[ConnectorResult]

    Optionally override:
        - authenticate()  called once before fetch(); default is a no-op.
        - batch_size      how many items to upsert per SharePoint request.
    """

    batch_size: int = 100

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)

    # ------------------------------------------------------------------ #
    # Must implement                                                       #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def schema(self) -> ConnectorSchema:
        """Return the SharePoint list schema for this connector."""

    @abstractmethod
    def fetch(self) -> Iterator[ConnectorResult]:
        """Yield ConnectorResult objects from the upstream API."""

    # ------------------------------------------------------------------ #
    # Optional overrides                                                   #
    # ------------------------------------------------------------------ #

    def authenticate(self) -> None:
        """Perform any pre-fetch authentication. Called automatically by the runner."""

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def batched_fetch(self) -> Iterator[list[ConnectorResult]]:
        """Yield rows in batches of self.batch_size."""
        batch: list[ConnectorResult] = []
        for item in self.fetch():
            batch.append(item)
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
