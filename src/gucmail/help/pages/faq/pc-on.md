# Does my PC need to stay on?

Yes for features performed by a local background process. No local process can send a scheduled message or detect new mail while the computer is powered off or asleep.

## What keeps working

Exchange continues receiving mail independently. Local drafts, scheduled jobs, checkpoints, history, indexes, and backups remain stored on disk. Closing the app is fine when the background task is on (`gucmail notifications on`). It checks for new mail and sends scheduled messages.

## What pauses

The local scheduler cannot transmit due mail. The watcher cannot detect events or deliver desktop, Discord, or webhook notifications. After wake, a configured one-shot task can evaluate overdue schedules and resume monitoring from its checkpoint. Exact timing can be delayed by sleep, runner interval, startup, network, authentication, or backoff.

For continuous alerts while a laptop is off, run the watcher on a secure always-on computer using [Headless operation](help:server.headless). For Windows login startup, follow [Start monitoring with Windows](help:startup.windows). Native server-delayed delivery or push must not be assumed unless current capability evidence explicitly confirms it.
