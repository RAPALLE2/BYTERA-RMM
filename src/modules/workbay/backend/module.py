"""Work Bay - backend entry.

Work Bay is a frontend-only feature: the scratch note and the clipboard are
stored through the Notepad app of the "collab" module (which keeps them in the
user's personal Space), and parking windows happens entirely in the browser
window manager. So there is nothing to serve here - this entry only exists so
the module loader has a backend hook to call.
"""


def on_startup(ctx) -> None:
    # Nothing to set up. Kept intentionally empty.
    return None
