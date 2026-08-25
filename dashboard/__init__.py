"""Local control-plane dashboard for a running crypto-arb-engine process.

This is not a hosted service. `dashboard.server.create_app` returns a
FastAPI app meant to run *inside the same process* as `main.py`'s
detection loop, sharing live Python objects directly (see
`core/control.py`). It binds to localhost by default -- see
`config.settings.DASHBOARD_HOST` -- and is meant to be viewed from a
browser on the same machine you're running the bot on, not exposed to
the internet.
"""
