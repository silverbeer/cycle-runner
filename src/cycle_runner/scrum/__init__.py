"""Scrum Master: a read-only engineering health report from Linear, GitHub and local git (SB-1385).

    collect (linear, github, git) ─► Snapshot ─► analyze (rules) ─► Report ─► render (terminal, markdown, json)

Each stage is a plain function over JSON-serialisable Pydantic models, so it can
run on its own (`scrum-master collect | analyze | render`) and be wrapped later
(a k3s CronJob, Telegram) without changing it.

Nothing here writes anywhere outside the local filesystem: Linear is read with a
read-scoped app token, GitHub only with GET and GraphQL queries, git only with
an allowlist of read-only subcommands. No model is involved: the same snapshot
always gives the same report.
"""
