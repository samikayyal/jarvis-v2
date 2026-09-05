# Keep reminder scheduling in Jarvis

Jarvis owns the durable schedule for approved one-time reminders and asks
OpenWA to send the exact stored body only when its due time arrives. OpenWA
remains an immediate messaging transport because the pinned version has no
delayed-send contract; keeping one SQLite store and one in-process scheduler in
Jarvis also preserves editing and cancellation without adding another service.

The active personal runtime implements this architecture with one SQLite store
and one asynchronous scheduler inside its existing service process. Approved
Reminders can be listed, edited, or cancelled while pending; terminal outcomes
remain retained history and are never selected for delivery.
