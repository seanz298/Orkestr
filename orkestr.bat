@echo off
rem Shortcut: orkestr status  ==  docker compose exec orchestrator python -m orchestrator status
docker compose exec orchestrator python -m orchestrator %*
