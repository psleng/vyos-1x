<!-- include start from serial/service/utils/remote.xml.i -->
<node name="remote">
  <properties>
    <help>Remote host connection settings</help>
  </properties>
  <children>
    <leafNode name="port">
      <properties>
        <help>Remote host port</help>
        <valueHelp>
          <format>u32:1-65535</format>
          <description>Port number</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-65535"/>
        </constraint>
      </properties>
    </leafNode>
    <leafNode name="address">
      <properties>
        <help>Remote address</help>
        <valueHelp>
          <format>ipv4</format>
          <description>IP address of remote host</description>
        </valueHelp>
        <valueHelp>
          <format>ipv6</format>
          <description>IPv6 address of remote host</description>
        </valueHelp>
        <valueHelp>
          <format>hostname</format>
          <description>Fully qualified host name of remote host</description>
        </valueHelp>
        <constraint>
          <validator name="ip-address"/>
          <validator name="fqdn"/>
        </constraint>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
