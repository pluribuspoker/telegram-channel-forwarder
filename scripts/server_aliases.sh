#!/bin/bash
# Server aliases for telegram-forwarder — sourced from .bashrc

Y='\e[1;33m'  # bold yellow
R='\e[0m'     # reset

# Format journalctl output: 12h ET timestamps + color coding
_fmtlog() {
    gawk '
    BEGIN {
        e = sprintf("%c", 27)
        RESET=e"[0m"; DIM=e"[90m"; BOLD=e"[1m"
        GREEN=e"[92m"; RED=e"[91m"; YELLOW=e"[93m"; GREY=e"[90m"
    }
    {
        line = $0

        # Convert 24h timestamp to 12h ET
        if (match(line, /^([A-Za-z]+ +[0-9]+ )([0-9]+):([0-9]+):([0-9]+)/, a)) {
            h = a[2]+0; ap = (h>=12 ? "PM" : "AM")
            h12 = h%12; if (h12==0) h12=12
            ts = a[1] h12 ":" a[3] ":" a[4] " " ap " ET"
            rest = substr(line, RLENGTH+1)
        } else {
            ts = ""; rest = line
        }

        # Separate syslog prefix from message content
        if (match(rest, /^( [^ ]+ [^ ]+\[[0-9]+\]: *)(.*)/, b)) {
            prefix = b[1]; msg = b[2]
        } else {
            prefix = rest; msg = ""
        }

        # Dim systemd lifecycle lines entirely
        if (prefix ~ /systemd/) {
            print DIM ts rest RESET; next
        }

        # Color message by content
        if      (msg ~ /WIN|✅|\[EDIT\]|✦ SENT|Completed successfully|Connected/)     color = GREEN
        else if (msg ~ /LOSS|❌|Crashed|Failed|errors: [1-9]|\[SKIP\]|failed: [1-9]/) color = RED
        else if (msg ~ /PENDING|⏳|\[WAIT\]/)                                          color = RESET
        else if (msg ~ /filtered|· filtered|UNKNOWN/)                                  color = DIM
        else                                                                            color = RESET

        print DIM ts prefix RESET color msg RESET
    }'
}

alias flogs='echo -e "${Y}▶  Forwarder logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -n 20 -f | _fmtlog'
alias tlogs='echo -e "${Y}▶  Tracker logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-tracker -n 50 -f | _fmtlog'
alias logs='echo -e "${Y}▶  All logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -u telegram-tracker -n 30 -f | _fmtlog'

alias start='echo -e "${Y}▶  Starting forwarder...${R}" && systemctl start telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias stop='echo -e "${Y}▶  Stopping forwarder...${R}" && systemctl stop telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias restart='echo -e "${Y}▶  Restarting forwarder...${R}" && systemctl restart telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias status='echo -e "${Y}▶  Forwarder status${R}" && systemctl status telegram-forwarder --no-pager -l'

alias deploy='cd /home/forwarder/app && echo -e "${Y}▶  Pulling latest code...${R}" && git pull && source /root/.bashrc && echo -e "${Y}\n▶  Restarting forwarder...${R}" && systemctl restart telegram-forwarder && echo -e "${Y}\n▶  Forwarder status${R}" && systemctl status telegram-forwarder --no-pager -l && echo -e "${Y}\n▶  Last tracker run${R}" && journalctl -u telegram-tracker -n 20 --no-pager | _fmtlog && echo -e "${Y}\n▶  Live logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -u telegram-tracker -n 20 -f | _fmtlog'

alias grade='echo -e "${Y}▶  Running pick grader (live, 1 day)...${R}" && su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 1 2>&1"'
alias gradetest='echo -e "${Y}▶  Running pick grader (dry run, 2 days)...${R}" && su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --dry-run --days 2 2>&1"'
