"""PROTOTYPE: interactive terminal driver for the WhatsApp control grammar."""

from __future__ import annotations

import json

from control_grammar import (
    ControlState,
    Transition,
    handle_operator_message,
    handle_system_event,
    state_dict,
)

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

HELP = """Operator messages: ordinary request text or a slash command.
The real orchestration agent chooses Ubuntu or the personal Windows laptop from natural language.
Ubuntu is the default. This no-model prototype requires `:route` to simulate that agent decision.

Prototype-only system events:
  :route ubuntu REASON  simulate the agent selecting default Ubuntu
  :route windows REASON simulate the agent selecting the personal Windows laptop
  :milestone TEXT       emit a request milestone
  :propose              propose a reusable-permission-eligible terminal action
  :propose mandatory    propose a this-time-only action
  :complete             complete the active request
  :expire               expire the pending action
  :restart              simulate a service restart
  :host ubuntu ready|down
  :host windows ready|down
  :demo                 replay the reference transcript
  :reset                reset all in-memory state
  :help                 show this help
  :quit                 quit
"""


def render(
    state: ControlState, sender: str, replies: tuple[str, ...], effects: tuple[str, ...]
) -> None:
    print("\033[2J\033[H", end="")
    print(f"{BOLD}PROTOTYPE 09 — WhatsApp control interaction{RESET}")
    print(f"{DIM}Throwaway, in memory, no OpenWA or external side effects.{RESET}\n")
    print(f"{BOLD}Last input{RESET}\n{sender}\n")
    print(f"{BOLD}Jarvis reply{RESET}")
    for reply in replies:
        print(reply)
    if effects:
        print(f"\n{DIM}Simulated effects: {', '.join(effects)}{RESET}")
    print(f"\n{BOLD}Complete control state{RESET}")
    print(json.dumps(state_dict(state), indent=2))
    print(f"\n{BOLD}Input{RESET} {DIM}Type :help for controls; :quit to exit.{RESET}")


def dispatch(state: ControlState, line: str) -> Transition:
    if not line.startswith(":"):
        return handle_operator_message(state, line)
    command, _, rest = line.partition(" ")
    args = rest.split()
    if command == ":route":
        return handle_system_event(state, "route", *args)
    if command == ":milestone":
        return handle_system_event(state, "milestone", rest)
    if command == ":propose":
        if args not in ([], ["mandatory"]):
            return Transition(
                state,
                (
                    (
                        "Prototype usage: `:propose` or `:propose mandatory`. This is a developer simulation "
                        "control, not a proposal name or a WhatsApp command."
                    ),
                ),
            )
        return handle_system_event(state, "propose", *args)
    if command in {":complete", ":expire", ":restart"}:
        return handle_system_event(state, command[1:])
    if command == ":host" and len(args) == 2:
        return handle_system_event(state, "availability", *args)
    return Transition(state, ("Unknown prototype command. Type :help.",))


def demo(state: ControlState) -> ControlState:
    steps = (
        ("operator", "/status"),
        ("operator", "/model gpt-5.6-sol"),
        ("operator", "/reasoning high"),
        ("operator", "Check the file I downloaded in Chrome on my laptop"),
        ("system", "route windows request refers to a file on the personal laptop"),
        ("system", "milestone Connected to the personal Windows laptop"),
        ("system", "propose"),
        ("operator", "allow for this session"),
        ("system", "complete"),
        ("operator", "/permissions"),
        ("operator", "/new"),
    )
    for source, value in steps:
        if source == "operator":
            transition = handle_operator_message(state, value)
        else:
            transition = dispatch(state, f":{value}")
        state = transition.state
        print(f"\n{source.title()}: {value}")
        for reply in transition.replies:
            print(f"Jarvis: {reply}")
    input(
        "\nReference transcript complete. Press Enter to return to the live prototype."
    )
    return state


def main() -> None:
    state = ControlState()
    render(state, "(prototype started)", ("Ready.",), ())
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line == ":quit":
            return
        if line == ":help":
            render(state, line, (HELP,), ())
            continue
        if line == ":reset":
            state = ControlState()
            render(state, line, ("All in-memory prototype state reset.",), ())
            continue
        if line == ":demo":
            state = demo(state)
            render(state, line, ("Reference transcript replayed.",), ())
            continue
        transition = dispatch(state, line)
        state = transition.state
        render(state, line, transition.replies, transition.effects)


if __name__ == "__main__":
    main()
