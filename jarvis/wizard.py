"""python -m jarvis setup: walks through every service, saves config.yaml/.env and tests each connection."""

from getpass import getpass

from . import setup_schema as S


def _ask(prompt: str, default=None) -> str:
    hint = f" [{default}]" if default not in (None, "", []) else ""
    v = input(f"  {prompt}{hint}: ").strip()
    return v if v else ("" if default is None else str(default))


def _yes(prompt: str, default: bool = False) -> bool:
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not v else v in ("y", "yes")


def _fill(svc: S.Service, current: dict, index: int) -> dict:
    values = {}
    sec = S.secrets()
    for f in svc.fields:
        if f.help:
            print(f"    ({f.help})")
        if f.kind == "secret":
            is_set = bool(sec.get(S.env_name(svc, f, current, index)))
            note = "already set, Enter to keep" if is_set else ("required" if f.required else "optional, Enter to skip")
            values[f.key] = getpass(f"  {f.label} ({note}): ").strip()
        elif f.kind == "bool":
            values[f.key] = _yes(f"  {f.label}?", bool(current.get(f.key, f.default)))
        elif f.kind == "choice":
            values[f.key] = _ask(f"{f.label} ({' / '.join(f.options)})", current.get(f.key, f.default))
        elif f.kind == "list":
            values[f.key] = _ask(f.label, ", ".join(map(str, current.get(f.key) or [])) or None)
        else:
            values[f.key] = _ask(f.label, current.get(f.key, f.default))
    return values


def _configure_entry(svc: S.Service, index: int | None, current: dict) -> None:
    while True:
        try:
            idx = S.save(svc.id, _fill(svc, current, index or 0), index)
        except ValueError as e:
            print(f"  ! {e}")
            if _yes("  Try again?", True):
                continue
            return
        print("  Saved.")
        if not svc.test:
            return
        print("  Testing the connection...")
        try:
            print(f"  OK: {S.test(svc.id, idx)}")
            return
        except Exception as e:
            print(f"  FAILED: {e}")
            if not _yes("  Edit and try again? (No keeps it as saved)", True):
                return
            index, current = idx, S.entries(svc)[idx]


def _service(svc: S.Service) -> None:
    existing = S.entries(svc)
    print(f"\n== {svc.label} ==")
    if svc.help:
        print(f"  {svc.help}")
    if svc.multi:
        for i, e in enumerate(existing):
            if _yes(f"Update {svc.label} entry '{e.get('name', i + 1)}'?", False):
                _configure_entry(svc, i, e)
            elif _yes(f"Remove '{e.get('name', i + 1)}'?", False):
                S.remove(svc.id, i)
        while _yes(f"Add {'another' if existing else 'a'} {svc.label} entry?", not existing):
            _configure_entry(svc, None, {})
            existing = S.entries(svc)
        return
    if existing:
        if _yes(f"{svc.label} is set up. Change it?", False):
            _configure_entry(svc, 0, existing[0])
        elif _yes(f"Remove {svc.label} from Jarvis?", False):
            S.remove(svc.id)
    elif _yes(f"Do you use {svc.label}?", False):
        _configure_entry(svc, None, {})


def run(only: str | None = None) -> None:
    print("Jarvis setup. Press Enter to accept the value in [brackets]. Ctrl+C to stop at any time (saved parts are kept).")
    if only:
        if only not in S.BY_ID:
            raise SystemExit(f"Unknown service '{only}'. Choose from: {', '.join(S.BY_ID)}")
        _service(S.BY_ID[only])
        return

    pub, created = S.ensure_ssh_key()
    print(f"\nJarvis's SSH key ({'just created' if created else 'already exists'}). Add this line to Unraid and your VMs:\n  {pub}")

    group = None
    for svc in S.SERVICES:
        if svc.group != group:
            group = svc.group
            print(f"\n######## {group} ########")
        try:
            _service(svc)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"  ! Something went wrong with {svc.label}: {e}")
    print("\nAll done. Next: python -m jarvis status   (or: python -m jarvis web)")
