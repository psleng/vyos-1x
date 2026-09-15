<!-- include start from serial/service/udp.xml.i -->
<node name="udp">
  <properties>
    <help>UDP service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/transmit-string-no-end.xml.i>
    <leafNode name="multicast-interface">
      <properties>
        <help>Ethernet Interface for multicast</help>
        <valueHelp>
          <format>ethN</format>
          <description>Ethernet interface name</description>
        </valueHelp>
        <completionHelp>
          <script>${vyos_completion_dir}/list_interfaces --type ethernet</script>
        </completionHelp>
        <constraint>
          <regex>eth[0-9]+</regex>
        </constraint>
        <constraintErrorMessage>Invalid Ethernet interface name</constraintErrorMessage>
      </properties>
    </leafNode>
    <leafNode name="port">
      <properties>
        <help>UDP port to listen for incoming connections</help>
        <valueHelp>
          <format>u32:1-65535</format>
          <description>Port number</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-65535"/>
        </constraint>
      </properties>
    </leafNode>
    <tagNode name="rule">
      <properties>
        <help>UDP rule entry</help>
        <valueHelp>
          <format>u32:1-10</format>
          <description>Entry ID (1-10)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-10"/>
        </constraint>
      </properties>
      <children>
        <node name="direction">
          <properties>
            <help>UDP rule direction</help>
          </properties>
          <children>
            <node name="both">
              <properties>
                <help>Bidirectional UDP traffic settings</help>
              </properties>
              <children>
                <leafNode name="port">
                  <properties>
                    <help>UDP port option for direction both</help>
                    <completionHelp>
                      <list>auto-learn</list>
                    </completionHelp>
                    <valueHelp>
                      <format>auto-learn</format>
                      <description>auto-learn</description>
                    </valueHelp>
                    <valueHelp>
                      <format>u32:1-65535</format>
                      <description>Specific UDP port number</description>
                    </valueHelp>
                    <constraint>
                      <validator name="udp-port-option"/>
                    </constraint>
                  </properties>
                </leafNode>
              </children>
            </node>
            <node name="lan-serial">
              <properties>
                <help>LAN to serial direction settings</help>
              </properties>
              <children>
                <leafNode name="source-port">
                  <properties>
                    <help>UDP source port option for direction lan-serial</help>
                    <completionHelp>
                      <list>auto-learn any</list>
                    </completionHelp>
                    <valueHelp>
                      <format>auto-learn</format>
                      <description>auto-learn</description>
                    </valueHelp>
                    <valueHelp>
                      <format>u32:1-65535</format>
                      <description>Specific UDP port number</description>
                    </valueHelp>
                    <valueHelp>
                      <format>any</format>
                      <description>any</description>
                    </valueHelp>
                    <constraint>
                      <validator name="udp-port-option"/>
                    </constraint>
                  </properties>
                </leafNode>
              </children>
            </node>
            <node name="serial-lan">
              <properties>
                <help>Serial to LAN direction settings</help>
              </properties>
              <children>
                <leafNode name="destination-port">
                  <properties>
                    <help>UDP destination port option for direction serial-lan</help>
                    <valueHelp>
                      <format>u32:1-65535</format>
                      <description>Specific UDP port number</description>
                    </valueHelp>
                    <constraint>
                      <validator name="udp-port-option"/>
                    </constraint>
                  </properties>
                </leafNode>
              </children>
            </node>
          </children>
        </node>
        <node name="address">
          <properties>
            <help>Remote host address range</help>
          </properties>
          <children>
            <leafNode name="start">
              <properties>
                <help>UDP Start Host IP</help>
                <valueHelp>
                  <format>ipv4</format>
                  <description>IP address of current host</description>
                </valueHelp>
                <valueHelp>
                  <format>ipv6</format>
                  <description>IPv6 address of current host</description>
                </valueHelp>
                <constraint>
                  <validator name="ip-address"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="end">
              <properties>
                <help>UDP End Host IP</help>
                <valueHelp>
                  <format>ipv4</format>
                  <description>IP address of current host</description>
                </valueHelp>
                <valueHelp>
                  <format>ipv6</format>
                  <description>IPv6 address of current host</description>
                </valueHelp>
                <constraint>
                  <validator name="ip-address"/>
                </constraint>
              </properties>
            </leafNode>
          </children>
        </node>
      </children>
    </tagNode>
  </children>
</node>
<!-- include end -->
