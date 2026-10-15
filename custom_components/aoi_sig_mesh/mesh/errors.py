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


class NoMeshLink(LinkClosedError):
    """There was no proxy link to send on, so nothing was sent."""


class SeqNotSaved(MeshError):
    """Sequence numbers are reserved but not saved yet; try again shortly."""


class NotProvisionedError(MeshError):
    """The operation needs a provisioned node or network."""


class ProvisioningError(MeshError):
    """Provisioning failed at a named step."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(step, message)  # both in args, so the error pickles
        self.step = step
        self.message = message

    def __str__(self) -> str:
        return f"{self.step}: {self.message}"
