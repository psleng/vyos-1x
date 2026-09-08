<!-- include start from serial/service/utils/multihost.xml.i -->
<node name="remote">
  <properties>
    <help>Remote host connection settings</help>
  </properties>
  <children>
    <node name="primary">
      <properties>
        <help>Primary host connection settings</help>
      </properties>
      <children>
        <leafNode name="port">
          <properties>
            <help>Primary host port</help>
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
            <help>Primary host address</help>
            <valueHelp>
              <format>ipv4</format>
              <description>IP address of primary host</description>
            </valueHelp>
            <valueHelp>
              <format>ipv6</format>
              <description>IPv6 address of primary host</description>
            </valueHelp>
            <valueHelp>
              <format>hostname</format>
              <description>Fully qualified host name of primary host</description>
            </valueHelp>
            <constraint>
              <validator name="ip-address"/>
              <validator name="fqdn"/>
            </constraint>
          </properties>
        </leafNode>
      </children>
    </node>
    <node name="backup">
      <properties>
        <help>Backup host connection settings</help>
      </properties>
      <children>
        <leafNode name="port">
          <properties>
            <help>Backup host port</help>
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
            <help>Backup host address</help>
            <valueHelp>
              <format>ipv4</format>
              <description>IP address of backup host</description>
            </valueHelp>
            <valueHelp>
              <format>ipv6</format>
              <description>IPv6 address of backup host</description>
            </valueHelp>
            <valueHelp>
              <format>hostname</format>
              <description>Fully qualified host name of backup host</description>
            </valueHelp>
            <constraint>
              <validator name="ip-address"/>
              <validator name="fqdn"/>
            </constraint>
          </properties>
        </leafNode>
      </children>
    </node>
    <node name="multi-host">
      <properties>
        <help>Multi-host connection settings</help>
      </properties>
      <children>
        <tagNode name="entry">
          <properties>
            <help>Multi-host entry</help>
            <valueHelp>
              <format>u32:1-50</format>
              <description>Host ID (1-50)</description>
            </valueHelp>
            <constraint>
              <validator name="numeric" argument="--range 1-50"/>
            </constraint>
          </properties>
          <children>
            <leafNode name="address">
              <properties>
                <help>Multi-host entry address</help>
                <valueHelp>
                  <format>ipv4</format>
                  <description>IP address of this entry</description>
                </valueHelp>
                <valueHelp>
                  <format>ipv6</format>
                  <description>IPv6 address of this entry</description>
                </valueHelp>
                <valueHelp>
                  <format>hostname</format>
                  <description>Fully qualified host name of this entry</description>
                </valueHelp>
                <constraint>
                  <validator name="ip-address"/>
                  <validator name="fqdn"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="port">
              <properties>
                <help>Multi-host entry port</help>
                <valueHelp>
                  <format>u32:1-65535</format>
                  <description>Port number</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 1-65535"/>
                </constraint>
              </properties>
            </leafNode>
          </children>
        </tagNode>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
