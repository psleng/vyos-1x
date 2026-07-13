<!-- include start from serial/service/modbus-profileable.xml.i -->
<node name="modbus-gateway">
  <properties>
    <help>Modbus gateway service settings</help>
  </properties>
  <children>
    <node name="protocol">
      <properties>
        <help>Protocol</help>
      </properties>
      <children>
        <leafNode name="rtu">
          <properties>
            <help>Modbus/RTU protocol (default)</help>
            <valueless/>
          </properties>
        </leafNode>
        <node name="ascii">
          <properties>
            <help>Modbus/ASCII protocol</help>
          </properties>
          <children>
            <leafNode name="append-crlf">
              <properties>
                <help>Enable appending CR/LF to the end of the transmission in ASCII mode</help>
                <valueless/>
              </properties>
            </leafNode>
          </children>
        </node>
      </children>
    </node>
    <node name="master">
      <properties>
        <help>Modbus master service settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/tls-port.xml.i>
        <node name="slave-mapping-list">
          <properties>
            <help>Slave mapping list Modbus master will communicate with</help>
          </properties>
          <children>
            <tagNode name="entry">
              <properties>
                <help>Slave mapping list entry</help>
                <valueHelp>
                  <format>u32:1-16</format>
                  <description>Mapping Entry ID (1-16)</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 1-16"/>
                </constraint>
              </properties>
              <children>
                <leafNode name="protocol">
                  <properties>
                    <help>Protocol</help>
                    <completionHelp>
                      <list>tcp udp</list>
                    </completionHelp>
                    <constraint>
                      <regex>(tcp|udp)</regex>
                    </constraint>
                  </properties>
                  <defaultValue>tcp</defaultValue>
                </leafNode>
                <leafNode name="range-mode">
                  <properties>
                    <help>Specify the configuration of the Modbus Slaves on the network</help>
                    <completionHelp>
                      <list>host gateway</list>
                    </completionHelp>
                    <constraint>
                      <regex>(host|gateway)</regex>
                    </constraint>
                  </properties>
                  <defaultValue>host</defaultValue>
                </leafNode>
                <leafNode name="port">
                  <properties>
                    <help>Slave mapping entry port</help>
                    <valueHelp>
                      <format>u32:1-65535</format>
                      <description>Port number</description>
                    </valueHelp>
                    <constraint>
                      <validator name="numeric" argument="--range 1-65535"/>
                    </constraint>
                  </properties>
                  <defaultValue>502</defaultValue>
                </leafNode>
                <leafNode name="slave-ip">
                  <properties>
                    <help>IP Address</help>
                    <valueHelp>
                      <format>ipv4</format>
                      <description>IPv4 address</description>
                    </valueHelp>
                    <valueHelp>
                      <format>ipv6</format>
                      <description>IPv6 address</description>
                    </valueHelp>
                    <constraint>
                      <validator name="ip-address"/>
                    </constraint>
                  </properties>
                </leafNode>
                <leafNode name="uid">
                  <properties>
                    <help>Slave UID or UID range</help>
                    <valueHelp>
                      <format>start-end</format>
                      <description>UID range (e.g. 2-5) to match, [1, 247]</description>
                    </valueHelp>
                    <valueHelp>
                      <format>&lt;1-247&gt;</format>
                      <description>UID number, from 1 to 247</description>
                    </valueHelp>
                    <constraint>
                      <validator name="modbus-uid-range"/>
                    </constraint>
                  </properties>
                </leafNode>
              </children>
            </tagNode>
          </children>
        </node>
      </children>
    </node>
    <node name="slave">
      <properties>
        <help>Modbus slave service settings</help>
      </properties>
      <children>
        <tagNode name="remap">
          <properties>
            <help>Source master UID or UID range to remap from</help>
            <valueHelp>
              <format>start-end</format>
              <description>UID range (e.g. 2-5) to match</description>
            </valueHelp>
            <valueHelp>
              <format>&lt;1-247&gt;</format>
              <description>UID number, from 1 to 247</description>
            </valueHelp>
            <constraint>
              <validator name="modbus-uid-range"/>
            </constraint>
          </properties>
          <children>
            <leafNode name="to">
              <properties>
                <help>Destination slave UID or UID range to remap to</help>
                <valueHelp>
                  <format>start-end</format>
                  <description>UID range (e.g. 2-5) to match</description>
                </valueHelp>
                <valueHelp>
                  <format>&lt;1-247&gt;</format>
                  <description>UID number, from 1 to 247</description>
                </valueHelp>
                <constraint>
                  <validator name="modbus-uid-range"/>
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
