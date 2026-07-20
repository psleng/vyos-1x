#!/bin/bash

IFACE=$1
if [[ "$1" == "" ]]; then
    IFACE="wwan0"
fi
num="0"
if [[ $IFACE =~ ^wwan([0-9]+)$ ]]; then
    num="${BASH_REMATCH[1]}"
    echo "$num"
fi
declare -A tests
declare -A rules
declare -A inbound
declare -A matches
count=$(($num+1))

config_output=$(cli-shell-api showConfig --show-active-only --show-commands | tr -d "'")
failover_detected=false
while read -r line; do
    echo "$line"
    # interface-health eth0 test 1

    if [[ "$line" =~ rule[[:space:]]+([0-9]+)[[:space:]]+failover ]]; then
        failover_detected=true
    fi

    if [[ "$line" =~ interface-health[[:space:]]+([A-Za-z0-9_]+)[[:space:]]+test[[:space:]]+([0-9]+) ]]; then
        iface="${BASH_REMATCH[1]}"
        testnum="${BASH_REMATCH[2]}"
        tests["$iface"]+="$testnum "
        echo "$iface"
    fi

    # rule 1 interface eth0
    if [[ "$line" =~ rule[[:space:]]+([0-9]+)[[:space:]]+interface[[:space:]]+([A-Za-z0-9_]+) ]]; then
        rulenum="${BASH_REMATCH[1]}"
        iface="${BASH_REMATCH[2]}"
        rules["$iface"]+="$rulenum "
        echo "$iface"
    fi

    # rule 1 inbound-interface 'any'
    if [[ "$line" =~ rule[[:space:]]+([0-9]+)[[:space:]]+inbound-interface[[:space:]]+([A-Za-z0-9_]+) ]]; then
        rulenum="${BASH_REMATCH[1]}"
        inboundval="${BASH_REMATCH[2]}"
        inbound["$rulenum"]+="$inboundval "
    fi
done <<< "$config_output"


#no failover found
if [[ $failover_detected == "false" ]]; then

    lan=$(for i in /sys/class/net/*; do
        interface="${i##*/}"
        if [[ -d "$i/device" ]] && [[ ! "$interface" =~ ^(wwan[0-9]+|wlan[0-9]+)$ ]]; then
            echo -n "$interface,"
        fi
    done | sed 's/,$//')
    echo $lan

    if [[ "$IFACE" =~ ^(wwan[0-9]+|wlan[0-9]+)$ ]]; then
            sudo nft add table inet wwan_raw_$count
            sudo nft add chain inet wwan_raw_$count output '{ type filter hook output priority raw; policy accept; }'
            sudo nft add rule inet wwan_raw_$count output oifname $IFACE queue num $count
            sudo nft add chain inet wwan_raw_$count prerouting '{ type filter hook prerouting priority raw; policy accept; }'
            sudo nft add rule inet wwan_raw_$count prerouting queue num $count
            sudo nft add rule inet wwan_raw_$count prerouting \
                    iifname { $lan } \
                    fib daddr oifname "$IFACE" \
                    queue num $count
    fi
fi

for iface in "${!tests[@]}"; do
    # Get the list of testnums for this iface
    testnums="${tests[$iface]}"

    # Get the list of rulenums for this iface
    rulenums="${rules[$iface]}"

    # Loop through each testnum and each rulenum
    for testnum in $testnums; do
        for rulenum in $rulenums; do
            # If there's a match between testnum and rulenum for the same iface, store it
            if [[ "$testnum" == "$rulenum" && "$iface" =~ "$IFACE" ]]; then
                matches["$iface:$testnum:$rulenum"]=1  # Store unique iface in matches array
            fi
        done
    done
done


# Display the matching interfaces along with their testnum and rulenum combinations
echo "Matching interfaces with their corresponding testnum and rulenum (no duplicates):"
for key in "${!matches[@]}"; do
    # Split the key into iface, testnum, and rulenum
    iface="${key%%:*}"
    temp="${key#*:}"
    testnum="${temp%%:*}"
    rulenum="${temp#*:}"
    echo "table wwan_raw_$count"

    echo "Interface: $iface, Testnum: $testnum, Rulenum: $rulenum"
    sudo nft add table inet wwan_raw_$count
    sudo nft add chain inet wwan_raw_$count output '{ type filter hook output priority raw; policy accept; }'
    sudo nft add rule inet wwan_raw_$count output oifname $iface queue num $count
    sudo nft add chain inet wwan_raw_$count prerouting '{ type filter hook prerouting priority raw; policy accept; }'
    sudo nft add rule inet wwan_raw_$count prerouting queue num $count
    for rule in "${!inbound[@]}"; do
        if [[ "$rule" == "$rulenum" ]]; then
            if [[ "${inbound[$rule]}" =~ "any" ]]; then
                #find /sys/class/net -maxdepth 1 -mindepth 1 -type l -exec test -e "{}/device" \; -printf "%f\n"
                sudo nft add rule inet wwan_raw_$count prerouting \
                    iifname { $(for i in /sys/class/net/*; do
                                    interface="${i##*/}"
                                    if [[ -d "$i/device" ]] && [[ ! "$interface" =~ ^(wwan[0-9]+|wlan[0-9]+)$ ]]; then
                                        echo -n "$interface,"
                                    fi
                                done | sed 's/,$//') } \
                    fib daddr oifname "$iface" \
                    queue num $count
            else
                sudo nft add rule inet wwan_raw_$count prerouting \
                    iifname { ${inbound[$rule]} } \
                    fib daddr oifname "$iface" \
                    queue num $count
            fi
        fi
    done
done
