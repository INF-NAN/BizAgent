"""Training launcher. Talks to the separate train env ONLY via subprocesses: the app and train
environments pin conflicting dependencies (ADR-002), so nothing in this package imports torch,
veRL or AgentFly."""
