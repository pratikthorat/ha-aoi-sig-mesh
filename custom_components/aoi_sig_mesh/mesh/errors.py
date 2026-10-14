"""Exception hierarchy for the mesh stack."""


class MeshError(Exception):
    """Base class for every mesh error."""


class MeshCryptoError(MeshError):
    """A cryptographic primitive got bad input."""


class MeshAuthError(MeshCryptoError):
    """A MIC or authentication value did not verify."""


class MalformedPDUError(MeshError):
    """A PDU or message could not be parsed."""


class MeshTimeout(MeshError, TimeoutError):
    """A node did not answer in time."""


class LinkClosedError(MeshError):
    """The proxy link is closed or dropped."""


class NotProvisionedError(MeshError):
    """The operation needs a provisioned node or network."""


class ProvisioningError(MeshError):
    """Provisioning failed at a named step."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
