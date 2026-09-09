"""Single authenticated entrypoint for Casey-directed Jarvis emergency work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .operator_authorization import AuthorizationEnvelope
from .seat_boundaries import Action, verify_casey_direction

Operation = Callable[..., Any]
Verifier = Callable[[bytes, str, str], bool]


@dataclass(frozen=True)
class JarvisEmergencyGateway:
    """Expose emergency tools only after exact signed-direction verification."""

    actor: str
    envelope: AuthorizationEnvelope | None
    public_key_armor: str
    verifier: Verifier
    operations: Mapping[Action, Operation]
    scope: str = "skcapstone,skdashboard,skworld"

    def _run(self, action: Action, target: str, /, *args: Any, **kwargs: Any) -> Any:
        verify_casey_direction(
            self.actor,
            action,
            envelope=self.envelope,
            target=target,
            scope=self.scope,
            public_key_armor=self.public_key_armor,
            verifier=self.verifier,
        )
        operation = self.operations.get(action)
        if operation is None:
            raise ValueError(f"no emergency operation registered for {action.value}")
        return operation(target, *args, **kwargs)

    def create_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.CREATE_CARD, target, *args, **kwargs)

    def claim_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.CLAIM, target, *args, **kwargs)

    def complete_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.COMPLETE_CARD, target, *args, **kwargs)

    def fleet(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.LAUNCH, target, *args, **kwargs)

    def merge(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.MERGE, target, *args, **kwargs)

    def deploy(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.DEPLOY, target, *args, **kwargs)

    def release(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.RELEASE_ARTIFACT, target, *args, **kwargs)

    def verify(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.VERIFY, target, *args, **kwargs)

    def actuate(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.ACTUATE_APPLICATION, target, *args, **kwargs)
