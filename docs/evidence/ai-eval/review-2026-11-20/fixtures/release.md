# Release process

Every release goes through three gates.

## Build

- compile the sources
- run the unit tests

## Deploy

| Stage | Owner |
|-------|-------|
| Build | Alice |
| Deploy | Bob |

The deploy step restarts the workers one at a time.
