import pytest

from mesh.errors import (
    LinkClosedError,
    MalformedPDUError,
    MeshAuthError,
    MeshCryptoError,
    MeshError,
    MeshTimeout,
    NotProvisionedError,
    ProvisioningError,
)


@pytest.mark.parametrize(
    "cls",
    [
        MeshCryptoError,
        MeshAuthError,
        MalformedPDUError,
        MeshTimeout,
        LinkClosedError,
        NotProvisionedError,
    ],
)
def test_all_errors_are_mesh_errors(cls):
    assert issubclass(cls, MeshError)


def test_auth_error_is_crypto_error():
    assert issubclass(MeshAuthError, MeshCryptoError)


def test_mesh_timeout_is_a_timeout():
    assert issubclass(MeshTimeout, TimeoutError)


def test_provisioning_error_carries_step():
    err = ProvisioningError("confirmation", "device confirmation mismatch")
    assert err.step == "confirmation"
    assert str(err) == "confirmation: device confirmation mismatch"
    assert isinstance(err, MeshError)
