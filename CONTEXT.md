# Jarvis messaging

Jarvis currently provides the reliable messaging boundary for one dedicated
WhatsApp account. Intelligence and assistant behavior are a later concern and
are not part of the completed messaging context.

## Language

**Messaging gateway**:
The persistent boundary that receives and sends WhatsApp messages for the
dedicated account.
_Avoid_: Bot, AI assistant

**Messaging layer**:
Pairing, transport, persistence, readiness, and recovery for the messaging
gateway. It does not decide how a message should be answered.
_Avoid_: Assistant logic, agent behavior

**Dedicated account**:
The single WhatsApp account assigned to the messaging gateway.
_Avoid_: User account, sender

**Session**:
The gateway's named logical connection to the dedicated account. A session is
messaging-ready only when its state is `ready`.
_Avoid_: Container, HTTP health

**Messaging engine**:
The one active integration used by a session to connect to WhatsApp.
_Avoid_: Session, gateway

**Pairing state**:
Confidential retained authorization material that lets a session reconnect
without scanning a new QR code.
_Avoid_: API key, session name

**Assistant behavior**:
The future decision-making layer that determines whether and how Jarvis should
respond to an inbound message.
_Avoid_: Messaging layer, transport

**Reactive assistant behavior**:
Assistant behavior that starts only in response to an explicit inbound request
from the authorized operator. It does not monitor sources, run scheduled work,
or initiate messages on its own.
_Avoid_: Automation, proactive monitoring, scheduled assistant behavior
