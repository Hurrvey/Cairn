import pytest
from pydantic import ValidationError

from cairn.core.config import MCPSettings
from cairn.core.mcp_management import ManagedMCPConfig


@pytest.mark.parametrize(
    "values",
    [
        {"port_min": 8090, "port_max": 8081},
        {"port_min": 8081, "port_max": 9000},
        {"port_min": 8099, "port_max": 8100},
    ],
)
def test_invalid_published_range_is_rejected(values: dict) -> None:
    with pytest.raises(ValidationError):
        MCPSettings(**values)


def test_managed_config_rejects_unbounded_lists() -> None:
    with pytest.raises(ValidationError):
        ManagedMCPConfig(allowed_hosts=["*"])
    with pytest.raises(ValidationError):
        ManagedMCPConfig(allowed_origins=["https://host"] * 33)
