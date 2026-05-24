#!/bin/bash
# run_background.sh
#
# Launches both the PDF scraper and the agent pipeline as persistent
# background processes that survive terminal closes.
#
# Usage:
#   ./run_background.sh          # start both
#   ./run_background.sh scraper  # start only the scraper
#   ./run_background.sh agents   # start only the agents
#
# Logs:
#   logs/scraper.log  — scraper output
#   logs/agents.log   — agent pipeline output
#
# PIDs:
#   logs/scraper.pid  — scraper process ID (to kill it: kill $(cat logs/scraper.pid))
#   logs/agents.pid   — agent process ID
#
# Monitor live:
#   tail -f logs/scraper.log
#   tail -f logs/agents.log

set -e
PROJ="/home/guy/Dev/infrastraucture-bridges"
PYTHON="$PROJ/.venv/bin/python"
LOGS="$PROJ/logs"

mkdir -p "$LOGS"

is_running() {
    local pidfile="$1"
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile")
        kill -0 "$pid" 2>/dev/null && return 0
    fi
    return 1
}

start_scraper() {
    if is_running "$LOGS/scraper.pid"; then
        echo "⚠  Scraper already running (PID $(cat $LOGS/scraper.pid)) — skipping"
        return
    fi
    nohup "$PYTHON" -m backend.scripts.scrape_mndot_reports \
        > "$LOGS/scraper.log" 2>&1 &
    echo $! > "$LOGS/scraper.pid"
    echo "✓  Scraper started  (PID $!)"
    echo "   tail -f $LOGS/scraper.log"
}

start_agents() {
    if is_running "$LOGS/agents.pid"; then
        echo "⚠  Agents already running (PID $(cat $LOGS/agents.pid)) — skipping"
        return
    fi
    nohup "$PYTHON" -m backend.scripts.run_agents \
        > "$LOGS/agents.log" 2>&1 &
    echo $! > "$LOGS/agents.pid"
    echo "✓  Agents started   (PID $!)"
    echo "   tail -f $LOGS/agents.log"
}

TARGET="${1:-both}"

cd "$PROJ"
echo "=== Bridge Pipeline Launcher ==="

case "$TARGET" in
    scraper) start_scraper ;;
    agents)  start_agents  ;;
    *)       start_scraper; start_agents ;;
esac

echo ""
echo "Monitor with:"
echo "  tail -f $LOGS/scraper.log"
echo "  tail -f $LOGS/agents.log"
echo ""
echo "Stop with:"
echo "  kill \$(cat $LOGS/scraper.pid)"
echo "  kill \$(cat $LOGS/agents.pid)"
