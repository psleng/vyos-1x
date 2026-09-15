<!-- Used to be port-buffering -->
<!-- include start from serial/global/ssh-telnet-server-logging.xml.i -->
<node name="ssh-telnet-server-logging">
  <properties>
    <help>SSH and Telnet server logging settings</help>
  </properties>
  <children>
    <node name="local">
      <properties>
        <help>Enable local logging</help>
      </properties>
      <children>
        <leafNode name="view-string">
          <properties>
            <help>Local logging escape view string</help>
            <constraint>
              <regex>.{0,8}</regex>
            </constraint>
            <constraintErrorMessage>View string too long (limit 8 characters)</constraintErrorMessage>
          </properties>
          <defaultValue>~show</defaultValue>
        </leafNode>
      </children>
    </node>
    <node name="nfs">
      <properties>
        <help>Enable NFS logging</help>
      </properties>
      <children>
        <node name="server">
          <properties>
            <help>NFS server</help>
          </properties>
          <children>
            <leafNode name="address">
              <properties>
                <help>NFS server address</help>
                <valueHelp>
                  <format>ipv4</format>
                  <description>IP address of NFS server</description>
                </valueHelp>
                <valueHelp>
                  <format>ipv6</format>
                  <description>IPv6 address of NFS server</description>
                </valueHelp>
                <valueHelp>
                  <format>hostname</format>
                  <description>Fully qualified host name of NFS server</description>
                </valueHelp>
                <constraint>
                  <validator name="ip-address"/>
                  <validator name="fqdn"/>
                </constraint>
              </properties>
            </leafNode>
            <leafNode name="directory">
              <properties>
                <help>Path to NFS server directory</help>
                <constraint>
                  <regex>.{0,40}</regex>
                </constraint>
                <constraintErrorMessage>Path string too long (limit 40 characters)</constraintErrorMessage>
              </properties>
              <defaultValue>/device_server/portlogs</defaultValue>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
    <node name="syslog">
      <properties>
        <help>Enable including serial message in syslog local and remote</help>
      </properties>
      <children>
        <leafNode name="level">
          <properties>
            <help>syslog marked with configured level</help>
            <completionHelp>
              <list>emergency alert critical error warning notice info debug</list>
            </completionHelp>
            <valueHelp>
              <format>emergency</format>
              <description>Emergency messages</description>
            </valueHelp>
            <valueHelp>
              <format>alert</format>
              <description>Urgent messages</description>
            </valueHelp>
            <valueHelp>
              <format>critical</format>
              <description>Critical messages</description>
            </valueHelp>
            <valueHelp>
              <format>error</format>
              <description>Error messages</description>
            </valueHelp>
            <valueHelp>
              <format>warning</format>
              <description>Warning messages</description>
            </valueHelp>
            <valueHelp>
              <format>notice</format>
              <description>Messages for further investigation</description>
            </valueHelp>
            <valueHelp>
              <format>info</format>
              <description>Informational messages</description>
            </valueHelp>
            <valueHelp>
              <format>debug</format>
              <description>Debug messages</description>
            </valueHelp>
            <constraint>
              <regex>(emergency|alert|critical|error|warning|notice|info|debug)</regex>
            </constraint>
          </properties>
          <defaultValue>info</defaultValue>
        </leafNode>
      </children>
    </node>
    <leafNode name="timestamping">
      <properties>
        <help>Enable adding timestamp to log</help>
        <valueless/>
      </properties>
    </leafNode>
    <leafNode name="keystroke-logging">
      <properties>
        <help>Enable logging transfer data, default is to log receive data only</help>
        <valueless/>
      </properties>
    </leafNode>
  </children>
</node>
<!-- include end -->
