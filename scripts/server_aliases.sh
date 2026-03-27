#!/bin/bash
# Server aliases for telegram-forwarder — sourced from .bashrc

Y='\e[1;33m'  # bold yellow
R='\e[0m'     # reset

alias flogs='echo -e "${Y}▶  Forwarder logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -n 20 -f'
alias tlogs='echo -e "${Y}▶  Tracker logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-tracker -n 50 -f'
alias logs='echo -e "${Y}▶  All logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -u telegram-tracker -n 30 -f'

alias start='echo -e "${Y}▶  Starting forwarder...${R}" && systemctl start telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias stop='echo -e "${Y}▶  Stopping forwarder...${R}" && systemctl stop telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias restart='echo -e "${Y}▶  Restarting forwarder...${R}" && systemctl restart telegram-forwarder && echo -e "${Y}▶  Status${R}" && systemctl status telegram-forwarder --no-pager -l'
alias status='echo -e "${Y}▶  Forwarder status${R}" && systemctl status telegram-forwarder --no-pager -l'

alias deploy='cd /home/forwarder/app && echo -e "${Y}▶  Pulling latest code...${R}" && git pull && echo -e "${Y}\n▶  Restarting forwarder...${R}" && systemctl restart telegram-forwarder && echo -e "${Y}\n▶  Forwarder status${R}" && systemctl status telegram-forwarder --no-pager -l && echo -e "${Y}\n▶  Last tracker run${R}" && journalctl -u telegram-tracker -n 20 --no-pager && echo -e "${Y}\n▶  Live logs  (Ctrl+C to stop tailing, services keep running)${R}" && journalctl -u telegram-forwarder -u telegram-tracker -n 20 -f'

alias grade='echo -e "${Y}▶  Running pick grader (live, 1 day)...${R}" && su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 1 2>&1"'
alias gradetest='echo -e "${Y}▶  Running pick grader (dry run, 2 days)...${R}" && su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --dry-run --days 2 2>&1"'
