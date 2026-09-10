"""Single authenticated entrypoint for Casey-directed Jarvis emergency work."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .key_io import read_armored_public_key
from .operator_authorization import (
    AuthorizationEnvelope,
    consume_authorization,
    load_authorization,
)
from .seat_boundaries import (
    JARVIS_DIRECT_ACTIONS,
    Action,
    canonical_human_principal,
    require_authority,
    verify_casey_direction,
)

Operation = Callable[..., Any]
Verifier = Callable[[bytes, str, str], bool]
SCOPE = "skcapstone,skdashboard,skworld"


def authorize_jarvis_entrypoint(
    actor: str,
    action: Action,
    target: str,
    authorization_path: Path | None,
    change_id: str | None,
) -> None:
    """Gate a real mutation surface when its authenticated actor is Jarvis."""
    if actor.strip().lower() != "jarvis":
        return
    if action in JARVIS_DIRECT_ACTIONS:
        return
    if authorization_path is None:
        raise ValueError(f"jarvis requires --casey-authorization for {action.value}")
    if not change_id:
        raise ValueError(f"jarvis requires --casey-change-id for {action.value}")

    from capauth import resolve_capauth_home
    from capauth.crypto import get_backend
    from capauth.profile import load_profile

    capauth_home = resolve_capauth_home()
    profile = load_profile(base_dir=capauth_home)
    handle = canonical_human_principal(profile.entity.handle or profile.entity.name)
    if handle != "casey":
        raise ValueError("active CapAuth human profile is not Casey")
    public_armor = read_armored_public_key(capauth_home / "identity" / "public.asc")
    if not public_armor:
        raise ValueError("Casey public key is unavailable")
    envelope = load_authorization(authorization_path)
    verify_casey_direction(
        actor,
        action,
        envelope=envelope,
        target=target,
        change_id=change_id,
        scope=SCOPE,
        public_key_armor=public_armor,
        expected_fingerprint=profile.key_info.fingerprint,
        verifier=get_backend(profile.crypto_backend).verify,
    )
    consume_authorization(envelope, capauth_home / "operator" / "used-authorizations")


@dataclass(frozen=True)
class JarvisEmergencyGateway:
    """Expose direct coordination and signed external-effect operations."""

    actor: str
    envelope: AuthorizationEnvelope | None
    public_key_armor: str
    expected_fingerprint: str
    change_id: str
    verifier: Verifier
    operations: Mapping[Action, Operation]
    replay_store: Path
    scope: str = SCOPE

    def _run(self, action: Action, target: str, /, *args: Any, **kwargs: Any) -> Any:
        if action in JARVIS_DIRECT_ACTIONS:
            require_authority(self.actor, action)
        else:
            verify_casey_direction(
                self.actor,
                action,
                envelope=self.envelope,
                target=target,
                change_id=self.change_id,
                scope=self.scope,
                public_key_armor=self.public_key_armor,
                expected_fingerprint=self.expected_fingerprint,
                verifier=self.verifier,
            )
            assert self.envelope is not None
            consume_authorization(self.envelope, self.replay_store)
        operation = self.operations.get(action)
        if operation is None:
            raise ValueError(f"no emergency operation registered for {action.value}")
        return operation(target, *args, **kwargs)

    def create_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.CREATE_CARD, target, *args, **kwargs)

    def claim_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.CLAIM, target, *args, **kwargs)

    def move_card(self, target: str, /, *args: Any, **kwargs: Any) -> Any:
        return self._run(Action.MOVE_CARD, target, *args, **kwargs)

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
