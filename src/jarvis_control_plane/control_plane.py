# ruff: noqa: F401, F403, I001, RUF100 -- compatibility exports are intentional.
"""Compatibility surface for the deterministic control plane."""

from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType

from .application.broker import (
    core as _core,
    proposal as _proposal,
    approve as _approve,
    dispatch_prepare as _dispatch_prepare,
    dispatch_run as _dispatch_run,
    dispatch_lifecycle as _dispatch_lifecycle,
    orchestration_run as _orchestration_run,
    orchestration_result as _orchestration_result,
    outbound_complete as _outbound_complete,
    history_session as _history_session,
    memory_commands as _memory_commands,
    memory_views as _memory_views,
    session_control as _session_control,
    outbound_audit as _outbound_audit,
    receiver as _receiver,
    support as _support,
)
from .application.broker.core import _BrokerCoreMixin
from .application.broker.proposal import _BrokerProposalMixin
from .application.broker.approve import _BrokerApprovalMixin
from .application.broker.dispatch_prepare import _BrokerDispatchPrepareMixin
from .application.broker.dispatch_run import _BrokerDispatchRunMixin
from .application.broker.dispatch_lifecycle import _BrokerDispatchLifecycleMixin
from .application.broker.orchestration_run import _BrokerOrchestrationRunMixin
from .application.broker.orchestration_result import _BrokerOrchestrationResultMixin
from .application.broker.outbound_complete import _BrokerOutboundCompleteMixin
from .application.broker.history_session import _BrokerHistorySessionMixin
from .application.broker.memory_commands import _BrokerMemoryCommandsMixin
from .application.broker.memory_views import _BrokerMemoryViewsMixin
from .application.broker.session_control import _BrokerSessionControlMixin
from .application.broker.outbound_audit import _BrokerOutboundAuditMixin
from .application.broker.receiver import _SignedMessageReceiverBase
from .application.broker.support import *


class DeterministicCapabilityBroker(
    _BrokerCoreMixin,
    _BrokerProposalMixin,
    _BrokerApprovalMixin,
    _BrokerDispatchPrepareMixin,
    _BrokerDispatchRunMixin,
    _BrokerDispatchLifecycleMixin,
    _BrokerOrchestrationRunMixin,
    _BrokerOrchestrationResultMixin,
    _BrokerOutboundCompleteMixin,
    _BrokerHistorySessionMixin,
    _BrokerMemoryCommandsMixin,
    _BrokerMemoryViewsMixin,
    _BrokerSessionControlMixin,
    _BrokerOutboundAuditMixin,
):
    """Reference monitor for the request-to-reply capability path."""


class SignedMessageReceiver(_SignedMessageReceiverBase):
    """Canonical signed-message receiver compatibility class."""


_MIRROR_TARGETS = (
    _support,
    _core,
    _proposal,
    _approve,
    _dispatch_prepare,
    _dispatch_run,
    _dispatch_lifecycle,
    _orchestration_run,
    _orchestration_result,
    _outbound_complete,
    _history_session,
    _memory_commands,
    _memory_views,
    _session_control,
    _outbound_audit,
    _receiver,
)


class _ControlPlaneCompatibilityModule(_ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in _MIRROR_TARGETS:
            if hasattr(target, name):
                setattr(target, name, value)


_module = _sys.modules[__name__]
_module.__class__ = _ControlPlaneCompatibilityModule
