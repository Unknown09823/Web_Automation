"""Embedded HTML/JS dashboard.

Static, dependency-free single-page UI served by FastAPI at ``/dashboard``.
It calls the same JSON API any other client uses, so it works identically
for SSH tunnels, Termux, or browser access.
"""
