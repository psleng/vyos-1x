<!-- include start from failover/protocol-common-config.xml.i -->
<tagNode name="route">
  <properties>
    <help>Failover IPv4 route</help>
    <valueHelp>
      <format>ipv4net</format>
      <description>IPv4 failover route</description>
    </valueHelp>
    <constraint>
      <validator name="ipv4-prefix"/>
    </constraint>
  </properties>
  <children>
    <tagNode name="next-hop">
      <properties>
        <help>Next-hop IPv4 router address</help>
        <valueHelp>
          <format>ipv4</format>
          <description>Next-hop router address</description>
        </valueHelp>
        <constraint>
          <validator name="ipv4-address"/>
        </constraint>
      </properties>
      #include <include/failover/common-failover.xml.i>
    </tagNode>
    <tagNode name="dhcp-interface">
      <properties>
        #include <include/dhcp-interface-properties.xml.i>
      </properties>
      #include <include/failover/common-failover.xml.i>
    </tagNode>
    #include <include/failover/interface-failover.xml.i>
  </children>
</tagNode>
<tagNode name="route6">
  <properties>
    <help>Failover IPv6 route</help>
    <valueHelp>
      <format>ipv6net</format>
      <description>IPv6 failover route</description>
    </valueHelp>
    <constraint>
      <validator name="ipv6-prefix"/>
    </constraint>
  </properties>
  <children>
    <tagNode name="next-hop">
      <properties>
        <help>Next-hop IPv6 router address</help>
        <valueHelp>
          <format>ipv6</format>
          <description>Next-hop router address</description>
        </valueHelp>
        <constraint>
          <validator name="ipv6-address"/>
        </constraint>
      </properties>
      #include <include/failover/common-failover.xml.i>
    </tagNode>
    #include <include/failover/interface-failover.xml.i>
  </children>
</tagNode>
<!-- include end -->
