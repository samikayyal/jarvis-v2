# Keep reminder scheduling in Jarvis

Jarvis owns the durable schedule for approved one-time reminders and asks
OpenWA to send the exact stored body only when its due time arrives. OpenWA
remains an immediate messaging transport because the pinned version has no
delayed-send contract; keeping one SQLite store and one in-process scheduler in
Jarvis also preserves editing and cancellation without adding another service.

This is the accepted destination architecture. Ticket 01 implements only
approved persistence and inspection; later tickets add the scheduler, delivery,
editing, and cancellation.
