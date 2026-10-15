"""The simulator and the integration share one mesh package (see conftest's aliasing)."""

from __future__ import annotations

import sys

import pytest

from custom_components.aoi_sig_mesh.mesh import errors
from tests.sim.mesh_sim import SimMesh


def test_mesh_is_an_alias_of_the_bundled_package() -> None:
    assert sys.modules["mesh.errors"] is errors


async def test_an_error_raised_by_the_sim_is_caught_by_the_integrations_except() -> None:
    transport = SimMesh().connect()
    transport.drop()

    with pytest.raises(errors.LinkClosedError):
        await transport.write(b"\x00")
