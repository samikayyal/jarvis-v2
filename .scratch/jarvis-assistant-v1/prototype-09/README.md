# PROTOTYPE 09 — WhatsApp control interaction

This throwaway logic prototype asks whether Jarvis's text-only V1 control
grammar makes working-session controls, request ownership, host selection,
milestones, approvals, expiry, cancellation, and reusable terminal permissions
unambiguous when driven as a WhatsApp transcript. It is not production code and
does not connect to OpenWA or any external service.

Run it from the repository root:

```powershell
uv run .scratch/jarvis-assistant-v1/prototype-09/prototype.py
```

The terminal shows the complete in-memory control state after every message.
Type ordinary text or a slash command as the operator. Because this prototype
does not run an orchestration model, prototype-only commands beginning with `:`
simulate agent/system events. After an ordinary request, use
`:route ubuntu REASON` or `:route windows REASON` to inject the agent's routing
decision; Ubuntu is the default and Windows means the operator's personal
laptop. Only then can the prototype simulate a milestone or proposal. Use
`:demo` to replay the reference transcript and `:help` to list all controls.

The pure parser and reducer are in `control_grammar.py`; `prototype.py` is only
the throwaway terminal shell.
