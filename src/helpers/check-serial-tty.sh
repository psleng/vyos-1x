#!/bin/bash
# Check if login session is on a serial console (except ttyS0)
# Used by PAM to conditionally skip MOTD on serial ports
#
# Exit 0 = serial detected (skip MOTD)
# Exit 1 = not serial or ttyS0 (show MOTD)

FD_LIST=$(ls -l /proc/"$PPID"/fd 2>/dev/null)

# If ttyS0, show MOTD (exit 1 = not serial for our purposes)
if echo "$FD_LIST" | grep -q '/dev/ttyS0'; then
    exit 1
fi

# Other ttyS ports, hide MOTD
if echo "$FD_LIST" | grep -q '/dev/ttyS'; then
    exit 0
fi

exit 1  # Not serial
